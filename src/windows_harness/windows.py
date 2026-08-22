"""Direct Windows control through public Win32 and UI Automation APIs.

One persistent process that can see any window, act on it, and report
honestly how intrusive each action was.

Every input primitive takes ``delivery="foreground" | "background"``.
Foreground (default) fronts the target and keeps it fronted across a burst
of actions (``hold=True``) — focus-driven UI such as autocomplete popups
closes the instant a window loses the foreground, so the harness holds it
until ``win.release()``. Background is the opt-in quiet path: synthetic pen
injection or window messages, never fronts, and refuses with
:class:`BackgroundUnavailable` when the framework would silently drop input.
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


def _click_delivery_hint(result: dict[str, Any], hwnd: int) -> str | None:
    """Posted clicks against WebView2/Tauri hosts are often silently ignored;
    point the agent at the designed escape hatch instead of more retries."""
    from .delivery import has_chromium_descendant

    if result.get("mode") == "message" and has_chromium_descendant(hwnd):
        return (
            "webview host (Tauri/WebView2): posted clicks may be ignored here. "
            "If verify_change(app) reports no change, record it once with "
            "note_drop('mouse_click', ...) and redo with delivery='foreground'."
        )
    return None


# UIA element handles are retired once the table outgrows this; evicted
# indices fail with an honest error instead of pinning COM references forever.
_ELEMENT_CACHE_LIMIT = 4096

# Auto-generated screenshots land in %TEMP% under this prefix and simply
# persist — agents read the returned path long after the producing process
# exits, so nothing deletes them proactively. Clean
# `%TEMP%\windows-harness-*.png` manually if they pile up.
_TEMP_SHOT_PREFIX = "windows-harness-"


class Windows:
    """Low-level Windows observation and control for one persistent process."""

    def __init__(self) -> None:
        if not is_interactive_desktop():
            raise HarnessError(
                "No interactive desktop is available (Session 0 or a locked "
                "workstation). Run windows-harness inside a logged-in session."
            )
        ensure_dpi_awareness()
        inject.recover_abandoned_cloaks()
        self._elements: dict[int, Any] = {}
        self._element_seq = 0
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
            "scripts_dir": str(delivery.scripts_dir()),
            "note": (
                "Elevated targets require an elevated harness (UIPI); "
                "everything else needs no administrator rights."
            ),
        }

    def list_apps(self, *, include_system: bool = False) -> list[dict[str, Any]]:
        return list_processes(include_system=include_system)

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
        # Indices come from a monotonic counter: a handle retired by eviction
        # or a newer snapshot can never alias a fresh element, so stale handles
        # fail loudly instead of silently acting on the wrong control.
        if len(self._elements) >= _ELEMENT_CACHE_LIMIT:
            for key in list(self._elements)[: _ELEMENT_CACHE_LIMIT // 4]:
                del self._elements[key]
        index = self._element_seq
        self._element_seq += 1
        self._elements[index] = element
        return index

    # --- focus guard -----------------------------------------------------------

    def _focus_outcome(
        self, before: int, operation: str, *, delivery: str, hold: bool
    ) -> dict[str, Any]:
        """Focus report for one action. A held foreground action is SUPPOSED
        to leave the target fronted, so the repair guard must not undo it."""
        if hold and delivery == "foreground":
            return {
                "foreground_before": before,
                "foreground_after": current_foreground(),
                "held": True,
            }
        return self._guard_focus(before, operation)

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

    def _capture_shot(self, app: str | None) -> tuple[int, dict[str, Any], dict[str, Any]]:
        """Capture one window with the image still in memory — no disk round
        trip; the caller encodes once, at its final size."""
        hwnd, info = self._resolve_hwnd(app)
        shot = capture_window(hwnd)
        if shot["minimized"]:
            # PrintWindow renders the iconic sliver for minimized windows;
            # restore invisibly under the cloak, capture, then re-minimize.
            with inject.cloaked_focus(hwnd, cloak=True) as _cloaked:
                shot = capture_window(hwnd)
        return hwnd, info, shot

    def _save_shot(self, image: Any, path: str | Path | None) -> Path:
        if path is None:
            with tempfile.NamedTemporaryFile(
                prefix=_TEMP_SHOT_PREFIX, suffix=".png", delete=False
            ) as handle:
                output = Path(handle.name)
        else:
            output = Path(path).expanduser().resolve()
            output.parent.mkdir(parents=True, exist_ok=True)
        image.save(output, format="PNG", compress_level=1)
        return output

    def capture_screenshot(
        self,
        app: str | None = None,
        *,
        path: str | Path | None = None,
    ) -> dict[str, Any]:
        hwnd, info, shot = self._capture_shot(app)
        image = shot.pop("image")
        output = self._save_shot(image, path)

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
        hwnd, info, shot = self._capture_shot(app)
        image = shot.pop("image")  # the capturer always hands back RGB pixels
        raw_size = (image.width, image.height)

        from PIL import Image, ImageDraw

        ratio = min(1.0, max_width / image.width, max_height / image.height)
        if ratio < 1.0:
            size = (
                max(1, round(image.width * ratio)),
                max(1, round(image.height * ratio)),
            )
            image = image.resize(size, Image.Resampling.LANCZOS)

        bounds = shot["client_bounds"]
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

        output = self._save_shot(image, path)
        foreground = current_foreground()
        screenshot = {
            "path": str(output),
            "app": info,
            "hwnd": hwnd,
            "raw_width": raw_size[0],
            "raw_height": raw_size[1],
            "width": image.width,
            "height": image.height,
            "client_bounds": bounds,
            "scale_x": scale_x,
            "scale_y": scale_y,
            "backend": shot["backend"],
            "minimized": shot["minimized"],
            "virtual_pointer": pointer_info,
            "focus": {
                "foreground_hwnd": foreground,
                "target_is_foreground": foreground == hwnd,
            },
        }
        self._last_window = info
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
        if coordinate_space == "normalized":
            # VLM-friendly 0..1000 grid over the window's client area —
            # independent of screenshot resolution and DPI scaling.
            if not (0.0 <= float(x) <= 1000.0 and 0.0 <= float(y) <= 1000.0):
                raise HarnessError(
                    f"normalized coordinates must be within 0..1000, got ({x}, {y})"
                )
            bounds = shot["client_bounds"]
            x = float(x) / 1000.0 * float(bounds["width"])
            y = float(y) / 1000.0 * float(bounds["height"])
        elif coordinate_space == "screenshot":
            x = float(x) / float(shot["scale_x"])
            y = float(y) / float(shot["scale_y"])
        elif coordinate_space not in ("client", "window"):
            raise HarnessError(
                "coordinate_space must be 'screenshot', 'normalized', "
                "'client', or 'screen'"
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

    def hover(
        self,
        x: float,
        y: float,
        *,
        app: str | None = None,
        dwell: float = 0.6,
        coordinate_space: str = "screenshot",
        delivery: str = "foreground",
        hold: bool = True,
    ) -> dict[str, Any]:
        """Really hover the point so tooltips and hover states fire.

        Unlike ``win.move`` (which only animates the virtual pointer and
        delivers no input), this moves the physical cursor — or injects a
        pen hover — so the target's tooltip actually appears. Hover is
        coordinate-routed: the target only needs to be unoccluded, not
        foreground. The cursor stays put afterwards, because moving it away
        dismisses the tooltip before anyone can read it; follow with
        ``win.see()`` to read it.
        """
        hwnd, _info = self._resolve_hwnd(app)
        point = self._screen_point(x, y, coordinate_space)
        self._pointer_position = point
        self._overlay.move(*point)
        return inject.hover_screen(
            hwnd, point, delivery_mode=delivery, hold=hold, dwell=dwell
        )

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
        delivery: str = "foreground",
        hold: bool = True,
    ) -> dict[str, Any]:
        """Coordinate click; foreground (default) fronts and holds the target,
        background routes pen/message without disturbing the user."""
        hwnd, info = self._resolve_hwnd(app)
        point = self._screen_point(x, y, coordinate_space)
        focus_before = current_foreground()
        self._pointer_position = point
        self._overlay.move(*point)
        result = inject.click_screen(
            hwnd, point, button=button, clicks=clicks, delivery_mode=delivery,
            hold=hold,
        )
        self._overlay.click()
        result["focus"] = self._focus_outcome(focus_before, "click", delivery=delivery, hold=hold)
        result["app"] = info
        hint = _click_delivery_hint(result, hwnd)
        if hint:
            result["hint"] = hint
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
        delivery: str = "foreground",
        hold: bool = True,
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
            delivery_mode=delivery, hold=hold,
        )
        result["focus"] = self._focus_outcome(focus_before, "drag", delivery=delivery, hold=hold)
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
        delivery: str = "foreground",
        hold: bool = True,
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
        result = inject.scroll(hwnd, point, delta_y, delta_x, delivery_mode=delivery, hold=hold)
        result["focus"] = self._focus_outcome(focus_before, "scroll", delivery=delivery, hold=hold)
        return result

    def type(
        self,
        text: str,
        *,
        app: str | None = None,
        x: float | None = None,
        y: float | None = None,
        coordinate_space: str = "screenshot",
        delivery: str = "foreground",
        hold: bool = True,
    ) -> dict[str, Any]:
        """Type text, optionally focusing a field at (x, y) first.

        Focus-driven fields (search boxes with suggestion popups) only accept
        input while focused; passing x/y clicks the field in the same call so
        the focus-then-type sequence cannot be split by a focus loss.
        """
        hwnd, _info = self._resolve_hwnd(app)
        if (x is None) != (y is None):
            raise HarnessError("Provide both x and y to focus a field before typing")
        focus_before = current_foreground()
        if x is not None and y is not None:
            point = self._screen_point(x, y, coordinate_space)
            self._pointer_position = point
            self._overlay.move(*point)
            inject.click_screen(hwnd, point, button="left", clicks=1,
                                delivery_mode=delivery, hold=hold)
            time.sleep(0.12)  # let the field's focus handlers settle
        result = inject.type_text(hwnd, text, delivery_mode=delivery, hold=hold)
        result["focus"] = self._focus_outcome(focus_before, "typing", delivery=delivery, hold=hold)
        return result

    def key(self, key: str, *, app: str | None = None, delivery: str = "foreground", hold: bool = True) -> dict[str, Any]:
        hwnd, _info = self._resolve_hwnd(app)
        focus_before = current_foreground()
        result = inject.press_key(hwnd, key, delivery_mode=delivery, hold=hold)
        result["focus"] = self._focus_outcome(focus_before, f"key {key!r}", delivery=delivery, hold=hold)
        return result

    def paste(self, text: str, *, app: str | None = None, delivery: str = "foreground", hold: bool = True) -> dict[str, Any]:
        """Set the clipboard to `text` and paste it into the target.

        The text route that survives swallowed SendInput: setting the
        clipboard is a plain data handoff no hook filters, and only the
        Ctrl+V trigger needs input. Focus the target field first (e.g.
        ``win.type`` with x/y, or ``win.click``).
        """
        hwnd, _info = self._resolve_hwnd(app)
        focus_before = current_foreground()
        inject.set_clipboard_text(text)
        result = inject.paste_text(hwnd, delivery_mode=delivery, hold=hold)
        result["clipboard"] = text
        result["focus"] = self._focus_outcome(focus_before, "paste", delivery=delivery, hold=hold)
        return result

    def release(self) -> dict[str, Any]:
        """Give the foreground back to the window the user had before a held
        burst. Safe to call when nothing is held."""
        held = inject.release_hold()
        return {"released": held is not None, "held": held}

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

        Every poll is a full PrintWindow capture (each bounded by its own
        ~4 s hung-window timeout), so worst-case wall time can exceed
        ``timeout`` by up to two capture timeouts.
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
        # dump(screenshot=True) shoots FIRST, so its frames are reported in
        # this very screenshot's pixel space.
        state = self.ax.dump(
            app, max_depth=max_depth, max_nodes=max_nodes, screenshot=screenshot
        )
        state["windows"] = self.windows(app)
        if not screenshot:
            state["screenshot"] = None
        return state

    snapshot = get_app_state
