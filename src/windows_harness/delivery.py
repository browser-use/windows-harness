"""Background-delivery policy: the silent-drop matrix and UIPI preflight.

Ported from trycua/cua `platform-windows/src/input/delivery.rs` and
`input/mod.rs` (MIT, Copyright 2025 Cua AI, Inc.).

Windows has no per-PID event delivery like macOS ``CGEventPostToPid``. Two
background transports exist — PostMessage and synthetic-pointer injection —
and some frameworks silently drop one or both while reporting success. The
matrix below encodes which ``(framework class, event kind)`` pairs are known
to drop, each entry backed by end-to-end observation. When background is
impossible we raise :class:`BackgroundUnavailable` instead of pretending,
so the agent can opt into foreground delivery explicitly.
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import json
import os
from pathlib import Path

from .capture import HarnessError, process_image_name, user32

advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

# Event families used by the matrix.
MOUSE_CLICK = "mouse_click"
MOUSE_MOVE = "mouse_move"
MOUSE_SCROLL = "mouse_scroll"
KEYSTROKE = "keystroke"
KEY_COMBO = "key_combo"
TEXT_INPUT = "text_input"

_ALL_EVENTS = (MOUSE_CLICK, MOUSE_MOVE, MOUSE_SCROLL, KEYSTROKE, KEY_COMBO, TEXT_INPUT)


class BackgroundUnavailable(HarnessError):
    """Background delivery cannot reach this target; escalate to foreground."""

    def __init__(self, code: str, target_class: str, kind: str, cause: str) -> None:
        super().__init__(
            f"Background delivery unavailable for '{target_class}' ({kind}): {cause} "
            'Retry with delivery="foreground".'
        )
        self.code = code  # background_unavailable | background_occluded | background_uipi_blocked
        self.target_class = target_class
        self.kind = kind
        self.cause = cause
        self.escalation = "foreground"


def read_class_name(hwnd: int) -> str:
    if not hwnd:
        return "<unknown>"
    buffer = ctypes.create_unicode_buffer(256)
    length = user32.GetClassNameW(wt.HWND(hwnd), buffer, 256)
    return buffer.value if length > 0 else "<unknown>"


def _starts_with_any(class_name: str, prefixes: tuple[str, ...]) -> bool:
    return any(class_name.startswith(prefix) for prefix in prefixes)


# --- framework detectors (delivery.rs L229-L307, mouse.rs L336-L361) ------

def is_chromium_target_window(hwnd: int) -> bool:
    """Chromium/Electron/CEF frames. Their input thread only accepts events
    with SendInput-queue origin; posted messages vanish."""
    return _starts_with_any(
        read_class_name(hwnd), ("Chrome_WidgetWin_", "CefBrowser")
    )


_ENUM_CHILD_PROC = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)


def has_chromium_descendant(hwnd: int) -> bool:
    """Tauri/WinForms hosts embedding a WebView2 renderer child."""
    if not hwnd:
        return False
    found = []

    @_ENUM_CHILD_PROC
    def on_child(child: int, _lparam: int) -> bool:
        if _starts_with_any(
            read_class_name(child), ("Chrome_WidgetWin_", "CefBrowser")
        ):
            found.append(child)
            return False
        return True

    user32.EnumChildWindows(wt.HWND(hwnd), on_child, 0)
    return bool(found)


def is_wpf_target_window(hwnd: int) -> bool:
    """WPF hosts its tree in HwndWrapper[...]; its InputManager ignores posted
    pointer messages unless the live cursor is over the window."""
    return read_class_name(hwnd).startswith("HwndWrapper")


def is_tk_target_window(hwnd: int) -> bool:
    """Tk's event loop does not treat posted key/text messages as genuine
    input for the focused widget (observed first-hand in this project)."""
    name = read_class_name(hwnd)
    return name == "TkTopLevel" or name.startswith("TkTopLevel.")


def is_gtk_target_window(hwnd: int) -> bool:
    """GTK3 gdkWindowToplevel / GTK4 gdkSurfaceToplevel; button widgets
    ignore posted clicks, canvas areas accept them — indistinguishable at
    HWND level, so flag clicks broadly."""
    return _starts_with_any(read_class_name(hwnd), ("gdkWindow", "gdkSurface"))


def is_vcl_target_window(hwnd: int) -> bool:
    """LibreOffice/OpenOffice (SAL* classes). Accelerators route through
    TranslateAccelerator reading GetKeyState, which PostMessage never updates."""
    return read_class_name(hwnd).startswith("SAL")


_XAML_HOST_CLASSES = {
    "ApplicationFrameWindow",
    "WinUIDesktopWin32WindowClass",
    "Windows.UI.Core.CoreWindow",
    "Microsoft.UI.Content.DesktopChildSiteBridge",
}
_XAML_HOST_EXES = {
    "notepad.exe",           # Win 11 modern Notepad (UWP-packaged)
    "calculatorapp.exe",     # UWP Calculator
    "calc.exe",              # some Win 11 builds expose the stub directly
    "applicationframehost.exe",
    "photos.exe",
    "systemsettings.exe",
}


def is_xaml_host_window(hwnd: int) -> bool:
    """XAML island hosts ignore posted keys and text entirely; UIA patterns
    are the only honest background route for typing there (cua CUA-543)."""
    if read_class_name(hwnd) in _XAML_HOST_CLASSES:
        return True
    pid = wt.DWORD()
    if not user32.GetWindowThreadProcessId(wt.HWND(hwnd), ctypes.byref(pid)):
        return False
    name = process_image_name(pid.value) or ""
    return os.path.basename(name).casefold() in _XAML_HOST_EXES


# --- observed drops: the matrix learns from first-hand evidence -------------
#
# The static matrix below encodes cua's cross-machine observations. Individual
# machines differ (hook software, OEM input stacks), so an agent that PROVED a
# drop — e.g. a `verified: false` action followed by a negative verify_change —
# records it once; future background calls refuse instead of repeating a dead
# transport. Stored as {"<window class>": ["<event kind>", ...]}.


def config_dir() -> Path:
    override = os.environ.get("WINDOWS_HARNESS_HOME")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".windows-harness"


def scripts_dir() -> Path:
    """Conventional home for agent-written task scripts (windows-harness run
    resolves bare filenames here); keeps generated .py files out of the
    caller's working directory."""
    return config_dir() / "scripts"


