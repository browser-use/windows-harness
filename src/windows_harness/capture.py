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


_TH32CS_SNAPPROCESS = 0x00000002


class _PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [
        ("dwSize", wt.DWORD),
        ("cntUsage", wt.DWORD),
        ("th32ProcessID", wt.DWORD),
        ("th32DefaultHeapID", ctypes.c_void_p),
        ("th32ModuleID", wt.DWORD),
        ("cntThreads", wt.DWORD),
        ("th32ParentProcessID", wt.DWORD),
        ("pcPriClassBase", wt.LONG),
        ("dwFlags", wt.DWORD),
        ("szExeFile", wt.WCHAR * 260),
    ]


# Typed Win32 Toolhelp signatures so 64-bit handles are not truncated by the
# default c_int return of an untyped WinDLL call.
kernel32.CreateToolhelp32Snapshot.restype = ctypes.c_void_p
kernel32.CreateToolhelp32Snapshot.argtypes = [wt.DWORD, wt.DWORD]
kernel32.Process32FirstW.argtypes = [ctypes.c_void_p, ctypes.POINTER(_PROCESSENTRY32W)]
kernel32.Process32FirstW.restype = wt.BOOL
kernel32.Process32NextW.argtypes = [ctypes.c_void_p, ctypes.POINTER(_PROCESSENTRY32W)]
kernel32.Process32NextW.restype = wt.BOOL
kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
kernel32.CloseHandle.restype = wt.BOOL


def _process_tree() -> dict[int, int]:
    """Map every live process id to its parent process id (Win32 Toolhelp)."""
    snapshot = kernel32.CreateToolhelp32Snapshot(_TH32CS_SNAPPROCESS, 0)
    if not snapshot or snapshot == ctypes.c_void_p(-1).value:
        return {}
    entry = _PROCESSENTRY32W()
    entry.dwSize = ctypes.sizeof(entry)
    tree: dict[int, int] = {}
    try:
        if kernel32.Process32FirstW(snapshot, ctypes.byref(entry)):
            while True:
                tree[int(entry.th32ProcessID)] = int(entry.th32ParentProcessID)
                if not kernel32.Process32NextW(snapshot, ctypes.byref(entry)):
                    break
    finally:
        kernel32.CloseHandle(snapshot)
    return tree


def _is_child_of_windowed_process(
    pid: int, *, window_pids: set[int], tree: dict[int, int]
) -> bool:
    """True when ``pid`` descends from a process that owns a ``window_pids`` window.

    An embedded web renderer (WebView2/CEF/Electron/QtWebEngine) is always a
    child process of the host app, so this separates a renderer's window from
    the app's own window without hardcoding any process or technology name.
    """
    cur = tree.get(pid)
    seen: set[int] = set()
    while cur is not None and cur != 0 and cur not in seen:
        if cur in window_pids:
            return True
        seen.add(cur)
        cur = tree.get(cur)
    return False


