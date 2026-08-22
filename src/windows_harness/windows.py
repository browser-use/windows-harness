"""Direct Windows control through public Win32 and UI Automation APIs.

The counterpart of ``macos.py``: one persistent process that can see any
window, act on it, and report honestly how intrusive each action was.

Every input primitive takes ``delivery="background" | "foreground"``.
Background (default) never fronts the target: it routes through synthetic
pen injection or window messages, or raises :class:`BackgroundUnavailable`
with a structured reason. Foreground is the agent's explicit escalation:
a brief cloaked takeover, restored afterwards.
"""

from __future__ import annotations

import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any

from . import delivery
from . import inject
from .capture import (
    HarnessError,
    capture_window,
    ensure_dpi_awareness,
    is_interactive_desktop,
    list_processes,
    process_image_name,
    resolve_hwnd,
    windows_for_process,
)
from .controls import Accessibility
from .inject import (
    client_to_screen,
    current_foreground,
    force_foreground,
)
from .overlay import LivePointerOverlay
from .pointer import POINTER_HOTSPOT, pointer_points

try:
    import ctypes

    _IS_ADMIN = bool(ctypes.windll.shell32.IsUserAnAdmin())
except Exception:  # pragma: no cover
    _IS_ADMIN = False


class FocusChangedError(HarnessError):
    """A background-targeted action disturbed the user's foreground."""