def proofs_journal() -> Path:
    """Append-only JSONL index of action proofs (one line per proof), so a
    proof path stays recoverable even when the producing call never printed
    it. Lives under the config dir, not %TEMP%, so a temp cleanup cannot take
    the index down with the images."""
    return config_dir() / "proofs.jsonl"


_OBSERVED_CACHE: tuple[float, dict[str, list[str]]] | None = None


def observed_drops() -> dict[str, list[str]]:
    """Locally recorded (class, kinds) pairs that drop background input."""
    global _OBSERVED_CACHE
    path = config_dir() / "drops.json"
    try:
        mtime = path.stat().st_mtime
    except OSError:
        _OBSERVED_CACHE = None
        return {}
    if _OBSERVED_CACHE is not None and _OBSERVED_CACHE[0] == mtime:
        return _OBSERVED_CACHE[1]
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    _OBSERVED_CACHE = (mtime, data)
    return data


def note_observed_drop(hwnd: int, kind: str) -> dict[str, list[str]]:
    """Record first-hand evidence that this window's class drops `kind`."""
    if kind not in _ALL_EVENTS:
        raise HarnessError(
            f"Unknown event kind {kind!r}; expected one of {sorted(_ALL_EVENTS)}"
        )
    data = dict(observed_drops())
    class_name = read_class_name(hwnd)
    kinds = list(data.get(class_name, []))
    if kind not in kinds:
        kinds.append(kind)
    data[class_name] = sorted(kinds)
    path = config_dir() / "drops.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    global _OBSERVED_CACHE
    _OBSERVED_CACHE = None
    return data


# --- the silent-drop matrix (delivery.rs would_be_silently_dropped) --------


def would_be_silently_dropped(hwnd: int, kind: str) -> bool:
    if kind in observed_drops().get(read_class_name(hwnd), ()):  # local evidence first
        return True
    if is_chromium_target_window(hwnd):
        return True
    if has_chromium_descendant(hwnd):
        # WebView2 hosts keep usable UIA/click routes, but drag/wheel/chords
        # still need the renderer's system input queue.
        return kind in (MOUSE_MOVE, MOUSE_SCROLL, KEY_COMBO)
    if is_wpf_target_window(hwnd):
        # Pointer events always dropped; keys dropped while another window
        # owns the foreground. WPF also ignores posted wheel messages (cua
        # routes scrolls through WM_*SCROLL/UIA instead — neither maps onto a
        # wheel-delta API, so refuse honestly). Background drags are refused
        # separately at dispatch (Wisp only processes input while foreground).
        pointer = kind in (MOUSE_CLICK, MOUSE_MOVE, MOUSE_SCROLL)
        keys = kind in (KEYSTROKE, KEY_COMBO) and not _target_is_foreground(hwnd)
        return pointer or keys
    if is_tk_target_window(hwnd):
        return kind in (KEYSTROKE, KEY_COMBO, TEXT_INPUT)
    if is_xaml_host_window(hwnd):
        # XAML islands ignore posted keys/text entirely (CUA-543); typing there
        # must route through UIA patterns (win.ax.set_value).
        return kind in (KEYSTROKE, KEY_COMBO, TEXT_INPUT)
    if is_winui3_target_window(hwnd):
        # Deliberately NOT flagged: pen injection click-activates WinUI3
        # frames 8/8 of the time, so background there means UIA patterns only.
        return False
    if is_gtk_target_window(hwnd):
        return kind == MOUSE_CLICK
    if is_vcl_target_window(hwnd):
        # Plain WM_CHAR text into document widgets works end-to-end.
        return kind in (KEYSTROKE, KEY_COMBO)
    return False