def resolve_hwnd(query: str) -> tuple[int, dict]:
    """Resolve a PID, exe name, path fragment, or window title to one window.

    Matching is tiered so a loose title substring can never outrank an exact
    process name: a VS Code window whose title merely mentions "Discord" must
    not win the query "Discord" over Discord.exe itself.

    Raises :class:`HarnessError` on no matches or ambiguity, mirroring the
    macOS harness's ``_resolve_app`` behaviour.
    """
    needle = str(query).casefold()
    all_windows = enumerate_windows()
    # Best tier first: hwnd/pid exact, process exact (with or without the
    # .exe suffix), title exact, process substring, then any substring.
    tiers: list[list[dict]] = [[], [], [], [], []]
    for window in all_windows:
        process = window["process"].casefold()
        title = window["title"].casefold()
        stem = process[:-4] if process.endswith(".exe") else process
        if needle in (str(window["hwnd"]), str(window["pid"]).casefold()):
            tiers[0].append(window)
        elif needle == process or needle == stem:
            tiers[1].append(window)
        elif needle == title:
            tiers[2].append(window)
        elif needle and needle in process:
            tiers[3].append(window)
        elif needle and needle in (
            title, str(window["hwnd"]), str(window["pid"]).casefold()
        ):
            tiers[4].append(window)

    # For title/fuzzy tiers an embedded web renderer (WebView2/CEF/Electron/
    # QtWebEngine) often owns a window that shares the host app's title. Such a
    # renderer is always a *child* process of the host, so detect it by process
    # ancestry instead of hardcoding a process name: any window whose process
    # descends from another process that also owns a query-matching window is
    # the embedded renderer, not the app.
    query_windows = [window for tier in tiers for window in tier]
    process_tree = _process_tree()
    query_window_pids = {window["pid"] for window in query_windows}
    child_pids = {
        window["pid"]
        for window in query_windows
        if _is_child_of_windowed_process(
            window["pid"], window_pids=query_window_pids, tree=process_tree
        )
    }

    def area(window: dict) -> int:
        left, top, right, bottom = window["bounds"]
        return (right - left) * (bottom - top)

    def on_screen_rank(window: dict) -> int:
        if window["visible"] and not window["cloaked"] and not window["minimized"]:
            return 0
        if window["visible"] and not window["cloaked"]:
            return 1
        if window["cloaked"]:
            return 2
        return 3

    def pick(matches: list[dict]) -> dict:
        return min(
            matches,
            key=lambda window: (
                not window["title"],
                on_screen_rank(window),
                window["minimized"],
                -area(window),
            ),
        )

    best_renderer: dict | None = None
    for tier_index, tier in enumerate(tiers):
        matches = [window for window in tier if window["bounds"]]
        # Drop hook/IME junk that merely mentions the query in its title, and
        # any window too small to interact with (macOS filters < 40 px too).
        # Minimized windows are exempt: their bounds are the parked iconic
        # rect (e.g. 256x35), not their real size, and the harness restores
        # them on demand.
        matches = [
            window
            for window in matches
            if window["minimized"]
            or (
                window["bounds"][2] - window["bounds"][0] >= 40
                and window["bounds"][3] - window["bounds"][1] >= 40
            )
        ]
        if not matches:
            continue

        # Never prefer an untitled window: helper hosts (crashpad watchers,
        # DDE servers) are top-level and visible but are never the
        # interaction target. Among titled windows, prefer the one that is
        # actually on the desktop, then a visible-but-parked (minimized) main
        # window, and only then any clocked/hidden helper. A hidden helper can
        # be big and non-minimized while the real main window is parked as a
        # minimized taskbar entry (IsWindowVisible() True, bounds at the
        # -32000 rect), so "non-minimized" must not outrank "visible".
        # Apply the same preference against embedded web renderers: exclude
        # child (renderer) windows from title/fuzzy tiers so a real app window
        # wins, and only fall back to the renderer when no app window matched
        # in any tier. An explicit hwnd/pid (tier 0) or exact process name
        # (tier 1) is honoured as-is: naming a renderer returns that renderer.
        de_priority = tier_index >= 2
        primary = (
            [w for w in matches if w["pid"] not in child_pids]
            if de_priority
            else matches
        )
        if primary:
            best = pick(primary)
            return best["hwnd"], best
        # This tier matched only renderer windows: remember the best of them
        # as a fallback, but keep scanning lower tiers for an app window.
        candidate = pick(matches)
        if best_renderer is None or area(candidate) > area(best_renderer):
            best_renderer = candidate

    if best_renderer is not None:
        return best_renderer["hwnd"], best_renderer

    raise HarnessError(_no_match_error(query, all_windows))


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
        if _looks_blank(image) and visible_on_screen:
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


def _looks_blank(image: Image.Image) -> bool:
    """True when the frame is a solid wash (one dominant luma value).

    A composited/DWM window that PrintWindow cannot read often succeeds without
    error and paints its own DC background instead of raising -- pure white
    (Tencent Video), pure black (WebView2), or a flat dark grey such as the
    Electron/Chromium un-painted background (a near-solid ~18 luma frame). Any
    of those is "no content", so it must trigger the screen-region fallback;
    otherwise a blank screenshot is returned as a "successful" capture. A real
    frame spreads across many luma levels, so a single-bin dominance is a safe
    blank signal and will not misfire on genuine dark content.
    """
    grey = image.convert("L")
    histogram = grey.histogram()
    total = float(grey.width * grey.height)
    return max(histogram) / total > 0.98


def png_size(path: Path) -> tuple[int, int]:
    with path.open("rb") as handle:
        header = handle.read(24)
    if len(header) < 24 or header[:8] != b"\x89PNG\r\n\x1a\n":
        raise HarnessError(f"Screenshot is not a PNG: {path}")
    return struct.unpack(">II", header[16:24])
