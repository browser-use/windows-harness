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

import json
import os
import subprocess
import tempfile
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from . import delivery, inject
from .annotation import annotate as annotate_image
from .capture import (
    HarnessError,
    StaleWindowError,
    anchor_health,
    capture_screen,
    capture_window,
    client_bounds,
    dpi_health,
    ensure_dpi_awareness,
    is_interactive_desktop,
    list_processes,
    process_dpi_awareness,
    process_image_name,
    resolve_hwnd,
    windows_for_process,
)
from .controls import Accessibility
from .inject import (
    current_foreground,
    force_foreground,
    foreground_root_hwnd,
    point_on_screen,
    zip_strict,
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
# Action proofs get their own prefix so a forgotten `result["proof"]["path"]`
# is still recoverable unambiguously via `win.last_proof()`.
_TEMP_PROOF_PREFIX = "windows-harness-proof-"
# The proof journal is append-only and self-trimming: once it passes
# _JOURNAL_MAX_BYTES only the newest _JOURNAL_KEEP_LINES entries survive, so
# long-lived machines never accumulate an unbounded index.
_JOURNAL_MAX_BYTES = 256 * 1024
_JOURNAL_KEEP_LINES = 500

def _env_bool(name: str, *, default: bool = True) -> bool:
    """Read a true/false environment flag; empty values keep the default."""
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().casefold() not in ("off", "0", "false", "no")

def _click_label(button: str, clicks: int) -> str:
    if clicks != 1:
        return f"{clicks}x {button} click"
    if button == "left":
        return "click"
    return f"{button} click"

def _scroll_label(delta_y: int, delta_x: int) -> str:
    parts = []
    if delta_y:
        parts.append("up" if delta_y > 0 else "down")
    if delta_x:
        parts.append("right" if delta_x > 0 else "left")
    direction = "/".join(parts) or "scroll"
    return f"scroll {direction} dy={delta_y} dx={delta_x}"


def _normalize_newlines(value: str) -> str:
    """Collapse CRLF/CR to LF so read-back compares against typed ``\\n``."""
    return value.replace("\r\n", "\n").replace("\r", "\n")


def _text_landed(
    read_back: str | None, typed: str, before: str | None = None
) -> bool | None:
    """Did the typed text make it into the read-back value?

    Returns ``None`` when there is nothing to compare (read-back unavailable)
    or the result is ambiguous (text present mid-document with no ``before``
    snapshot), ``True`` when the typed text is confirmed present, and
    ``False`` when the read-back contradicts the delivery -- either the field
    is *shorter* than what was typed (the signature of a text box that dropped
    injected characters, XAML/CEF editors) or, with a ``before`` snapshot,
    the document neither contains the payload nor grew by its length (the
    signature of a launch/session-restore race eating the burst).
    """
    if read_back is None:
        return None
    needle = _normalize_newlines(typed).rstrip()
    haystack = _normalize_newlines(read_back).rstrip()
    if not needle:
        return True
    if haystack == needle or haystack.endswith(needle):
        return True
    if before is not None:
        before_norm = _normalize_newlines(before).rstrip()
        # Typed into the middle of existing content: the payload is present
        # now and was not there before.
        if needle in haystack and needle not in before_norm:
            return True
        # The payload was already present elsewhere in the document (typing a
        # repeated word); confirm by insertion delta instead of position.
        grew = len(_normalize_newlines(read_back)) - len(_normalize_newlines(before))
        if grew >= len(_normalize_newlines(typed)):
            return True
        # Both snapshots available and neither presence nor growth confirms
        # the payload: the text did not land intact.
        return False
    if len(haystack) < len(needle):
        return False
    # Mid-document or unrelated content: do not guess, only a shorter-than-
    # typed field proves an injection drop.
    return None

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
        self._proof_enabled = _env_bool("WINDOWS_HARNESS_PROOF", default=True)
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
            "dpi": dpi_health(process_dpi_awareness()),
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
            # No app named and no prior see(): target the foreground window, so
            # a bare ``see()`` grabs the modal overlay that just appeared (e.g.
            # a Save As dialog) instead of raising "Specify an app name".
            root = foreground_root_hwnd()
            if root:
                query = str(root)
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

    def _target_hwnd(self, app: str | None) -> tuple[int, dict[str, Any]]:
        """Resolve the window to inject into, keeping it consistent with the
        window the coordinate mapping in :meth:`_screen_point` uses.

        :meth:`_screen_point` converts screenshot/normalised/client
        coordinates against ``self._last_screenshot["hwnd"]``. Injecting into
        a *different* freshly re-resolved window would fire coordinates
        computed for window A at window B. So when the caller did not name a
        different app (or named one that matches the anchored window), reuse
        the anchored hwnd; only fall back to a fresh resolution when the
        caller explicitly targets a different window, and re-anchor then.
        """
        if app and self._last_window:
            target = self._last_window
            needle = str(app).casefold()
            matches = (
                needle == str(target["hwnd"]).casefold()
                or needle == target["process"].casefold()
                or needle == target["process"].removesuffix(".exe").casefold()
                or needle == target["title"].casefold()
            )
            if not matches:
                # User asked for a different window: resolve and re-anchor so
                # the screenshot coordinate basis follows the new target.
                return self._resolve_hwnd(app)
            return target["hwnd"], target
        if app:
            return self._resolve_hwnd(app)
        if self._last_window:
            return self._last_window["hwnd"], self._last_window
        raise HarnessError("Specify an app name, exe name, title, or HWND")

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

    def _capture_shot(
        self, app: str | None, *, bring_to_front: bool = False
    ) -> tuple[int, dict[str, Any], dict[str, Any]]:
        """Capture one window with the image still in memory — no disk round
        trip; the caller encodes once, at its final size."""
        hwnd, info = self._resolve_hwnd(app)
        shot = capture_window(hwnd)
        if bring_to_front:
            # Explicit front: restore a minimized target, bring it to the
            # foreground, and keep it fronted until release() so the caller
            # can follow up with foreground-delivery input.
            with inject.cloaked_focus(hwnd, cloak=False, hold=True) as _cloaked:
                shot = capture_window(hwnd)
        elif shot["minimized"]:
            # PrintWindow renders the iconic sliver for minimized windows;
            # restore invisibly under the cloak, capture, then re-minimize.
            with inject.cloaked_focus(hwnd, cloak=True) as _cloaked:
                shot = capture_window(hwnd)
        return hwnd, info, shot

    def _save_shot(
        self,
        image: Any,
        path: str | Path | None,
        prefix: str = _TEMP_SHOT_PREFIX,
    ) -> Path:
        if path is None:
            with tempfile.NamedTemporaryFile(
                prefix=prefix, suffix=".png", delete=False
            ) as handle:
                output = Path(handle.name)
        else:
            output = Path(path).expanduser().resolve()
            output.parent.mkdir(parents=True, exist_ok=True)
        image.save(output, format="PNG", compress_level=1)
        return output

    def _annotate_proof(
        self,
        hwnd: int,
        point: tuple[float, float],
        *,
        kind: str,
        label: str,
        coordinate_space: str,
        delta: dict[str, float] | None = None,
        end: tuple[float, float] | None = None,
        annotate: bool = True,
    ) -> dict[str, Any] | None:
        """Stamp an action's intended point onto a screenshot and return it.

        Reuses the screenshot the coordinates were anchored to (so the marker
        is pixel-exact against the image the agent reasoned over), and falls
        back to a fresh capture of the target window when no matching
        screenshot is held in memory. The source screenshot is never mutated
        on disk; the annotated copy is written to a new temp path.
        """
        if not (annotate and self._proof_enabled):
            return None
        from PIL import Image

        try:
            shot = self._last_screenshot
            image: Image.Image | None = None
            if shot is not None and shot.get("hwnd") == hwnd and shot.get("path"):
                try:
                    image = Image.open(shot["path"])
                    image.load()
                except OSError:
                    image = None
            if image is not None:
                bounds = shot["client_bounds"]
                scale_x = float(shot["scale_x"])
                scale_y = float(shot["scale_y"])
            else:
                fresh = capture_window(hwnd)
                image = fresh["image"]
                bounds = fresh["client_bounds"]
                scale_x = float(fresh["scale_x"])
                scale_y = float(fresh["scale_y"])

            image_x = (point[0] - bounds["x"]) * scale_x
            image_y = (point[1] - bounds["y"]) * scale_y
            actions: list[dict[str, Any]] = [
                {"kind": kind, "x": image_x, "y": image_y, "label": label}
            ]
            if delta:
                actions[0]["delta_x"] = float(delta.get("delta_x", 0))
                actions[0]["delta_y"] = float(delta.get("delta_y", 0))
            if end is not None:
                actions[0]["end_x"] = (end[0] - bounds["x"]) * scale_x
                actions[0]["end_y"] = (end[1] - bounds["y"]) * scale_y

            annotate_image(image, actions)
            output = self._save_shot(image, None, prefix=_TEMP_PROOF_PREFIX)
            proof: dict[str, Any] = {
                "path": str(output),
                "kind": kind,
                "label": label,
                "coordinate_space": coordinate_space,
                "image": {"x": round(image_x, 1), "y": round(image_y, 1)},
                "screen": {"x": round(point[0], 1), "y": round(point[1], 1)},
            }
            if delta:
                proof["delta"] = {
                    key: round(delta[key], 1)
                    for key in ("delta_x", "delta_y")
                    if key in delta
                }
            if end is not None:
                proof["end"] = {
                    "x": round((end[0] - bounds["x"]) * scale_x, 1),
                    "y": round((end[1] - bounds["y"]) * scale_y, 1),
                }
            self._journal_proof(proof, hwnd)
            return proof
        except Exception:
            # The proof is best-effort: a capture/encode failure must never
            # turn a successful coordinate action into a reported failure.
            return None

    def _journal_proof(self, proof: dict[str, Any], hwnd: int) -> None:
        """Append one JSON line to the proof journal; best-effort, never
        raises — an index hiccup must not affect the returned proof."""
        try:
            entry: dict[str, Any] = {
                "ts": datetime.now().astimezone().isoformat(timespec="seconds"),
                **proof,
            }
            last = self._last_window
            if last and last.get("hwnd") == hwnd:
                entry["app"] = {
                    "hwnd": hwnd,
                    "process": last.get("process"),
                    "title": last.get("title"),
                }
            journal = delivery.proofs_journal()
            journal.parent.mkdir(parents=True, exist_ok=True)
            with journal.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
            self._rotate_journal(journal)
        except Exception:  # noqa: BLE001
            pass

    @staticmethod
    def _rotate_journal(journal: Path) -> None:
        try:
            if journal.stat().st_size <= _JOURNAL_MAX_BYTES:
                return
            lines = journal.read_text(encoding="utf-8").splitlines()
            journal.write_text(
                "\n".join(lines[-_JOURNAL_KEEP_LINES:]) + "\n", encoding="utf-8"
            )
        except OSError:
            pass

    def capture_screen(self, *, path: str | Path | None = None) -> dict[str, Any]:
        """Capture the entire virtual desktop (saved to a PNG, path returned).

        Unlike :meth:`see` (which is window-scoped and therefore misses a
        modal overlay sitting on top), this sees every window including Save
        As / Open / Print dialogs. Screenshot-space coordinates match the
        physical pixels of the virtual desktop, so they pair with
        ``coordinate_space="screen"``.
        """
        shot = capture_screen()
        image = shot.pop("image")
        output = self._save_shot(image, path)
        bounds = shot["client_bounds"]
        self._last_screenshot = {
            "path": str(output),
            "app": {"hwnd": 0, "pid": 0, "process": "<screen>", "title": "desktop"},
            "hwnd": 0,
            "width": image.width,
            "height": image.height,
            "raw_width": image.width,
            "raw_height": image.height,
            "client_bounds": bounds,
            "scale_x": shot["scale_x"],
            "scale_y": shot["scale_y"],
            "backend": shot["backend"],
            "minimized": False,
        }
        self._last_window = None
        return self._last_screenshot

    def foreground_window(self) -> dict[str, Any]:
        """Describe the top-level window currently holding the foreground.

        The catch-all for a modal dialog that just appeared: instead of
        guessing its title, read the foreground root and either act on it
        directly or pass its hwnd to ``see``/``key``/``click``.
        """
        # Resolve the live foreground root, not the last anchored window: a
        # modal dialog is foreground and must win over a prior see() target.
        root = foreground_root_hwnd()
        if not root:
            raise HarnessError("No foreground window")
        hwnd, info = self._resolve_hwnd(str(root))
        return {"hwnd": hwnd, **info}

    def proofs(
        self,
        limit: int = 10,
        *,
        app: str | None = None,
        kind: str | None = None,
    ) -> list[dict[str, Any]]:
        """Return recent action proofs, newest first.

        Reads the append-only journal (``proofs.jsonl`` under the config dir,
        ``WINDOWS_HARNESS_HOME`` overrides the root), so a proof stays
        recoverable even when the producing call never printed
        ``result["proof"]["path"]`` — including from a later CLI invocation.
        Entries whose PNG was deleted are skipped. ``app`` matches process or
        title as a case-insensitive substring; ``kind`` matches exactly
        (``click``, ``drag``, ``scroll``, ``hover``, ``focus`` for
        click-to-type).
        """
        journal = delivery.proofs_journal()
        try:
            lines = journal.read_text(encoding="utf-8").splitlines()
        except OSError:
            return []
        needle = app.casefold() if app else None
        entries: list[dict[str, Any]] = []
        for line in reversed(lines):
            if len(entries) >= limit:
                break
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            path = entry.get("path")
            if not path or not Path(path).exists():
                continue
            if kind and entry.get("kind") != kind:
                continue
            if needle:
                info = entry.get("app") or {}
                haystack = " ".join(
                    str(info.get(key) or "") for key in ("process", "title")
                ).casefold()
                if needle not in haystack:
                    continue
            entries.append(entry)
        return entries

    def last_proof(self) -> dict[str, Any] | None:
        """The newest journal entry whose PNG still exists, or None."""
        recent = self.proofs(limit=1)
        return recent[0] if recent else None

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
        max_width: int = 1920,
        max_height: int = 1920,
        show_pointer: bool = True,
        bring_to_front: bool = False,
    ) -> dict[str, Any]:
        """Capture a bounded window image and draw the harness pointer onto it.

        ``bring_to_front=True`` fronts the target and keeps it fronted
        (restoring it if minimized) until :meth:`release`; the default is a
        quiet background grab that never activates or raises the window. The
        image is bounded by ``max_width`` x ``max_height`` (default 1920),
        downscaling only when the window is larger than that cap.
        """
        if max_width <= 0 or max_height <= 0:
            raise HarnessError("max_width and max_height must be positive")
        hwnd, info, shot = self._capture_shot(app, bring_to_front=bring_to_front)
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
        # Anchor the conversion to the client origin captured at screenshot
        # time (shot["client_bounds"] records x,y as the on-screen client
        # origin). Relying on a live ClientToScreen here re-reads the window's
        # current placement, which can be a transient off-screen value (e.g.
        # -32000,-32000 during an iconified/foreground handoff) and would make
        # a screenshot-space click fly off the virtual desktop. The frozen
        # bounds are the same geometry the screenshot was built from, so the
        # mapping stays consistent with the image the agent is looking at.
        bounds = shot["client_bounds"]
        # Refuse a stale anchor: if the window was moved/resized since the
        # screenshot, screenshot-space coordinates no longer reflect reality
        # (and the action-proof would lie). The -32000 iconified/foreground
        # handoff is the one transient we deliberately keep frozen through.
        try:
            life_state = anchor_health(
                shot["client_bounds"], client_bounds(shot["hwnd"])
            )
        except HarnessError:
            # Window vanished or hwnd invalid (e.g. unit tests): let the
            # action fail downstream instead of mis-reporting a stale anchor.
            life_state = "ok"
        if life_state in ("moved", "resized"):
            title = (self._last_window or {}).get("title") or str(shot["hwnd"])
            raise StaleWindowError(
                f"Window #{shot['hwnd']} '{title}' was {life_state} since the "
                f"last screenshot; its saved client anchor no longer matches. "
                f"Call win.see(...) to re-capture the screenshot and refresh "
                f"the anchor before coordinate actions."
            )
        screen_x = float(bounds["x"]) + x
        screen_y = float(bounds["y"]) + y
        return screen_x, screen_y

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
        annotate: bool = True,
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
        hwnd, _info = self._target_hwnd(app)
        point = self._screen_point(x, y, coordinate_space)
        self._pointer_position = point
        self._overlay.move(*point)
        result = inject.hover_screen(
            hwnd, point, delivery_mode=delivery, hold=hold, dwell=dwell
        )
        proof = self._annotate_proof(
            hwnd, point, kind="hover", label="hover",
            coordinate_space=coordinate_space, annotate=annotate,
        )
        if proof:
            result["proof"] = proof
        return result

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
        annotate: bool = True,
    ) -> dict[str, Any]:
        """Coordinate click; foreground (default) fronts and holds the target,
        background routes pen/message without disturbing the user."""
        hwnd, info = self._target_hwnd(app)
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
        proof = self._annotate_proof(
            hwnd, point, kind="click",
            label=_click_label(button, clicks),
            coordinate_space=coordinate_space, annotate=annotate,
        )
        if proof:
            result["proof"] = proof
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
        annotate: bool = True,
    ) -> dict[str, Any]:
        hwnd, _info = self._target_hwnd(app)
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
        proof = self._annotate_proof(
            hwnd, start, kind="drag", label="drag",
            coordinate_space=coordinate_space,
            end=end, annotate=annotate,
        )
        if proof:
            result["proof"] = proof
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
        annotate: bool = True,
    ) -> dict[str, Any]:
        if not delta_y and not delta_x:
            raise HarnessError("Provide a nonzero delta_y or delta_x")
        hwnd, _info = self._target_hwnd(app)
        if (x is None) != (y is None):
            raise HarnessError("Provide both x and y when targeting a scroll point")
        if x is not None and y is not None:
            point = self._screen_point(x, y, coordinate_space)
        else:
            bounds = client_bounds(hwnd)
            point = (bounds[0] + bounds[2] / 2.0, bounds[1] + bounds[3] / 2.0)
            # A transient client-bounds read during a foreground/cloak handoff
            # can land the "center" off the virtual desktop (large negative
            # coords); that would silently scroll nothing. Fall back to the
            # window the last screenshot anchored (same coordinate mapping as
            # _screen_point) instead of firing into the void.
            if not point_on_screen(*point) and self._last_screenshot is not None:
                point = self._screen_point(500.0, 500.0, "normalized")
                hwnd = self._last_screenshot["hwnd"]
        self._pointer_position = point
        self._overlay.move(*point)
        focus_before = current_foreground()
        result = inject.scroll(hwnd, point, delta_y, delta_x, delivery_mode=delivery, hold=hold)
        result["focus"] = self._focus_outcome(focus_before, "scroll", delivery=delivery, hold=hold)
        proof = self._annotate_proof(
            hwnd, point, kind="scroll",
            label=_scroll_label(delta_y, delta_x),
            coordinate_space=coordinate_space,
            delta={"delta_x": delta_x, "delta_y": delta_y},
            annotate=annotate,
        )
        if proof:
            result["proof"] = proof
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
        annotate: bool = True,
    ) -> dict[str, Any]:
        """Type text, optionally focusing a field at (x, y) first.

        Focus-driven fields (search boxes with suggestion popups) only accept
        input while focused; passing x/y clicks the field in the same call so
        the focus-then-type sequence cannot be split by a focus loss.

        After a foreground burst the field is read back over UIA and
        ``verified`` reflects that read-back, not just the transport:
        ``verified_via`` reports ``"value_pattern"`` (read-back confirmed),
        ``"clipboard"`` (confirmed after a paste retry), or ``"transport"``
        (no readable text state; delivery succeeded but is unconfirmed).
        """
        hwnd, _info = self._target_hwnd(app)
        if (x is None) != (y is None):
            raise HarnessError("Provide both x and y to focus a field before typing")
        focus_before = current_foreground()
        focus_point: tuple[float, float] | None = None
        if x is not None and y is not None:
            point = self._screen_point(x, y, coordinate_space)
            focus_point = point
            self._pointer_position = point
            self._overlay.move(*point)
            inject.click_screen(hwnd, point, button="left", clicks=1,
                                delivery_mode=delivery, hold=hold)
            time.sleep(0.12)  # let the field's focus handlers settle
        read_before: str | None = None
        if delivery == "foreground":
            # Snapshot the field's text so the post-type read-back can judge
            # by insertion delta, not just by tail position — typing into a
            # document that already has content (a restored Notepad tab) is
            # invisible to an end-of-text check.
            read_before, _ = self._read_back_text(hwnd)
        result = inject.type_text(hwnd, text, delivery_mode=delivery, hold=hold)
        if result.get("verified") and delivery == "foreground":
            # XAML/CEF editors accept the keystrokes but silently drop many of
            # them; confirm the text actually landed instead of trusting the
            # transport. On a dropped-field mismatch, retry through the
            # verified UIA ValuePattern path (set_value), then clipboard paste.
            read_back, element_index = self._read_back_text(hwnd)
            landed = _text_landed(read_back, text, before=read_before)
            if landed is None and read_back is not None:
                # The editor may still be settling (session restore, async
                # render); give it a short window to reach its final text
                # before judging the delivery.
                deadline = time.monotonic() + 0.75
                while time.monotonic() < deadline:
                    time.sleep(0.15)
                    read_back, element_index = self._read_back_text(hwnd)
                    landed = _text_landed(read_back, text, before=read_before)
                    if landed is not None:
                        break
            dropped = (
                landed is False
                and read_back is not None
                and len(_normalize_newlines(read_back)) < len(_normalize_newlines(text))
            )
            if dropped and element_index is not None:
                # The field holds less than the payload: an (almost) empty
                # field dropped the burst, so replacing its whole value is
                # safe. Never fire set_value against a document with prior
                # content — it would clobber the user's text.
                try:
                    self.ax.set_value(element_index, text)
                    retried_via = "set_value"
                except HarnessError:
                    inject.set_clipboard_text(text)
                    inject.paste_text(hwnd, delivery_mode="foreground", hold=hold)
                    retried_via = "paste"
                read_back, element_index = self._read_back_text(hwnd)
                landed = _text_landed(read_back, text, before=read_before)
                result["retried_via"] = retried_via
                result["verified"] = landed is True
                result["verified_via"] = "value_pattern" if retried_via == "set_value" else "clipboard"
                result["read_back"] = _normalize_newlines(read_back) if read_back else None
                hint = (
                    "foreground type did not appear in the field (XAML/CEF text "
                    "boxes drop injected unicode)"
                )
                if landed is True:
                    result["note"] = f"{hint}; retried via {retried_via} and it landed."
                else:
                    result["note"] = (
                        f"{hint}; retried via {retried_via} and it still did not "
                        "land. Use win.ax.set_value() directly."
                    )
            elif landed is True:
                result["verified_via"] = "value_pattern"
            elif read_back is None:
                # No readable text state (control exposes no value): the
                # transport's verdict stands, honestly marked as unconfirmed
                # by read-back.
                result["verified_via"] = "transport"
            else:
                # The read-back contradicts the transport: the payload is
                # neither at the tail, nor newly present, nor matched by the
                # insertion delta (a launch/session-restore race can eat the
                # burst while the window reports focus). Report it honestly.
                result["verified"] = False
                result["verified_via"] = "value_pattern"
                result["read_back"] = _normalize_newlines(read_back) if read_back else None
                result["note"] = (
                    "read-back does not confirm the typed text landed "
                    "(window accepted the keystrokes but the text did not "
                    "appear; possible focus/restore race). Click the field "
                    "and retry, or use win.paste()."
                )
        result["focus"] = self._focus_outcome(focus_before, "typing", delivery=delivery, hold=hold)
        if focus_point is not None:
            proof = self._annotate_proof(
                hwnd, focus_point, kind="focus", label="click to type",
                coordinate_space=coordinate_space, annotate=annotate,
            )
            if proof:
                result["proof"] = proof
        return result

    def key(self, key: str, *, app: str | None = None, delivery: str = "foreground", hold: bool = True) -> dict[str, Any]:
        hwnd, _info = self._resolve_hwnd(app)
        focus_before = current_foreground()
        result = inject.press_key(hwnd, key, delivery_mode=delivery, hold=hold)
        result["focus"] = self._focus_outcome(focus_before, f"key {key!r}", delivery=delivery, hold=hold)
        return result

    def _read_back_text(self, hwnd: int) -> tuple[str | None, int | None]:
        """Best-effort read-back of the window's editable text via UIA.

        XAML/WinUI text boxes and CEF editors drop ``KEYEVENTF_UNICODE``
        injection while still reporting ``verified: True``, so after a
        foreground :meth:`type` we confirm against what the window actually
        holds. Returns ``(value, element_index)`` -- the element index lets
        the caller retry through ``ax.set_value`` (the verified UIA path) when
        the keyboard route was dropped. ``(None, None)`` means the window
        exposes no readable *editable* value and the transport's verdict
        stands, honestly unverifiable.

        Hosts like Word put a WebView/ribbon in the UIA tree whose value is a
        URL, never the document text; those are skipped so the retry only ever
        fires against a control that could actually hold the typed text.
        """
        # Control classes whose Value is navigational (a URL / the browser
        # chrome), never the field the agent is typing into.
        _SKIP_VALUE_CLASSES = ("webview", "hubwebview", "contentswebview")
        _URL_PREFIXES = ("http://", "https://", "file://", "about:")
        # Ribbon/toolbar controls whose Value is formatting state, never the
        # text the agent is typing (Word's 字号/字体 boxes, search boxes, and
        # "Page N content" placeholders). Reading these and finding the typed
        # text in them triggers a spurious retry and a false "did not land".
        _SKIP_VALUE_NAMES = ("字号", "字体", "Microsoft 搜索", "搜索", "页面")
        try:
            for control_type in ("Document", "Edit"):
                controls = self.ax.query(
                    app=str(hwnd), control_type=control_type, limit=5
                )
                for control in controls:
                    index = control["element_index"]
                    class_name = (control.get("class_name") or "").casefold()
                    if any(skip in class_name for skip in _SKIP_VALUE_CLASSES):
                        continue
                    control_name = control.get("name") or ""
                    if any(skip in control_name for skip in _SKIP_VALUE_NAMES):
                        continue
                    try:
                        value = self.ax.get(index, "Value")
                    except HarnessError:
                        continue
                    if isinstance(value, str) and value:
                        # Skip navigational chrome even when the class hints
                        # nothing (Word's task-pane WebView has class None but
                        # its Value is a https URL / a registry key).
                        if value.casefold().startswith(_URL_PREFIXES):
                            continue
                        return value, index
        except HarnessError:
            pass
        return None, None

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
            differ = sum(a != b for a, b in zip_strict(before, after))
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