def is_winui3_target_window(hwnd: int) -> bool:
    return read_class_name(hwnd) == "WinUIDesktopWin32WindowClass"


def _target_is_foreground(hwnd: int) -> bool:
    GA_ROOT = 2
    root = user32.GetAncestor(wt.HWND(hwnd), GA_ROOT) or hwnd
    fg = user32.GetForegroundWindow() or 0
    fg_root = user32.GetAncestor(wt.HWND(fg), GA_ROOT) or fg
    return bool(root) and root == fg_root


# --- UIPI preflight (mod.rs post_message_blocked_by_uipi) ------------------
#
# PostMessage to a higher-integrity window returns TRUE but the target's pump
# filters the message out. The only honest move is checking integrity levels
# up front.

_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_TOKEN_QUERY = 0x0008
_TokenIntegrityLevel = 25

_INTEGRITY_NAMES = {0x1000: "Low", 0x2000: "Medium", 0x2100: "Medium+",
                    0x3000: "High", 0x4000: "System"}


class _SID_AND_ATTRIBUTES(ctypes.Structure):
    _fields_ = (("Sid", ctypes.c_void_p), ("Attributes", wt.DWORD))


class _TOKEN_MANDATORY_LABEL(ctypes.Structure):
    _fields_ = (("Label", _SID_AND_ATTRIBUTES),)


def _process_integrity_rid(hprocess: wt.HANDLE) -> int | None:
    token = wt.HANDLE()
    if not advapi32.OpenProcessToken(hprocess, _TOKEN_QUERY, ctypes.byref(token)):
        return None
    try:
        needed = wt.DWORD(0)
        advapi32.GetTokenInformation(token, _TokenIntegrityLevel, None, 0,
                                     ctypes.byref(needed))
        if not needed.value:
            return None
        buffer = (ctypes.c_char * needed.value)()
        if not advapi32.GetTokenInformation(
            token, _TokenIntegrityLevel, buffer, needed.value,
            ctypes.byref(needed),
        ):
            return None
        label = ctypes.cast(buffer, ctypes.POINTER(_TOKEN_MANDATORY_LABEL)).contents
        # SID layout: Revision(1) SubAuthorityCount(1) IdentifierAuthority(6)
        # then DWORD SubAuthority[count]; the RID is the last sub-authority.
        if not label.Label.Sid:
            return None
        sid = (ctypes.c_ubyte * 72).from_address(label.Label.Sid)
        count = sid[1]
        if count == 0:
            return None
        offset = 8 + 4 * (count - 1)
        return int.from_bytes(bytes(sid[offset:offset + 4]), "little")
    finally:
        kernel32.CloseHandle(token)


# This process's integrity level never changes; query the token once.
_OWN_INTEGRITY_RID: int | None = None
_OWN_INTEGRITY_CHECKED = False


def post_message_blocked_by_uipi(hwnd: int) -> str | None:
    global _OWN_INTEGRITY_RID, _OWN_INTEGRITY_CHECKED
    pid = wt.DWORD()
    if not user32.GetWindowThreadProcessId(wt.HWND(hwnd), ctypes.byref(pid)):
        return None
    if not _OWN_INTEGRITY_CHECKED:
        _OWN_INTEGRITY_CHECKED = True
        _OWN_INTEGRITY_RID = _process_integrity_rid(kernel32.GetCurrentProcess())
    own_rid = _OWN_INTEGRITY_RID
    if own_rid is None:
        return None
    handle = kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid.value)
    if not handle:
        return None
    try:
        target_rid = _process_integrity_rid(wt.HANDLE(handle))
    finally:
        kernel32.CloseHandle(handle)
    if target_rid is None or target_rid <= own_rid:
        return None
    own_name = _INTEGRITY_NAMES.get(own_rid, f"0x{own_rid:x}")
    target_name = _INTEGRITY_NAMES.get(target_rid, f"0x{target_rid:x}")
    return (
        f"UIPI: target pid {pid.value} runs at {target_name} integrity but this "
        f"harness is at {own_name}; posted input would be silently dropped "
        "(common cause: the app requires Administrator). Run windows-harness "
        "elevated to drive elevated apps."
    )


def refuse_background(hwnd: int, kind: str, cause: str | None = None) -> BackgroundUnavailable:
    """Build the structured error an agent escalates from."""
    if cause is None:
        cause = (
            f"the '{read_class_name(hwnd)}' input stack silently drops "
            f"{kind} posted in the background"
        )
    lowered = cause.lower()
    if "occluded" in lowered:
        code = "background_occluded"
    elif "uipi" in lowered or "integrity" in lowered:
        code = "background_uipi_blocked"
    else:
        code = "background_unavailable"
    return BackgroundUnavailable(code, read_class_name(hwnd), kind, cause)
