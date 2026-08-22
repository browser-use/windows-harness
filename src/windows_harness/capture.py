"""Window discovery and background capture through public Win32 APIs.

Enumeration mirrors ``CGWindowListCopyWindowInfo``; capture prefers
``PrintWindow`` with ``PW_RENDERFULLCONTENT`` (works while the window is
occluded or in the background) and falls back to a screen-region ``BitBlt``
when an accelerated window renders black and is actually visible.
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import os
import struct
import threading
from pathlib import Path

from PIL import Image

user32 = ctypes.WinDLL("user32", use_last_error=True)
gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
dwmapi = ctypes.WinDLL("dwmapi")

DWMWA_CLOAK = 13
DWMWA_EXTENDED_FRAME_BOUNDS = 9
PW_RENDERFULLCONTENT = 0x2
CWP_SKIPINVISIBLE = 0x1
CWP_SKIPTRANSPARENT = 0x2
CWP_SKIPDISABLED = 0x8
GWL_EXSTYLE = -20
WS_EX_TOOLWINDOW = 0x80
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_ENUM_WINDOW_PROC = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)


class HarnessError(RuntimeError):
    """Base error for Windows harness failures."""


class AccessibilityPermissionError(HarnessError):
    """UI Automation is unavailable on this desktop."""


def _err(operation: str) -> OSError:
    code = ctypes.get_last_error()
    detail = ctypes.FormatError(code)
    if code == 0:
        # Some calls fail without setting last-error (PrintWindow on hidden
        # webview windows); echoing "operation completed successfully" as the
        # reason would send the agent debugging in the wrong direction.
        detail = "no diagnostic returned"
    return HarnessError(f"{operation} failed: {detail}")


def _capture_refusal_reason(hwnd: int, cause: Exception) -> str:
    """Why a non-visible window cannot be captured right now, and what to do."""
    if user32.IsIconic(hwnd):
        state = "minimized"
    elif is_cloaked(hwnd):
        state = "cloaked"
    elif not user32.IsWindowVisible(hwnd):
        state = "hidden"  # e.g. a tray app whose window was dismissed
    else:
        state = "on screen but not rendering into the capture"
    return (
        f"background capture unavailable: window {hwnd:#x} is {state} and "
        f"produced no pixels ({cause}). Restore/show the window once and "
        "capture again, or read its structure through win.ax instead."
    )


def ensure_dpi_awareness() -> None:
    """Opt into physical-pixel coordinates so screenshots match input points."""
    try:
        context = wt.DPI_AWARENESS_CONTEXT(-4)  # PER_MONITOR_AWARE_V2
        if user32.SetProcessDpiAwarenessContext(context):
            return
    except AttributeError:
        pass
    try:
        shcore = ctypes.WinDLL("shcore")
        shcore.SetProcessDpiAwareness(2)
        return
    except (OSError, AttributeError):
        pass
    user32.SetProcessDPIAware()


def is_interactive_desktop() -> bool:
    """False inside Session 0 services or a locked workstation."""
    desktop = user32.OpenInputDesktop(0, False, 0x0001)  # DESKTOP_READOBJECTS
    if not desktop:
        return False
    user32.CloseDesktop(desktop)
    return True


def process_image_name(pid: int) -> str | None:
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return None
    try:
        size = wt.DWORD(1024)
        buffer = ctypes.create_unicode_buffer(size.value)
        if kernel32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
            return buffer.value
        return None
    finally:
        kernel32.CloseHandle(handle)


def _window_pid(hwnd: int) -> int:
    pid = wt.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return pid.value


def _dword_attribute(hwnd: int, attribute: int) -> int | None:
    value = wt.DWORD()
    result = dwmapi.DwmGetWindowAttribute(
        wt.HWND(hwnd), wt.DWORD(attribute), ctypes.byref(value), ctypes.sizeof(value)
    )
    return value.value if result == 0 else None


def is_cloaked(hwnd: int) -> bool:
    return bool(_dword_attribute(hwnd, DWMWA_CLOAK))


def extended_bounds(hwnd: int) -> tuple[int, int, int, int] | None:
    """Visible frame bounds excluding the invisible DWM drop shadow."""
    rect = wt.RECT()
    result = dwmapi.DwmGetWindowAttribute(
        wt.HWND(hwnd),
        wt.DWORD(DWMWA_EXTENDED_FRAME_BOUNDS),
        ctypes.byref(rect),
        ctypes.sizeof(rect),
    )
    if result != 0 or rect.right <= rect.left or rect.bottom <= rect.top:
        return None
    return rect.left, rect.top, rect.right, rect.bottom


def client_bounds(hwnd: int) -> tuple[int, int, int, int]:
    """Client-area origin on screen plus width/height in physical pixels."""
    rect = wt.RECT()
    if not user32.GetClientRect(hwnd, ctypes.byref(rect)):
        raise _err("GetClientRect")
    point = wt.POINT(rect.left, rect.top)
    user32.ClientToScreen(hwnd, ctypes.byref(point))
    width = rect.right - rect.left
    height = rect.bottom - rect.top
    return point.x, point.y, max(1, width), max(1, height)


def enumerate_windows() -> list[dict]:
    """All top-level windows with owner process metadata."""
    results: list[dict] = []
    image_names: dict[int, str] = {}  # many windows share one pid

    @_ENUM_WINDOW_PROC
    def on_window(hwnd: int, _lparam: int) -> bool:
        ex_style = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
        title_length = user32.GetWindowTextLengthW(hwnd)
        title_buffer = ctypes.create_unicode_buffer(title_length + 1)
        user32.GetWindowTextW(hwnd, title_buffer, title_length + 1)
        class_buffer = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(hwnd, class_buffer, 256)
        pid = _window_pid(hwnd)
        name = image_names.get(pid)
        if name is None:
            name = os.path.basename(process_image_name(pid) or "")
            image_names[pid] = name
        results.append(
            {
                "hwnd": hwnd,
                "pid": pid,
                "process": name,
                "title": title_buffer.value,
                "class_name": class_buffer.value,
                "bounds": extended_bounds(hwnd),
                "visible": bool(user32.IsWindowVisible(hwnd)),
                "cloaked": is_cloaked(hwnd),
                "minimized": bool(user32.IsIconic(hwnd)),
                "tool_window": bool(ex_style & WS_EX_TOOLWINDOW),
            }
        )
        return True

    user32.EnumWindows(on_window, 0)
    return results


def windows_for_process(pid: int) -> list[dict]:
    """Top-level windows owned by one PID, biggest visible first — the
    counterpart of sorting CGWindowList output by area."""
    windows = [
        window
        for window in enumerate_windows()
        if window["pid"] == pid
        and window["bounds"] is not None
        and not window["tool_window"]
    ]
    for window in windows:
        left, top, right, bottom = window["bounds"]
        window["area"] = (right - left) * (bottom - top)

    def order(window: dict) -> tuple[object, ...]:
        shown = window["visible"] and not window["cloaked"] and not window["minimized"]
        rank = 0 if shown else 1
        return (rank, -int(window["area"]), -len(window["title"]))

    return sorted(windows, key=order)


def _no_match_error(query: str, windows: list[dict]) -> str:
    """No-match error that names the closest real targets, so the agent can
    retry with a correct name instead of burning a round trip on discovery."""
    import difflib

    labels: dict[str, str] = {}
    for window in windows:
        for label in {window["title"], window["process"]}:
            if label:
                labels.setdefault(label.casefold(), label)
    close = difflib.get_close_matches(
        str(query).casefold(), list(labels), n=3, cutoff=0.5
    )
    hints = ", ".join(labels[key] for key in close)
    suffix = f" — closest: {hints}" if hints else ""
    return f"No matching window for {query!r}{suffix}"


def resolve_hwnd(query: str) -> tuple[int, dict]:
    """Resolve a PID, exe name, path fragment, or window title to one window.

    Raises :class:`HarnessError` on no matches or ambiguity, mirroring the
    macOS harness's ``_resolve_app`` behaviour.
    """
    needle = str(query).casefold()
    all_windows = enumerate_windows()
    exact: list[dict] = []
    candidates: list[dict] = []
    for window in all_windows:
        lowered = [
            str(window["hwnd"]),
            str(window["pid"]).casefold(),
            window["process"].casefold(),
            window["title"].casefold(),
        ]
        if needle in lowered:
            exact.append(window)
        elif any(needle and needle in value for value in lowered):
            candidates.append(window)

    matches = [window for window in (exact or candidates) if window["bounds"]]
    # Drop hook/IME junk that merely mentions the query in its title, and any
    # window too small to interact with (macOS filters < 40 px as well).
    matches = [
        window
        for window in matches
        if window["bounds"][2] - window["bounds"][0] >= 40
        and window["bounds"][3] - window["bounds"][1] >= 40
    ]
    if not matches:
        raise HarnessError(_no_match_error(query, all_windows))

    def area(window: dict) -> int:
        left, top, right, bottom = window["bounds"]
        return (right - left) * (bottom - top)

    # Never prefer a minimized window over a shown one: iconic windows report
    # IsWindowVisible() == True but their bounds are the parked (-32000) rect.
    best = min(
        matches,
        key=lambda window: (
            window["minimized"],
            not (window["visible"] and not window["cloaked"]),
            -area(window),
        ),
    )
    return best["hwnd"], best


# System-internal windows that show up in every enumeration and mean nothing
# to an agent. Filtered from the default inventory; `include_system` bypasses.
_SYSTEM_WINDOW_CLASSES = {
    "Default IME", "MSCTFIME UI", "IME", "GDI+ Window",
    "BroadcastListenerWindow", "MSITProSignOff", "MSUIHTML",
    "Windows.Input.App.Bar", "ApplicationFrame Input Window",
}


def list_processes(*, include_system: bool = False) -> list[dict]:
    """One entry per process that owns a top-level window.

    By default system plumbing (IME helpers, tool windows, untitled frames)
    is filtered out — a full enumeration runs to hundreds of lines that
    overflow agent output limits and bury the real apps. Pass
    ``include_system=True`` for the raw inventory.
    """
    processes: dict[int, dict] = {}
    for window in enumerate_windows():
        if not include_system and (
            window["tool_window"]
            or not window["title"].strip()
            or window["class_name"] in _SYSTEM_WINDOW_CLASSES
        ):
            continue
        entry = processes.setdefault(
            window["pid"],
            {"pid": window["pid"], "name": window["process"] or "<unknown>", "windows": []},
        )
        entry["windows"].append(window["title"])
    return sorted(processes.values(), key=lambda item: item["name"].casefold())


def _screen_capture_region(left: int, top: int, width: int, height: int) -> Image.Image:
    screen_dc = user32.GetDC(None)
    memory_dc = gdi32.CreateCompatibleDC(screen_dc)
    bitmap = gdi32.CreateCompatibleBitmap(screen_dc, width, height)
    previous = gdi32.SelectObject(memory_dc, bitmap)
    try:
        if not gdi32.BitBlt(memory_dc, 0, 0, width, height, screen_dc, left, top, 0x00CC0020):
            raise _err("BitBlt")
        return _bitmap_to_image(memory_dc, bitmap, width, height)
    finally:
        gdi32.SelectObject(memory_dc, previous)
        gdi32.DeleteObject(bitmap)
        gdi32.DeleteDC(memory_dc)
        user32.ReleaseDC(None, screen_dc)


# A capture thread abandoned mid-PrintWindow holds its DC/bitmap pair until
# the hung window finally answers — potentially forever. GDI objects are capped
# at 10k per process, so bound how many may be outstanding at once.
_HUNG_CAPTURE_THREADS: list[threading.Thread] = []
_MAX_HUNG_CAPTURES = 2


def _print_window_capture(hwnd: int, width: int, height: int, *, timeout: float = 4.0) -> Image.Image:
    """PrintWindow on a daemon thread: a hung window must not hang the agent.

    On timeout or failure the caller falls back to BitBlt when the window is
    on screen; the abandoned thread only leaks one DC pair until it returns.
    At most :data:`_MAX_HUNG_CAPTURES` threads may be abandoned at once — past
    that the capture refuses rather than bleeding GDI handles.
    """
    _HUNG_CAPTURE_THREADS[:] = [t for t in _HUNG_CAPTURE_THREADS if t.is_alive()]
    if len(_HUNG_CAPTURE_THREADS) >= _MAX_HUNG_CAPTURES:
        raise HarnessError(
            f"{len(_HUNG_CAPTURE_THREADS)} earlier captures of unresponsive "
            "windows are still blocked; refusing to tie up more GDI handles"
        )
    outcome: dict[str, object] = {}

    def work() -> None:
        try:
            outcome["image"] = _print_window_capture_blocking(hwnd, width, height)
        except Exception as exc:  # noqa: BLE001 - surfaced by the caller
            outcome["error"] = exc

    thread = threading.Thread(target=work, daemon=True)
    thread.start()
    thread.join(timeout)
    if thread.is_alive():
        _HUNG_CAPTURE_THREADS.append(thread)
        raise HarnessError(
            f"PrintWindow did not answer within {timeout:g}s (window not responding)"
        )
    if "error" in outcome:
        raise outcome["error"]  # type: ignore[misc]
    return outcome["image"]  # type: ignore[return-value]


def _print_window_capture_blocking(hwnd: int, width: int, height: int) -> Image.Image:
    window_dc = user32.GetWindowDC(hwnd)
    memory_dc = gdi32.CreateCompatibleDC(window_dc)
    bitmap = gdi32.CreateCompatibleBitmap(window_dc, width, height)
    previous = gdi32.SelectObject(memory_dc, bitmap)
    try:
        result = user32.PrintWindow(
            wt.HWND(hwnd), memory_dc, PW_RENDERFULLCONTENT
        )
        if not result:
            raise _err("PrintWindow")
        return _bitmap_to_image(memory_dc, bitmap, width, height)
    finally:
        gdi32.SelectObject(memory_dc, previous)
        gdi32.DeleteObject(bitmap)
        gdi32.DeleteDC(memory_dc)
        user32.ReleaseDC(hwnd, window_dc)


def _bitmap_to_image(dc: int, bitmap: int, width: int, height: int) -> Image.Image:
    header = struct.pack(
        "<IiiHHIIiiII",
        40,
        width,
        -height,  # top-down rows so PIL does not need a vertical flip
        1,
        32,
        0,
        0,
        0,
        0,
        0,
        0,
    )
    buffer = ctypes.create_string_buffer(width * height * 4)
    rows = gdi32.GetDIBits(dc, bitmap, 0, height, buffer, header, 0)
    if rows != height:
        raise _err("GetDIBits")
    channels = Image.frombytes("RGBA", (width, height), buffer.raw, "raw", "BGRA")
    # PrintWindow leaves alpha undefined for opaque content; flatten onto white.
    background = Image.new("RGBA", channels.size, (255, 255, 255, 255))
    return Image.alpha_composite(background, channels).convert("RGB")


def capture_window(hwnd: int) -> dict:
    """Capture one window's client area without raising it.

    Returns metadata compatible with the macOS harness screenshot shape:
    physical pixels, client bounds, and the scale from window to pixels.
    """
    if not user32.IsWindow(hwnd):
        raise HarnessError(f"Window {hwnd:#x} no longer exists")
    x, y, width, height = client_bounds(hwnd)
    visible_on_screen = (
        user32.IsWindowVisible(hwnd)
        and not user32.IsIconic(hwnd)
        and not is_cloaked(hwnd)
    )

    fallback_used = False
    try:
        image = _print_window_capture(hwnd, width, height)
    except HarnessError as exc:
        if not visible_on_screen:
            raise HarnessError(_capture_refusal_reason(hwnd, exc)) from exc
        image = _screen_capture_region(x, y, width, height)
        fallback_used = True
    else:
        if _looks_black(image) and visible_on_screen:
            image = _screen_capture_region(x, y, width, height)
            fallback_used = True

    return {
        "image": image,
        "hwnd": hwnd,
        "client_bounds": {"x": x, "y": y, "width": width, "height": height},
        "scale_x": image.width / float(width),
        "scale_y": image.height / float(height),
        "backend": "bitblt" if fallback_used else "printwindow",
        "minimized": bool(user32.IsIconic(hwnd)),
    }


def _looks_black(image: Image.Image) -> bool:
    grey = image.convert("L")
    histogram = grey.histogram()
    dark_pixels = sum(histogram[:16])
    return dark_pixels / float(grey.width * grey.height) > 0.995


def png_size(path: Path) -> tuple[int, int]:
    with path.open("rb") as handle:
        header = handle.read(24)
    if len(header) < 24 or header[:8] != b"\x89PNG\r\n\x1a\n":
        raise HarnessError(f"Screenshot is not a PNG: {path}")
    return struct.unpack(">II", header[16:24])