class Windows:
    """Low-level Windows observation and control for one persistent process."""

    def __init__(self) -> None:
        if not is_interactive_desktop():
            raise HarnessError(
                "No interactive desktop is available (Session 0 or a locked "
                "workstation). Run windows-harness inside a logged-in session."
            )
        ensure_dpi_awareness()
        self._elements: dict[int, Any] = {}
        self._last_window: dict[str, Any] | None = None
        self._last_screenshot: dict[str, Any] | None = None
        self._pointer_position: tuple[float, float] | None = None
        self._overlay = LivePointerOverlay()
        self.ax = Accessibility(self)

    # --- runtime report ----------------------------------------------------

    def doctor(self) -> dict[str, Any]:
        try:
            import uiautomation  # noqa: F401

            uia_ok = True
        except ImportError:
            uia_ok = False
        try:
            inject.CreateSyntheticPointerDevice  # noqa: B018 - availability probe
            pen_ok = True
        except (AttributeError, HarnessError):
            pen_ok = False
        sendinput = inject.sendinput_health()
        # The fresh verdict doubles as this session's transport-selection cache.
        inject._health_cache = sendinput
        return {
            "platform": "Windows",
            "interactive_desktop": is_interactive_desktop(),
            "elevated": _IS_ADMIN,
            "uiautomation": uia_ok,
            "synthetic_pointer": pen_ok,
            "input_health": {
                # Hook software can eat SendInput events; foreground delivery
                # then falls back to pen/message transports automatically.
                "sendinput": sendinput,
                "possible_injectors": inject.suspect_injectors(),
                "observed_drops": delivery.observed_drops(),
                "drops_path": str(delivery.config_dir() / "drops.json"),
            },
            "note": (
                "Elevated targets require an elevated harness (UIPI); "
                "everything else needs no administrator rights."
            ),
        }

    def list_apps(self) -> list[dict[str, Any]]:
        return list_processes()

    def windows(self, app: str) -> list[dict[str, Any]]:
        _hwnd, info = self._resolve_hwnd(app)
        result = []
        for window in windows_for_process(info["pid"]):
            left, top, right, bottom = window["bounds"]
            result.append(
                {
                    "hwnd": window["hwnd"],
                    "title": window["title"],
                    "class_name": window["class_name"],
                    "bounds": {
                        "x": left,
                        "y": top,
                        "width": right - left,
                        "height": bottom - top,
                    },
                    "visible": window["visible"],
                    "cloaked": window["cloaked"],
                    "minimized": window["minimized"],
                }
            )
        return result

    # --- app resolution ------------------------------------------------------

    def _resolve_hwnd(self, query: str | None) -> tuple[int, dict[str, Any]]:
        if not query and self._last_window:
            query = str(self._last_window["hwnd"])
        if not query:
            raise HarnessError("Specify an app name, exe name, title, or HWND")
        hwnd, window = resolve_hwnd(str(query))
        info = {
            "hwnd": hwnd,
            "pid": window["pid"],
            "process": window["process"],
            "title": window["title"],
            "path": process_image_name(window["pid"]),
        }
        self._last_window = info
        return hwnd, info

    # --- element handles ------------------------------------------------------

    def _element(self, element_index: int) -> Any:
        try:
            return self._elements[int(element_index)]
        except (KeyError, ValueError) as exc:
            raise HarnessError(
                f"Unknown element index {element_index!r}; take a fresh "
                "snapshot first"
            ) from exc

    def _remember_element(self, element: Any) -> int:
        index = max(self._elements, default=-1) + 1
        self._elements[index] = element
        return index

    # --- focus guard -----------------------------------------------------------

    def _guard_focus(self, before: int, operation: str) -> dict[str, Any]:
        """Restore the user's foreground if an action displaced it."""
        after = current_foreground()
        if after == before or not before:
            return {"foreground_before": before, "foreground_after": after}
        try:
            force_foreground(before)
        except HarnessError as exc:
            raise FocusChangedError(
                f"the foreground moved to {after:#x} during {operation} "
                f"and could not be restored: {exc}"
            ) from exc
        return {
            "foreground_before": before,
            "foreground_after": current_foreground(),
            "focus_repaired": True,
        }

    # --- screenshots ---------------------------------------------------------

    def capture_screenshot(
        self,
        app: str | None = None,
        *,
        path: str | Path | None = None,
    ) -> dict[str, Any]:
        hwnd, info = self._resolve_hwnd(app)
        shot = capture_window(hwnd)
        if shot["minimized"]:
            # PrintWindow renders the iconic sliver for minimized windows;
            # restore invisibly under the cloak, capture, then re-minimize.
            with inject.cloaked_focus(hwnd, cloak=True) as _cloaked:
                shot = capture_window(hwnd)
        image = shot.pop("image")

        if path is None:
            with tempfile.NamedTemporaryFile(
                prefix="windows-harness-", suffix=".png", delete=False
            ) as handle:
                output = Path(handle.name)
        else:
            output = Path(path).expanduser().resolve()
            output.parent.mkdir(parents=True, exist_ok=True)
        image.save(output, format="PNG", compress_level=1)

        bounds = shot["client_bounds"]
        screenshot = {
            "path": str(output),
            "app": info,
            "hwnd": hwnd,
            "width": image.width,
            "height": image.height,
            "client_bounds": bounds,
            "scale_x": shot["scale_x"],
            "scale_y": shot["scale_y"],
            "backend": shot["backend"],
            "minimized": shot["minimized"],
        }
        self._last_window = info
        self._last_screenshot = screenshot
        return screenshot

    def see(
        self,
        app: str | None = None,
        *,
        path: str | Path | None = None,
        max_width: int = 1280,
        max_height: int = 1280,
        show_pointer: bool = True,
    ) -> dict[str, Any]:
        """Capture a bounded window image and draw the harness pointer onto it."""
        if max_width <= 0 or max_height <= 0:
            raise HarnessError("max_width and max_height must be positive")
        screenshot = self.capture_screenshot(app, path=path)
        output = Path(screenshot["path"])

        from PIL import Image, ImageDraw

        with Image.open(output) as source:
            image = source.convert("RGB")
        ratio = min(1.0, max_width / image.width, max_height / image.height)
        if ratio < 1.0:
            size = (
                max(1, round(image.width * ratio)),
                max(1, round(image.height * ratio)),
            )
            image = image.resize(size, Image.Resampling.LANCZOS)

        bounds = screenshot["client_bounds"]
        scale_x = image.width / float(bounds["width"])
        scale_y = image.height / float(bounds["height"])
        pointer = self._pointer_position
        pointer_info: dict[str, Any] | None = None
        if pointer is not None:
            image_x = (pointer[0] - bounds["x"]) * scale_x
            image_y = (pointer[1] - bounds["y"]) * scale_y
            inside = 0 <= image_x < image.width and 0 <= image_y < image.height
            pointer_info = {
                "screen": {"x": pointer[0], "y": pointer[1]},
                "image": {"x": image_x, "y": image_y},
                "inside": inside,
                "visible": self._overlay.visible,
            }
            if show_pointer and self._overlay.visible and inside:
                draw = ImageDraw.Draw(image)
                hot_x, hot_y = POINTER_HOTSPOT
                points = [
                    (
                        round(image_x + (px - hot_x) * scale_x),
                        round(image_y + (py - hot_y) * scale_y),
                    )
                    for px, py in pointer_points()
                ]
                shadow = [
                    (px + max(1, round(scale_x)), py + max(1, round(scale_y)))
                    for px, py in points
                ]
                draw.polygon(shadow, fill=(70, 70, 70))
                outline_width = max(2, round(2.5 * (scale_x + scale_y) / 2))
                draw.polygon(points, fill=(10, 10, 10), outline=(255, 255, 255), width=outline_width)

        image.save(output, format="PNG", compress_level=1)
        foreground = current_foreground()
        screenshot.update(
            {
                "raw_width": screenshot["width"],
                "raw_height": screenshot["height"],
                "width": image.width,
                "height": image.height,
                "scale_x": scale_x,
                "scale_y": scale_y,
                "virtual_pointer": pointer_info,
                "focus": {
                    "foreground_hwnd": foreground,
                    "target_is_foreground": foreground == screenshot["hwnd"],
                },
            }
        )
        self._last_screenshot = screenshot
        return screenshot

    # --- coordinates -----------------------------------------------------------

    def _screen_point(self, x: float, y: float, coordinate_space: str) -> tuple[float, float]:
        if coordinate_space == "screen":
            return float(x), float(y)
        if self._last_screenshot is None or self._last_window is None:
            raise HarnessError(
                "Take a screenshot before using window or screenshot "
                "coordinates, or pass coordinate_space='screen'"
            )
        shot = self._last_screenshot
        if coordinate_space == "screenshot":
            x = float(x) / float(shot["scale_x"])
            y = float(y) / float(shot["scale_y"])
        elif coordinate_space not in ("client", "window"):
            raise HarnessError(
                "coordinate_space must be 'screenshot', 'client', or 'screen'"
            )
        screen_x, screen_y = client_to_screen(shot["hwnd"], x, y)
        return float(screen_x), float(screen_y)

    def _pointer_info(self) -> dict[str, Any] | None:
        if self._pointer_position is None:
            return None
        screen_x, screen_y = self._pointer_position
        result: dict[str, Any] = {"screen": {"x": screen_x, "y": screen_y}}
        shot = self._last_screenshot
        if shot is not None:
            bounds = shot["client_bounds"]
            image_x = (screen_x - bounds["x"]) * float(shot["scale_x"])
            image_y = (screen_y - bounds["y"]) * float(shot["scale_y"])
            result["image"] = {"x": image_x, "y": image_y}
            result["inside"] = 0 <= image_x < float(shot["width"]) and 0 <= image_y < float(
                shot["height"]
            )
        return result

    # --- virtual pointer ---------------------------------------------------

    def move(
        self,
        x: float,
        y: float,
        *,
        coordinate_space: str = "screenshot",
        duration: float = 0.16,
    ) -> dict[str, Any]:
        """Animate the virtual pointer without moving the physical cursor."""
        self._pointer_position = self._screen_point(x, y, coordinate_space)
        self._overlay.move(*self._pointer_position, duration=duration)
        pointer = self._pointer_info()
        assert pointer is not None
        return pointer

    def show_pointer(self) -> dict[str, Any]:
        if self._pointer_position is None:
            raise HarnessError("Move the virtual pointer before showing it")
        self._overlay.show(*self._pointer_position)
        pointer = self._pointer_info()
        assert pointer is not None
        return pointer

    def hide_pointer(self) -> None:
        self._overlay.hide()

    # --- input primitives --------------------------------------------------

    def click(
        self,
        x: float,
        y: float,
        *,
        app: str | None = None,
        button: str = "left",
        clicks: int = 1,
        coordinate_space: str = "screenshot",
        delivery: str = "background",
    ) -> dict[str, Any]:
        """Coordinate click; background routes pen/message, foreground cloaks."""
        hwnd, info = self._resolve_hwnd(app)
        point = self._screen_point(x, y, coordinate_space)
        focus_before = current_foreground()
        self._pointer_position = point
        self._overlay.move(*point)
        result = inject.click_screen(
            hwnd, point, button=button, clicks=clicks, delivery_mode=delivery
        )
        self._overlay.click()
        result["focus"] = self._guard_focus(focus_before, "click")
        result["app"] = info
        return result

    def drag(
        self,
        from_x: float,
        from_y: float,
        to_x: float,
        to_y: float,
        *,
        app: str | None = None,
        button: str = "left",
        coordinate_space: str = "screenshot",
        duration: float = 0.25,
        steps: int = 12,
        delivery: str = "background",
    ) -> dict[str, Any]:
        hwnd, _info = self._resolve_hwnd(app)
        start = self._screen_point(from_x, from_y, coordinate_space)
        end = self._screen_point(to_x, to_y, coordinate_space)
        focus_before = current_foreground()
        self._pointer_position = start
        self._overlay.move(*start, duration=0)
        self._overlay.move(*end, duration=duration)
        result = inject.drag(
            hwnd, start, end, button=button, steps=steps, duration=duration,
            delivery_mode=delivery,
        )
        result["focus"] = self._guard_focus(focus_before, "drag")
        return result

    def scroll(
        self,
        delta_y: int = 0,
        delta_x: int = 0,
        *,
        app: str | None = None,
        x: float | None = None,
        y: float | None = None,
        coordinate_space: str = "screenshot",
        delivery: str = "background",
    ) -> dict[str, Any]:
        if not delta_y and not delta_x:
            raise HarnessError("Provide a nonzero delta_y or delta_x")
        hwnd, _info = self._resolve_hwnd(app)
        if (x is None) != (y is None):
            raise HarnessError("Provide both x and y when targeting a scroll point")
        if x is not None and y is not None:
            point = self._screen_point(x, y, coordinate_space)
        else:
            from .capture import client_bounds

            bounds = client_bounds(hwnd)
            point = (bounds[0] + bounds[2] / 2.0, bounds[1] + bounds[3] / 2.0)
        self._pointer_position = point
        self._overlay.move(*point)
        focus_before = current_foreground()
        result = inject.scroll(hwnd, point, delta_y, delta_x, delivery_mode=delivery)
        result["focus"] = self._guard_focus(focus_before, "scroll")
        return result

    def type(self, text: str, *, app: str | None = None, delivery: str = "background") -> dict[str, Any]:
        hwnd, _info = self._resolve_hwnd(app)
        focus_before = current_foreground()
        result = inject.type_text(hwnd, text, delivery_mode=delivery)
        result["focus"] = self._guard_focus(focus_before, "typing")
        return result

    def key(self, key: str, *, app: str | None = None, delivery: str = "background") -> dict[str, Any]:
        hwnd, _info = self._resolve_hwnd(app)
        focus_before = current_foreground()
        result = inject.press_key(hwnd, key, delivery_mode=delivery)
        result["focus"] = self._guard_focus(focus_before, f"key {key!r}")
        return result

    # --- verification --------------------------------------------------------

    @staticmethod
    def _fingerprint(hwnd: int) -> bytes:
        from PIL import Image

        image = capture_window(hwnd)["image"]
        return image.convert("L").resize((64, 64), Image.Resampling.BILINEAR).tobytes()

    def verify_change(
        self,
        app: str | None = None,
        *,
        timeout: float = 2.0,
        interval: float = 0.12,
        sensitivity: float = 0.002,
    ) -> dict[str, Any]:
        """Background pixel diff: did the window's pixels change within timeout?

        The cheap confirmation for a ``verified: false`` action — no raise, no
        foreground, no agent vision round trip. Animated windows (video,
        blinking carets) report ``changed`` on their own; treat those targets
        as unverifiable here and confirm semantically through ``win.ax``.
        """
        hwnd, info = self._resolve_hwnd(app)
        before = self._fingerprint(hwnd)
        started = time.monotonic()
        deadline = started + max(0.0, timeout)
        while True:
            time.sleep(max(0.02, interval))
            after = self._fingerprint(hwnd)
            differ = sum(a != b for a, b in zip(before, after, strict=True))
            changed = differ / float(len(before)) > max(0.0, sensitivity)
            elapsed = time.monotonic() - started
            if changed or time.monotonic() >= deadline:
                return {"changed": changed, "app": info, "elapsed": round(elapsed, 3)}

    def note_drop(self, kind: str, *, app: str | None = None) -> dict[str, Any]:
        """Record first-hand evidence that this window's framework silently
        drops `kind` in the background; future background calls to windows of
        the same class refuse honestly instead of repeating a dead transport."""
        hwnd, info = self._resolve_hwnd(app)
        drops = delivery.note_observed_drop(hwnd, kind)
        return {"observed_drops": drops, "app": info}

    # --- PowerShell escape hatch ---------------------------------------------

    def script(self, command: str, *, timeout: float = 60.0) -> str:
        """Run one PowerShell snippet; the AppleScript-shaped escape hatch."""
        result = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", "-"],
            input=command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )
        if result.returncode != 0:
            raise HarnessError((result.stderr or result.stdout).strip())
        return result.stdout.rstrip("\n")

    # --- state ---------------------------------------------------------------

    def get_app_state(
        self,
        app: str,
        *,
        screenshot: bool = False,
        max_depth: int = 12,
        max_nodes: int = 1500,
    ) -> dict[str, Any]:
        state = self.ax.dump(app, max_depth=max_depth, max_nodes=max_nodes)
        state["windows"] = self.windows(app)
        state["screenshot"] = self.see(app) if screenshot else None
        return state

    snapshot = get_app_state
