"""Input transports: synthetic-pen injection, window messages, SendInput.

Ported from trycua/cua `platform-windows/src/input/` (MIT, Copyright 2025
Cua AI, Inc.): inject.rs, mouse.rs, keyboard.rs, delivery dispatch in
tools/impl_.rs.

Three transports, one contract:

- ``pen``      — InjectSyntheticPointerInput routes by screen coordinate
                 through the system input queue. The kernel injection path
                 gates only on a per-process injection enable, NOT on the
                 target being foreground, so Chromium/WPF/UWP accept it in
                 the background and the user's cursor never moves. Left
                 click = pen tap; right click = pen tap with the barrel
                 button held; middle has no pointer mapping.
- ``message``  — PostMessageW for classic Win32 windows. Never activates.
- ``foreground`` — brief cloaked SetForegroundWindow, then SendInput; when
                 hook software swallows SendInput (see :func:`sendinput_health`)
                 each action falls back to pen injection or window messages
                 against the now-foreground target, and key combos refuse.

Background dispatch refuses honestly (:mod:`windows_harness.delivery`)
instead of silently fronting; only the agent escalates.
"""

from __future__ import annotations

import atexit
import ctypes
import ctypes.wintypes as wt
import json
import os
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Callable

from . import delivery
from .capture import (
    _PROCESSENTRY32W,
    CWP_SKIPDISABLED,
    CWP_SKIPINVISIBLE,
    CWP_SKIPTRANSPARENT,
    DWMWA_CLOAK,
    HarnessError,
    dwmapi,
    is_cloaked,
    kernel32,
    user32,
)


class ForegroundError(HarnessError):
    """The harness could not take (or give back) the foreground."""


# --- Win32 constants and SendInput structures ------------------------------

SW_RESTORE = 9
SW_MINIMIZE = 6
WHEEL_DELTA = 120
VK_NONAME = 0xFC  # no-application-meaning key; grants the foreground token

WM_MOUSEMOVE = 0x0200
WM_LBUTTONDOWN = 0x0201
WM_LBUTTONUP = 0x0202
WM_LBUTTONDBLCLK = 0x0203
WM_RBUTTONDOWN = 0x0204
WM_RBUTTONUP = 0x0205
WM_RBUTTONDBLCLK = 0x0206
WM_MBUTTONDOWN = 0x0207
WM_MBUTTONUP = 0x0208
WM_MBUTTONDBLCLK = 0x0209
WM_MOUSEWHEEL = 0x020A
WM_MOUSEHWHEEL = 0x020E
WM_KEYDOWN = 0x0100
WM_KEYUP = 0x0101
WM_CHAR = 0x0102
WM_PASTE = 0x0302

MK_LBUTTON = 0x0001
MK_RBUTTON = 0x0002
MK_MBUTTON = 0x0010

VK_SHIFT = 0x10
VK_CONTROL = 0x11
VK_MENU = 0x12
VK_LWIN = 0x5B
VK_RETURN = 0x0D
VK_TAB = 0x09
VK_SPACE = 0x20

MOUSEEVENTF_MOVE = 0x0001
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
MOUSEEVENTF_RIGHTDOWN = 0x0008
MOUSEEVENTF_RIGHTUP = 0x0010
MOUSEEVENTF_MIDDLEDOWN = 0x0020
MOUSEEVENTF_MIDDLEUP = 0x0040
MOUSEEVENTF_WHEEL = 0x0800
MOUSEEVENTF_HWHEEL = 0x01000
MOUSEEVENTF_ABSOLUTE = 0x8000

KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_EXTENDEDKEY = 0x0001
KEYEVENTF_UNICODE = 0x0004
INPUT_MOUSE = 0
INPUT_KEYBOARD = 1

GA_ROOT = 2
GWL_EXSTYLE = -20
WS_EX_NOACTIVATE = 0x08000000

# Synthetic pointer (Win10 1709+)
PT_TOUCH = 2
PT_PEN = 3
POINTER_FEEDBACK_DEFAULT = 1
POINTER_FLAG_INRANGE = 0x00000002
POINTER_FLAG_INCONTACT = 0x00000004
POINTER_FLAG_DOWN = 0x00010000
POINTER_FLAG_UPDATE = 0x00020000
POINTER_FLAG_UP = 0x00040000
PEN_FLAG_BARREL = 0x00000001  # barrel held == secondary (right) click

if hasattr(user32, "GetWindowLongPtrW"):
    _get_window_long = user32.GetWindowLongPtrW
    _set_window_long = user32.SetWindowLongPtrW
else:  # 32-bit Python
    _get_window_long = user32.GetWindowLongW
    _set_window_long = user32.SetWindowLongW


class _MOUSEINPUT(ctypes.Structure):
    _fields_ = (
        ("dx", wt.LONG),
        ("dy", wt.LONG),
        ("mouseData", wt.DWORD),
        ("dwFlags", wt.DWORD),
        ("time", wt.DWORD),
        ("dwExtraInfo", ctypes.c_size_t),
    )


class _KEYBDINPUT(ctypes.Structure):
    _fields_ = (
        ("wVk", wt.WORD),
        ("wScan", wt.WORD),
        ("dwFlags", wt.DWORD),
        ("time", wt.DWORD),
        ("dwExtraInfo", ctypes.c_size_t),
    )


class _HARDWAREINPUT(ctypes.Structure):
    _fields_ = (("uMsg", wt.DWORD), ("wParamL", wt.WORD), ("wParamH", wt.WORD))


class _INPUT_UNION(ctypes.Union):
    _fields_ = (("mi", _MOUSEINPUT), ("ki", _KEYBDINPUT), ("hi", _HARDWAREINPUT))


class _INPUT(ctypes.Structure):
    _anonymous_ = ("union",)
    _fields_ = (("type", wt.DWORD), ("union", _INPUT_UNION))


def send_inputs(*inputs: tuple[int, dict]) -> None:
    """Inject through the system input queue; verify the full batch landed."""
    array = (_INPUT * len(inputs))()
    for slot, (kind, fields) in zip_strict(array, inputs):
        slot.type = kind
        # The union's members (mi/ki/hi) are promoted to _INPUT, but their
        # sub-fields (wVk, dwFlags, dx, dy, wScan, mouseData, ...) are NOT.
        # setattr on the outer slot silently writes a Python-only attribute,
        # leaving the C union all zero, so SendInput sends a null event that
        # never reaches the target (and the health probe reads 'swallowed').
        # Write into the concrete member for the input kind instead.
        target = slot.union.ki if kind == INPUT_KEYBOARD else slot.union.mi
        for name, value in fields.items():
            setattr(target, name, value)
    sent = user32.SendInput(len(inputs), array, ctypes.sizeof(_INPUT))
    if sent != len(inputs):
        raise ForegroundError(
            f"SendInput delivered {sent}/{len(inputs)} events — the foreground "
            "swap was likely rejected, so the events would land elsewhere"
        )


# --- key tables -------------------------------------------------------------

MODIFIER_VK = {
    "ctrl": VK_CONTROL,
    "control": VK_CONTROL,
    "alt": VK_MENU,
    "option": VK_MENU,
    "shift": VK_SHIFT,
    "win": VK_LWIN,
    "cmd": VK_LWIN,
    "super": VK_LWIN,
}

_VK_BASE = {
    **{chr(code): code for code in range(0x41, 0x5B)},  # A-Z
    **{str(digit): 0x30 + digit for digit in range(10)},
    "return": VK_RETURN,
    "enter": VK_RETURN,
    "tab": VK_TAB,
    "space": VK_SPACE,
    "backspace": 0x08,
    "delete": 0x2E,
    "escape": 0x1B,
    "esc": 0x1B,
    "home": 0x24,
    "end": 0x23,
    "pageup": 0x21,
    "page_up": 0x21,
    "pagedown": 0x22,
    "page_down": 0x22,
    "insert": 0x2D,
    "left": 0x25,
    "up": 0x26,
    "right": 0x27,
    "down": 0x28,
    **{f"f{index}": 0x6F + index for index in range(1, 13)},
}
_VK_PUNCTUATION = {
    "-": 0xBD,
    "=": 0xBB,
    "[": 0xDB,
    "]": 0xDD,
    "\\": 0xDC,
    ";": 0xBA,
    "'": 0xDE,
    ",": 0xBC,
    ".": 0xBE,
    "/": 0xBF,
    "`": 0xC0,
}

# Keys whose events need the extended-key flag (keyboard.rs key_input).
_EXTENDED_KEYS = {
    0x21, 0x22,  # page up / page down
    0x23, 0x24,  # end / home
    0x25, 0x26, 0x27, 0x28,  # arrows
    0x2D, 0x2E,  # insert / delete
    0x5B, 0x5C,  # left / right win
}


def vk_for_key(key: str) -> int:
    lowered = key.casefold()
    if len(lowered) == 1:
        # Non-ASCII alnum characters (e.g. CJK) would produce a bogus VK code
        # above 0xFF that SendInput silently ignores; refuse them honestly.
        if lowered.isascii() and lowered.isalnum():
            return ord(lowered.upper())
        if lowered in _VK_PUNCTUATION:
            return _VK_PUNCTUATION[lowered]
    try:
        return _VK_BASE[lowered]
    except KeyError as exc:
        raise HarnessError(f"Unsupported key {key!r}") from exc


def parse_combo(key: str) -> tuple[int, list[int]]:
    """Split 'ctrl+s' into (base_vk, [modifier_vks])."""
    parts = [part.casefold() for part in key.split("+") if part]
    if not parts:
        raise HarnessError("key must not be empty")
    base = vk_for_key(parts[-1])
    modifiers = []
    for modifier in parts[:-1]:
        if modifier not in MODIFIER_VK:
            raise HarnessError(f"Unsupported modifier {modifier!r}")
        modifiers.append(MODIFIER_VK[modifier])
    return base, modifiers


def _keyboard_input(vk: int, *, up: bool = False) -> tuple[int, dict]:
    flags = KEYEVENTF_KEYUP if up else 0
    if vk in _EXTENDED_KEYS:
        flags |= KEYEVENTF_EXTENDEDKEY
    return (INPUT_KEYBOARD, {"wVk": vk, "dwFlags": flags})


_BUTTON_MESSAGES = {
    "left": (MK_LBUTTON, WM_LBUTTONDOWN, WM_LBUTTONDBLCLK, WM_LBUTTONUP),
    "right": (MK_RBUTTON, WM_RBUTTONDOWN, WM_RBUTTONDBLCLK, WM_RBUTTONUP),
    "middle": (MK_MBUTTON, WM_MBUTTONDOWN, WM_MBUTTONDBLCLK, WM_MBUTTONUP),
}
_BUTTON_SENDINPUT = {
    "left": (MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP),
    "right": (MOUSEEVENTF_RIGHTDOWN, MOUSEEVENTF_RIGHTUP),
    "middle": (MOUSEEVENTF_MIDDLEDOWN, MOUSEEVENTF_MIDDLEUP),
}


# --- small Win32 helpers ----------------------------------------------------


def pack_lparam(x: int, y: int) -> int:
    return ((int(y) & 0xFFFF) << 16) | (int(x) & 0xFFFF)


def _split_scroll_delta(delta: int, maximum: int) -> list[int]:
    """Split a wheel delta into small exact steps accepted reliably by apps."""
    if maximum <= 0:
        raise ValueError("maximum must be positive")
    remaining = int(delta)
    steps: list[int] = []
    while remaining:
        step = max(-maximum, min(maximum, remaining))
        steps.append(step)
        remaining -= step
    return steps


def screen_to_client(hwnd: int, x: float, y: float) -> tuple[int, int]:
    point = wt.POINT(int(round(x)), int(round(y)))
    user32.ScreenToClient(hwnd, ctypes.byref(point))
    return point.x, point.y


def client_to_screen(hwnd: int, x: float, y: float) -> tuple[int, int]:
    point = wt.POINT(int(round(x)), int(round(y)))
    user32.ClientToScreen(hwnd, ctypes.byref(point))
    return point.x, point.y


def child_window_at(hwnd: int, client_x: int, client_y: int) -> int:
    point = wt.POINT(client_x, client_y)
    flags = CWP_SKIPINVISIBLE | CWP_SKIPTRANSPARENT | CWP_SKIPDISABLED
    child = user32.ChildWindowFromPointEx(hwnd, point, flags)
    return child or hwnd


def post_message(hwnd: int, message: int, wparam: int, lparam: int) -> None:
    posted = user32.PostMessageW(
        wt.HWND(hwnd), message, wt.WPARAM(wparam), wt.LPARAM(lparam)
    )
    if posted:
        return
    raise HarnessError(
        f"PostMessage {message:#x} failed: {ctypes.FormatError(ctypes.get_last_error())}"
    )


def scan_code(vk: int) -> int:
    return user32.MapVirtualKeyW(vk, 0)  # MAPVK_VK_TO_VSC


def set_cursor(x: float, y: float) -> None:
    if not user32.SetCursorPos(int(round(x)), int(round(y))):
        raise ForegroundError("SetCursorPos failed")


def get_cursor() -> tuple[int, int]:
    point = wt.POINT()
    if not user32.GetCursorPos(ctypes.byref(point)):
        raise ForegroundError("GetCursorPos failed")
    return point.x, point.y


def current_foreground() -> int:
    return user32.GetForegroundWindow() or 0


def foreground_root_hwnd() -> int:
    """The top-level (root) window that owns the foreground, or 0.

    ``GetForegroundWindow`` returns the foreground HWND, which is often a
    child/editor control. ``GetAncestor(GA_ROOT)`` walks up to the window
    the taskbar/title belongs to so ``see()`` with no app targets the real
    frame a modal dialog (Save As / Open / Print) sits on top of.
    """
    hwnd = current_foreground()
    if not hwnd:
        return 0
    root = user32.GetAncestor(wt.HWND(hwnd), GA_ROOT) or hwnd
    return int(root)


def zip_strict(*iterables):
    """``zip`` that refuses unequal lengths, matching Python 3.10+ semantics.

    The project targets Python 3.10+ where ``zip(..., strict=True)`` exists,
    but the bundled CLI can run against an older interpreter; keeping the
    strict length check here preserves the "no silent truncation" guarantee
    on every runtime instead of silently dropping the tail of a batch.
    """
    sentinel = object()
    iterators = [iter(iterable) for iterable in iterables]
    while True:
        items = [next(it, sentinel) for it in iterators]
        if items[0] is sentinel and all(item is sentinel for item in items):
            return
        if any(item is sentinel for item in items):
            raise ValueError("zip_strict() arguments have different lengths")
        yield tuple(items)


def point_on_screen(x: float, y: float) -> bool:
    """True when a physical screen point lies inside the virtual desktop.

    The harness produces physical pixel coordinates; a bad client-bounds read
    (usually a transient off-screen value during a foreground/cloak handoff)
    can turn a window center into a large negative point. Callers should bail
    to a known-good anchor rather than scroll/click a point that is not on any
    monitor.
    """
    SM_XVIRTUALSCREEN = 76
    SM_YVIRTUALSCREEN = 77
    SM_CXVIRTUALSCREEN = 78
    SM_CYVIRTUALSCREEN = 79
    left = user32.GetSystemMetrics(SM_XVIRTUALSCREEN)
    top = user32.GetSystemMetrics(SM_YVIRTUALSCREEN)
    width = user32.GetSystemMetrics(SM_CXVIRTUALSCREEN)
    height = user32.GetSystemMetrics(SM_CYVIRTUALSCREEN)
    if width <= 0 or height <= 0:
        return False
    return left <= x < left + width and top <= y < top + height


def _root_ancestor(hwnd: int) -> int:
    return user32.GetAncestor(wt.HWND(hwnd), GA_ROOT) or hwnd


# --- foreground plumbing -----------------------------------------------------


def _attached_set_foreground(target: int) -> bool:
    foreground_thread = user32.GetWindowThreadProcessId(current_foreground() or target, None)
    current_thread = kernel32.GetCurrentThreadId()
    attached = bool(
        foreground_thread
        and foreground_thread != current_thread
        and user32.AttachThreadInput(current_thread, foreground_thread, True)
    )
    try:
        user32.SetForegroundWindow(target)
    finally:
        if attached:
            user32.AttachThreadInput(current_thread, foreground_thread, False)
    return current_foreground() == target


def force_foreground(target: int) -> bool:
    """Bring one window to the foreground; AttachThreadInput inherits the
    foreground-lock token, a VK_NONAME tap grants it on retry (inject.rs
    force_foreground_attached / force_foreground_assisted)."""
    if current_foreground() == target:
        return True
    foreground_thread = user32.GetWindowThreadProcessId(current_foreground() or target, None)
    current_thread = kernel32.GetCurrentThreadId()
    attached = bool(
        foreground_thread
        and foreground_thread != current_thread
        and user32.AttachThreadInput(current_thread, foreground_thread, True)
    )
    try:
        if user32.IsIconic(target):
            user32.ShowWindow(target, SW_RESTORE)
        user32.BringWindowToTop(target)
        user32.SetForegroundWindow(target)
    finally:
        if attached:
            user32.AttachThreadInput(current_thread, foreground_thread, False)
    if current_foreground() == target:
        return True
    send_inputs(
        (INPUT_KEYBOARD, {"wVk": VK_NONAME}),
        (INPUT_KEYBOARD, {"wVk": VK_NONAME, "dwFlags": KEYEVENTF_KEYUP}),
    )
    for _ in range(3):
        time.sleep(0.025)
        if _attached_set_foreground(target):
            return True
    # Last resort: the taskbar's own activation path. Unlike SetForeground-
    # Window it needs no foreground token, so it still works when hook
    # software swallowed the VK_NONAME tap above (observed on machines with
    # Nahimic/QQLive-class hooks; QQMusic refused every SetForegroundWindow
    # but yielded to this).
    try:
        user32.SwitchToThisWindow(wt.HWND(target), True)
    except Exception:  # not present on some server SKUs
        return False
    time.sleep(0.05)
    return current_foreground() == target


def set_cloak(hwnd: int, enabled: bool) -> bool:
    """Hide/show a window through DWM cloaking; False when unsupported.

    Tries DWM_CLOAKED_APP first, then DWM_CLOAKED_SHELL — some UWP frames
    refuse the former but honour the latter. Success means the attribute
    actually took, verified by read-back.
    """
    if not enabled:
        value = wt.INT(0)
        result = dwmapi.DwmSetWindowAttribute(
            wt.HWND(hwnd), wt.DWORD(DWMWA_CLOAK), ctypes.byref(value), ctypes.sizeof(value)
        )
        return result == 0
    for reason in (1, 2):  # DWM_CLOAKED_APP, then DWM_CLOAKED_SHELL
        value = wt.INT(reason)
        result = dwmapi.DwmSetWindowAttribute(
            wt.HWND(hwnd), wt.DWORD(DWMWA_CLOAK), ctypes.byref(value), ctypes.sizeof(value)
        )
        if result == 0 and is_cloaked(hwnd):
            return True
    return False


# --- cloak crash journal -----------------------------------------------------
#
# A harness killed between cloaking a window and uncloaking it would leave the
# user's window permanently invisible. Cloaked hwnds are journaled to disk and
# released either on restore or by the next session at startup.


def _cloak_journal_path() -> Path:
    return delivery.config_dir() / "cloaked.json"


def _journaled_hwnds() -> list[int]:
    try:
        data = json.loads(_cloak_journal_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    if not isinstance(data, list):
        return []
    entries: list[int] = []
    for token in data:
        try:
            entries.append(int(token, 16))
        except (TypeError, ValueError):
            continue
    return entries


def _rewrite_cloak_journal(entries: list[int]) -> None:
    try:
        path = _cloak_journal_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        if entries:
            path.write_text(
                json.dumps([f"{hwnd:x}" for hwnd in entries]), encoding="utf-8"
            )
        else:
            path.unlink(missing_ok=True)
    except OSError:
        pass  # the journal is best-effort; never block an action on it


def _journal_cloak(hwnd: int) -> None:
    entries = _journaled_hwnds()
    if hwnd not in entries:
        entries.append(hwnd)
    _rewrite_cloak_journal(entries)


def _unjournal_cloak(hwnd: int) -> None:
    entries = _journaled_hwnds()
    if hwnd in entries:
        entries.remove(hwnd)
        _rewrite_cloak_journal(entries)


def recover_abandoned_cloaks() -> int:
    """Uncloak windows left hidden by a crashed previous session."""
    recovered = 0
    for hwnd in _journaled_hwnds():
        if user32.IsWindow(wt.HWND(hwnd)) and set_cloak(hwnd, False):
            recovered += 1
    if _journaled_hwnds():
        _rewrite_cloak_journal([])
    return recovered


# --- SendInput health probe -------------------------------------------------
#
# Hook software (audio enhancements, overlay recorders, some IMEs) silently
# eats events carrying the INJECTED flag: SendInput reports full delivery,
# the foreground swap succeeds, and no application ever sees the input. The
# probe focuses a hidden window of our own through an attached input queue,
# injects one VK_NONAME tap, and watches the WndProc. 'unknown' when the
# probe cannot run (no foreground thread, or UIPI blocks the attachment).
# Force a verdict with WINDOWS_HARNESS_SENDINPUT=ok|swallowed.

_WNDPROC = ctypes.WINFUNCTYPE(
    ctypes.c_ssize_t, wt.HWND, wt.UINT, ctypes.c_size_t, ctypes.c_ssize_t
)


class _WNDCLASSEXW(ctypes.Structure):
    _fields_ = (
        ("cbSize", wt.UINT),
        ("style", wt.UINT),
        ("lpfnWndProc", _WNDPROC),
        ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int),
        ("hInstance", wt.HANDLE),
        ("hIcon", wt.HANDLE),
        ("hCursor", wt.HANDLE),
        ("hbrBackground", wt.HANDLE),
        ("lpszMenuName", wt.LPCWSTR),
        ("lpszClassName", wt.LPCWSTR),
        ("hIconSm", wt.HANDLE),
    )


_PROBE_CLASS_NAME = "WindowsHarnessInputProbe"
_PROBE_RECEIVED: list[int] = []


@_WNDPROC
def _probe_wnd_proc(hwnd: int, message: int, wparam: int, lparam: int) -> int:
    if message == WM_KEYDOWN and wparam == VK_NONAME:
        _PROBE_RECEIVED.append(1)
        return 0
    return user32.DefWindowProcW(hwnd, message, wparam, lparam)


user32.DefWindowProcW.restype = ctypes.c_ssize_t
user32.DefWindowProcW.argtypes = (wt.HWND, wt.UINT, ctypes.c_size_t, ctypes.c_ssize_t)
user32.CreateWindowExW.restype = wt.HWND
user32.CreateWindowExW.argtypes = (
    wt.DWORD, wt.LPCWSTR, wt.LPCWSTR, wt.DWORD,
    ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
    wt.HWND, wt.HANDLE, wt.HANDLE, wt.LPCVOID,
)

# The probe path below previously leaned on ctypes' default signatures, which
# truncate HMODULE/HWND return values to c_int on 64-bit. GetModuleHandleW(None)
# then returned the low 32 bits of the exe base (e.g. 0x3be00000 instead of
# 0x7ff63be00000), so the probe registered its window class and created its
# window under a bogus hInstance -- intermittent access violations in
# RegisterClassExW/CreateWindowExW. Pin the full 64-bit signatures.
kernel32.GetModuleHandleW.restype = wt.HANDLE
kernel32.GetModuleHandleW.argtypes = (wt.LPCWSTR,)
user32.RegisterClassExW.restype = ctypes.c_ushort  # ATOM
user32.RegisterClassExW.argtypes = (ctypes.POINTER(_WNDCLASSEXW),)
user32.GetFocus.restype = wt.HWND
user32.SetFocus.restype = wt.HWND
user32.SetFocus.argtypes = (wt.HWND,)
user32.DestroyWindow.restype = wt.BOOL
user32.DestroyWindow.argtypes = (wt.HWND,)
user32.PeekMessageW.restype = wt.BOOL
user32.PeekMessageW.argtypes = (
    ctypes.POINTER(wt.MSG), wt.HWND, wt.UINT, wt.UINT, wt.UINT,
)
user32.AttachThreadInput.restype = wt.BOOL
user32.AttachThreadInput.argtypes = (wt.DWORD, wt.DWORD, wt.BOOL)
user32.GetWindowThreadProcessId.restype = wt.DWORD
user32.GetWindowThreadProcessId.argtypes = (wt.HWND, ctypes.POINTER(wt.DWORD))


def _register_probe_class() -> bool:
    hinstance = kernel32.GetModuleHandleW(None)
    wcex = _WNDCLASSEXW()
    wcex.cbSize = ctypes.sizeof(_WNDCLASSEXW)
    wcex.lpfnWndProc = _probe_wnd_proc
    wcex.hInstance = hinstance
    wcex.lpszClassName = _PROBE_CLASS_NAME
    if user32.RegisterClassExW(ctypes.byref(wcex)):
        return True
    # ERROR_CLASS_ALREADY_EXISTS (1410): a previous probe registered it.
    return ctypes.get_last_error() == 1410


def sendinput_health(timeout: float = 0.8) -> str:
    """'ok' when injected keys arrive, 'swallowed' when hooks eat them,
    'unknown' when the probe cannot run. Briefly takes keyboard focus."""
    override = os.environ.get("WINDOWS_HARNESS_SENDINPUT", "").strip().casefold()
    if override in ("swallowed", "off", "0", "false"):
        return "swallowed"
    if override in ("ok", "on", "1", "true"):
        return "ok"

    foreground = current_foreground()
    if not foreground:
        return "unknown"
    foreground_thread = user32.GetWindowThreadProcessId(foreground, None)
    current_thread = kernel32.GetCurrentThreadId()
    attached = bool(
        foreground_thread
        and foreground_thread != current_thread
        and user32.AttachThreadInput(current_thread, foreground_thread, True)
    )
    if foreground_thread != current_thread and not attached:
        return "unknown"  # foreground runs elevated (UIPI); cannot share focus
    if not _register_probe_class():
        if attached:
            user32.AttachThreadInput(current_thread, foreground_thread, False)
        return "unknown"

    _PROBE_RECEIVED.clear()
    hinstance = kernel32.GetModuleHandleW(None)
    probe = user32.CreateWindowExW(
        0, _PROBE_CLASS_NAME, "", 0, -32000, -32000, 1, 1,
        None, None, hinstance, None,
    )
    if not probe:
        if attached:
            user32.AttachThreadInput(current_thread, foreground_thread, False)
        return "unknown"
    try:
        previous_focus = user32.GetFocus()
        user32.SetFocus(wt.HWND(probe))
        if user32.GetFocus() != probe:
            return "unknown"  # focus refused; a verdict would be a guess
        send_inputs(
            (INPUT_KEYBOARD, {"wVk": VK_NONAME}),
            (INPUT_KEYBOARD, {"wVk": VK_NONAME, "dwFlags": KEYEVENTF_KEYUP}),
        )
        deadline = time.monotonic() + max(0.1, timeout)
        message = wt.MSG()
        while time.monotonic() < deadline and not _PROBE_RECEIVED:
            while user32.PeekMessageW(ctypes.byref(message), wt.HWND(probe), 0, 0, 1):
                user32.TranslateMessage(ctypes.byref(message))
                user32.DispatchMessageW(ctypes.byref(message))
            time.sleep(0.004)
        return "ok" if _PROBE_RECEIVED else "swallowed"
    finally:
        if previous_focus:
            user32.SetFocus(previous_focus)
        if attached:
            user32.AttachThreadInput(current_thread, foreground_thread, False)
        user32.DestroyWindow(wt.HWND(probe))


_health_cache: str | None = None


def sendinput_healthy() -> bool:
    """Cached probe verdict; only foreground transports consult it."""
    global _health_cache
    if _health_cache is None:
        _health_cache = sendinput_health()
    return _health_cache == "ok"


def reset_health_cache() -> None:
    global _health_cache
    _health_cache = None


# --- hook-software suspects -------------------------------------------------

_HOOK_SUSPECTS = frozenset({
    "nahimicservice.exe", "nahimic2.exe", "nahimic3.exe", "nahimicnotif.exe",
    "qqlive.exe", "qqlivebrowser.exe", "qqliveservice.exe",
    "nvidia share.exe", "nvsphelper64.exe", "nvcontainer.exe",
    "rtss.exe", "rtsshooksloader.exe", "msiafterburner.exe",
    "obs64.exe", "obs32.exe", "gamebarpresencewriter.exe",
})

_TH32CS_SNAPPROCESS = 0x2


def suspect_injectors() -> list[str]:
    """Running processes known to install global hooks that can eat injected
    input. A heuristic hint for the agent, not proof of guilt."""
    snapshot = kernel32.CreateToolhelp32Snapshot(_TH32CS_SNAPPROCESS, 0)
    if not snapshot or snapshot == ctypes.c_size_t(-1).value:
        return []
    found: set[str] = set()
    entry = _PROCESSENTRY32W()
    entry.dwSize = ctypes.sizeof(_PROCESSENTRY32W)
    try:
        has_next = kernel32.Process32FirstW(snapshot, ctypes.byref(entry))
        while has_next:
            name = entry.szExeFile.casefold()
            if name in _HOOK_SUSPECTS:
                found.add(entry.szExeFile)
            has_next = kernel32.Process32NextW(snapshot, ctypes.byref(entry))
    finally:
        kernel32.CloseHandle(snapshot)
    return sorted(found)


_HELD_FOREGROUND: dict[str, int] | None = None


def release_hold() -> dict[str, int] | None:
    """Give the foreground back after a held burst; None when nothing held."""
    global _HELD_FOREGROUND
    held = _HELD_FOREGROUND
    _HELD_FOREGROUND = None
    if held is None:
        return None
    previous = held["previous"]
    if (previous and previous != held["target"]
            and user32.IsWindow(wt.HWND(previous))):
        try:
            force_foreground(previous)
        except HarnessError:
            pass
    return held


@contextmanager
def cloaked_focus(hwnd: int, *, cloak: bool = True, hold: bool = False):
    """Temporarily own the desktop on behalf of one background window.

    Default behaviour cloaks the target when the OS allows so the takeover is
    invisible, then restores the previous foreground window and cursor
    position. Yields whether cloaking actually happened — callers report it
    honestly.

    ``hold=True`` is the interactive default: the target stays fronted and
    visible afterwards, because focus-driven UI (autocomplete popups, menus)
    closes the instant the window loses the foreground between two actions.
    The previous foreground is remembered once and restored only by an
    explicit :func:`release_hold`.
    """
    global _HELD_FOREGROUND
    previous_foreground = current_foreground()
    previous_cursor = get_cursor()
    was_minimized = bool(user32.IsIconic(hwnd))
    cloaked_ok = set_cloak(hwnd, True) if cloak and not hold else False
    if cloaked_ok:
        _journal_cloak(hwnd)  # a crash before uncloak must stay recoverable
    try:
        if was_minimized:
            user32.ShowWindow(hwnd, SW_RESTORE)
            time.sleep(0.05)
        if not force_foreground(hwnd):
            raise ForegroundError(
                "could not bring the target window to the foreground"
            )
        if hold:
            if _HELD_FOREGROUND is None:
                _HELD_FOREGROUND = {
                    "previous": previous_foreground,
                    "target": hwnd,
                }
            else:
                _HELD_FOREGROUND["target"] = hwnd
        yield cloaked_ok
    finally:
        # hold: the whole point is NOT giving the foreground back between
        # actions — the window stays fronted until release_hold().
        if not hold:
            # Give the foreground back BEFORE touching the target's placement
            # so the user never sees a focus jump.
            if previous_foreground and previous_foreground != hwnd:
                try:
                    force_foreground(previous_foreground)
                except HarnessError:
                    pass
            if was_minimized:
                user32.ShowWindow(hwnd, SW_MINIMIZE)
                time.sleep(0.02)
            if cloaked_ok:
                # Only drop the journal entry once the window is veribly
                # visible again; a failed restore stays recoverable at next
                # startup.
                if set_cloak(hwnd, False):
                    _unjournal_cloak(hwnd)
            try:
                set_cursor(*previous_cursor)
            except ForegroundError:
                pass


# --- synthetic pen injection (inject.rs) -------------------------------------

_POINTER_FEEDBACK_DEFAULT = ctypes.c_uint(1)
try:
    CreateSyntheticPointerDevice = user32.CreateSyntheticPointerDevice
    InjectSyntheticPointerInput = user32.InjectSyntheticPointerInput
    DestroySyntheticPointerDevice = user32.DestroySyntheticPointerDevice
except AttributeError as exc:  # pragma: no cover - pre-1709 Windows
    raise HarnessError(
        "Synthetic pointer injection requires Windows 10 1709 or newer"
    ) from exc

CreateSyntheticPointerDevice.restype = wt.HANDLE
CreateSyntheticPointerDevice.argtypes = (wt.UINT, wt.UINT, ctypes.c_uint)
InjectSyntheticPointerInput.restype = wt.BOOL
InjectSyntheticPointerInput.argtypes = (
    wt.HANDLE, ctypes.c_void_p, wt.UINT,
)
DestroySyntheticPointerDevice.argtypes = (wt.HANDLE,)


class _POINTER_INFO(ctypes.Structure):
    _fields_ = (
        ("pointerType", wt.UINT),
        ("pointerId", wt.UINT),
        ("frameId", wt.UINT),
        ("pointerFlags", wt.UINT),
        ("sourceDevice", wt.HANDLE),
        ("hwndTarget", wt.HWND),
        ("ptPixelLocation", wt.POINT),
        ("ptHimetricLocation", wt.POINT),
        ("ptPixelLocationRaw", wt.POINT),
        ("ptHimetricLocationRaw", wt.POINT),
        ("dwTime", wt.DWORD),
        ("historyCount", wt.UINT),
        ("InputData", wt.LONG),
        ("dwKeyStates", wt.DWORD),
        ("PerformanceCount", ctypes.c_uint64),
    )


class _POINTER_PEN_INFO(ctypes.Structure):
    _fields_ = (
        ("pointerInfo", _POINTER_INFO),
        ("penFlags", wt.UINT),
        ("penMask", wt.UINT),
        ("pressure", wt.UINT),
        ("rotation", wt.LONG),
        ("tiltX", wt.LONG),
        ("tiltY", wt.LONG),
    )


class _POINTER_TOUCH_INFO(ctypes.Structure):
    _fields_ = (
        ("pointerInfo", _POINTER_INFO),
        ("touchFlags", wt.UINT),
        ("touchMask", wt.UINT),
        ("rcContact", wt.RECT),
        ("rcContactRaw", wt.RECT),
        ("orientation", wt.UINT),
        ("pressure", wt.UINT),
    )


class _POINTER_TYPE_INFO_UNION(ctypes.Union):
    _fields_ = (("penInfo", _POINTER_PEN_INFO), ("touchInfo", _POINTER_TOUCH_INFO))


class _POINTER_TYPE_INFO(ctypes.Structure):
    _anonymous_ = ("union",)
    _fields_ = (("pointerType", wt.UINT), ("union", _POINTER_TYPE_INFO_UNION))


def _pen_info(sx: int, sy: int, flags: int, barrel: bool) -> _POINTER_TYPE_INFO:
    info = _POINTER_TYPE_INFO()
    info.pointerType = PT_PEN
    pen = info.penInfo
    pen.pointerInfo.pointerType = PT_PEN
    pen.pointerInfo.pointerFlags = flags
    pen.pointerInfo.ptPixelLocation = wt.POINT(sx, sy)
    pen.penFlags = PEN_FLAG_BARREL if barrel else 0
    pen.pressure = 512
    return info


class _PenInjectionFailed(Exception):
    """Internal: an InjectSyntheticPointerInput call reported failure."""


_pointer_devices: dict[int, int] = {}


def _acquire_pointer_device(pointer_type: int) -> int:
    """One shared synthetic device per pointer type; creating a virtual HID
    stack is PnP work, and rapid create/destroy cycles fail outright in quick
    succession (cua #1984)."""
    device = _pointer_devices.get(pointer_type)
    if device:
        return device
    device = CreateSyntheticPointerDevice(pointer_type, 1, _POINTER_FEEDBACK_DEFAULT)
    if not device:
        raise HarnessError(
            f"CreateSyntheticPointerDevice failed: {ctypes.FormatError(ctypes.get_last_error())}"
        )
    _pointer_devices[pointer_type] = device
    return device


def _discard_pointer_device(pointer_type: int, device: int) -> None:
    """Destroy a possibly-wedged device so the next acquire builds a fresh one."""
    if _pointer_devices.get(pointer_type) == device:
        del _pointer_devices[pointer_type]
    DestroySyntheticPointerDevice(device)


def _destroy_cached_pointer_devices() -> None:
    while _pointer_devices:
        _, device = _pointer_devices.popitem()
        DestroySyntheticPointerDevice(device)


atexit.register(_destroy_cached_pointer_devices)


def _with_pointer_device(pointer_type: int, act: Callable[[int], None]) -> None:
    """Run act(device); a wedged shared device is destroyed and the action
    retried once on a fresh one before giving up."""
    for attempt in (1, 2):
        device = _acquire_pointer_device(pointer_type)
        try:
            act(device)
            return
        except _PenInjectionFailed as exc:
            _discard_pointer_device(pointer_type, device)
            if attempt == 2:
                raise HarnessError(
                    f"InjectSyntheticPointerInput failed: {exc}"
                ) from None


def pen_taps(sx: int, sy: int, *, barrel: bool, count: int = 1) -> None:
    """Down→up pen taps through the shared synthetic pen device."""
    down = _pen_info(
        sx, sy, POINTER_FLAG_DOWN | POINTER_FLAG_INRANGE | POINTER_FLAG_INCONTACT, barrel
    )
    up = _pen_info(sx, sy, POINTER_FLAG_UP, barrel)

    def act(device: int) -> None:
        for index in range(max(1, count)):
            ok = InjectSyntheticPointerInput(
                device, ctypes.byref(down), 1
            ) and InjectSyntheticPointerInput(device, ctypes.byref(up), 1)
            if not ok:
                raise _PenInjectionFailed(
                    ctypes.FormatError(ctypes.get_last_error())
                )
            time.sleep(0.025)
            if index + 1 < count:
                time.sleep(0.07)

    _with_pointer_device(PT_PEN, act)


def pen_drag(sx0: int, sy0: int, sx1: int, sy1: int, *, steps: int = 12, duration: float = 0.25) -> None:
    """Press, glide, release — all through the shared synthetic pen device."""

    def act(device: int) -> None:
        contact = POINTER_FLAG_DOWN | POINTER_FLAG_INRANGE | POINTER_FLAG_INCONTACT
        if not InjectSyntheticPointerInput(
            device, ctypes.byref(_pen_info(sx0, sy0, contact, False)), 1
        ):
            raise _PenInjectionFailed(ctypes.FormatError(ctypes.get_last_error()))
        time.sleep(0.025)
        move_flags = POINTER_FLAG_UPDATE | POINTER_FLAG_INRANGE | POINTER_FLAG_INCONTACT
        for index in range(1, max(1, steps) + 1):
            ratio = index / max(1, steps)
            x = int(sx0 + (sx1 - sx0) * ratio)
            y = int(sy0 + (sy1 - sy0) * ratio)
            if not InjectSyntheticPointerInput(
                device, ctypes.byref(_pen_info(x, y, move_flags, False)), 1
            ):
                raise _PenInjectionFailed(ctypes.FormatError(ctypes.get_last_error()))
            time.sleep(max(0.0, duration) / max(1, steps))
        if not InjectSyntheticPointerInput(
            device, ctypes.byref(_pen_info(sx1, sy1, POINTER_FLAG_UP, False)), 1
        ):
            raise _PenInjectionFailed(ctypes.FormatError(ctypes.get_last_error()))

    _with_pointer_device(PT_PEN, act)


def _touch_info(sx: int, sy: int, flags: int) -> _POINTER_TYPE_INFO:
    info = _POINTER_TYPE_INFO()
    info.pointerType = PT_TOUCH
    touch = info.touchInfo
    touch.pointerInfo.pointerType = PT_TOUCH
    touch.pointerInfo.pointerFlags = flags
    touch.pointerInfo.ptPixelLocation = wt.POINT(sx, sy)
    touch.rcContact = wt.RECT(sx - 2, sy - 2, sx + 2, sy + 2)
    touch.rcContactRaw = wt.RECT(sx - 2, sy - 2, sx + 2, sy + 2)
    touch.pressure = 512
    return info


def touch_drag(sx0: int, sy0: int, sx1: int, sy1: int, *, steps: int = 12) -> None:
    """Left-drag through a synthetic TOUCH contact from a standing digitizer.

    A pen is an absolute *cursor* device: on non-pointer-aware windows the OS
    promotes it to mouse input and drags the user's real cursor along. A touch
    contact from a standing digitizer is consumed as touch and does not move
    the cursor (cua touch_drag); for the promotion cases where it still does,
    the cursor is snapped back twice so the net displacement is zero. A fast
    stroke (few frames, tiny dwell) keeps any excursion a brief flick.
    """
    try:
        prev_cursor = get_cursor()
    except ForegroundError:
        prev_cursor = None
    steps = min(max(1, steps), 3)

    def act(device: int) -> None:
        contact = POINTER_FLAG_DOWN | POINTER_FLAG_INRANGE | POINTER_FLAG_INCONTACT
        down = _touch_info(sx0, sy0, contact)
        if not InjectSyntheticPointerInput(device, ctypes.byref(down), 1):
            raise _PenInjectionFailed(ctypes.FormatError(ctypes.get_last_error()))
        move_flags = POINTER_FLAG_UPDATE | POINTER_FLAG_INRANGE | POINTER_FLAG_INCONTACT
        for index in range(1, steps + 1):
            time.sleep(0.002)
            ratio = index / steps
            x = int(sx0 + (sx1 - sx0) * ratio)
            y = int(sy0 + (sy1 - sy0) * ratio)
            move = _touch_info(x, y, move_flags)
            if not InjectSyntheticPointerInput(device, ctypes.byref(move), 1):
                raise _PenInjectionFailed(ctypes.FormatError(ctypes.get_last_error()))
        time.sleep(0.002)
        up = _touch_info(sx1, sy1, POINTER_FLAG_UP)
        if not InjectSyntheticPointerInput(device, ctypes.byref(up), 1):
            raise _PenInjectionFailed(ctypes.FormatError(ctypes.get_last_error()))

    try:
        _with_pointer_device(PT_TOUCH, act)
    finally:
        if prev_cursor is not None:
            # The OS processes promoted mouse moves slightly after injection;
            # settle, then restore twice to win the race (cua mouse.rs).
            try:
                set_cursor(*prev_cursor)
                time.sleep(0.012)
                set_cursor(*prev_cursor)
            except ForegroundError:
                pass


def target_visible_at_point(target: int, sx: int, sy: int) -> bool:
    """True when the target's root is the topmost window at the point —
    coordinate-routed injection would land on it, not an occluder."""
    top = user32.WindowFromPoint(wt.POINT(sx, sy))
    if not top:
        return False
    return _root_ancestor(top) == _root_ancestor(target)


class _NoActivateGuard:
    """WS_EX_NOACTIVATE on the target's root for the duration of one
    background actuation: Windows refuses to activate the window at all
    (click-activation, WM_MOUSEACTIVATE, WPF/XAML self-foregrounding) while
    it still receives the input. Drop clears only the bit this guard added."""

    def __init__(self, hwnd: int) -> None:
        self._root = _root_ancestor(hwnd)
        self._applied = False
        previous = _get_window_long(wt.HWND(self._root), GWL_EXSTYLE)
        if not (previous & WS_EX_NOACTIVATE):
            _set_window_long(wt.HWND(self._root), GWL_EXSTYLE, previous | WS_EX_NOACTIVATE)
            # Cross-process style writes can be denied by UIPI; confirm.
            self._applied = bool(
                _get_window_long(wt.HWND(self._root), GWL_EXSTYLE) & WS_EX_NOACTIVATE
            )

    def __enter__(self) -> "_NoActivateGuard":
        return self

    def __exit__(self, *_exc: object) -> None:
        if self._applied:
            current = _get_window_long(wt.HWND(self._root), GWL_EXSTYLE)
            _set_window_long(wt.HWND(self._root), GWL_EXSTYLE, current & ~WS_EX_NOACTIVATE)


def inject_click_screen(target: int, sx: int, sy: int, *, button: str, clicks: int) -> dict:
    """Background coordinate click via synthetic pen (inject.rs L307)."""
    if button == "middle":
        raise HarnessError("pen injection supports left/right buttons only")
    barrel = button == "right"
    if not user32.IsWindow(wt.HWND(target)):
        raise HarnessError(f"Window {target:#x} no longer exists")
    blocked = delivery.post_message_blocked_by_uipi(target)
    if blocked:
        raise delivery.refuse_background(target, delivery.MOUSE_CLICK, blocked)
    if not target_visible_at_point(target, sx, sy):
        raise delivery.refuse_background(
            target, delivery.MOUSE_CLICK,
            f"target is occluded at screen point ({sx},{sy}); raising it is "
            "forbidden in background delivery",
        )
    previous_foreground = current_foreground()
    with _NoActivateGuard(target):
        pen_taps(sx, sy, barrel=barrel, count=max(1, clicks))
    # WS_EX_NOACTIVATE cannot stop an Electron/Chromium click handler that
    # calls SetForegroundWindow(self) asynchronously — re-assert the user's
    # foreground twice to win that race.
    if previous_foreground and previous_foreground != target:
        force_foreground(previous_foreground)
        time.sleep(0.012)
        force_foreground(previous_foreground)
    return {"mode": "pen", "verified": False, "cloaked": False}


# --- transport 2: window messages --------------------------------------------


if hasattr(user32, "GetClassLongPtrW"):
    _get_class_long = user32.GetClassLongPtrW
else:  # 32-bit Python
    _get_class_long = user32.GetClassLongW

GCL_STYLE = -26
CS_DBLCLKS = 0x0008


def _class_wants_double_click(hwnd: int) -> bool:
    """Only classes registered with CS_DBLCLKS honour WM_*BUTTONDBLCLK;
    posting it elsewhere silently swallows the second click (cua mouse.rs)."""
    return bool(_get_class_long(wt.HWND(hwnd), GCL_STYLE) & CS_DBLCLKS)


def _deepest_child_at(root: int, sx: int, sy: int) -> int:
    """Walk to the deepest visible child under the screen point; a single
    ChildWindowFromPointEx hop stops at the first nesting level."""
    current = root
    flags = CWP_SKIPINVISIBLE | CWP_SKIPTRANSPARENT | CWP_SKIPDISABLED
    while True:
        cx, cy = screen_to_client(current, sx, sy)
        child = user32.ChildWindowFromPointEx(wt.HWND(current), wt.POINT(cx, cy), flags)
        if not child or child == current:
            return current
        current = child


def post_click_screen(root: int, sx: int, sy: int, *, button: str, clicks: int) -> dict:
    mk_flag, down_msg, dbl_msg, up_msg = _BUTTON_MESSAGES[button]
    target = _deepest_child_at(root, sx, sy)
    client_x, client_y = screen_to_client(target, sx, sy)
    wants_double = _class_wants_double_click(target)
    previous_foreground = current_foreground()
    target_root = _root_ancestor(target)
    # Posted messages are normally non-activating, but WebView hosts can call
    # SetForegroundWindow from their event handlers — hold the burst guarded.
    with _NoActivateGuard(target):
        for click_count in range(1, max(1, clicks) + 1):
            press = dbl_msg if (wants_double and click_count % 2 == 0) else down_msg
            # Hover first with no buttons held so hover state is correct.
            post_message(target, WM_MOUSEMOVE, 0, pack_lparam(client_x, client_y))
            post_message(target, press, mk_flag, pack_lparam(client_x, client_y))
            time.sleep(0.03)
            post_message(target, up_msg, 0, pack_lparam(client_x, client_y))
            if click_count < clicks:
                time.sleep(0.08)
        time.sleep(0.05)
    if (previous_foreground and previous_foreground != target_root
            and current_foreground() == target_root):
        force_foreground(previous_foreground)
        time.sleep(0.012)
        force_foreground(previous_foreground)
    return {"mode": "message", "verified": False, "target_hwnd": target}


class _GUITHREADINFO(ctypes.Structure):
    _fields_ = [
        ("cbSize", wt.DWORD),
        ("flags", wt.DWORD),
        ("hwndActive", wt.HWND),
        ("hwndFocus", wt.HWND),
        ("hwndCapture", wt.HWND),
        ("hwndMenuOwner", wt.HWND),
        ("hwndMoveSize", wt.HWND),
        ("hwndCaret", wt.HWND),
        ("rcCaret", wt.RECT),
    ]


_ENUM_CHILD_PROC = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)


def focused_descendant(parent: int) -> int:
    """Focused child across ALL UI threads under `parent`, or 0.

    Embedded editors (Scintilla in Notepad++, RichEdit in WordPad, WebView2
    renderers) keep their focused child on a different UI thread than the
    top-level frame; a single-thread GetGUIThreadInfo misses it and WM_CHAR
    posted to the frame silently no-ops (cua keyboard.rs). The deepest
    focused descendant wins when several threads hold focus."""
    parent_thread = user32.GetWindowThreadProcessId(wt.HWND(parent), None)
    if not parent_thread:
        return 0
    threads = [parent_thread]

    @_ENUM_CHILD_PROC
    def collect(child: int, _lparam: int) -> bool:
        thread = user32.GetWindowThreadProcessId(wt.HWND(child), None)
        if thread and thread not in threads:
            threads.append(thread)
        return True

    user32.EnumChildWindows(wt.HWND(parent), collect, 0)

    best_hwnd, best_depth = 0, -1
    for thread in threads:
        info = _GUITHREADINFO()
        info.cbSize = ctypes.sizeof(info)
        if not user32.GetGUIThreadInfo(thread, ctypes.byref(info)):
            continue
        focused = info.hwndFocus
        if (not focused or focused == parent
                or not user32.IsChild(wt.HWND(parent), wt.HWND(focused))):
            continue
        depth, current = 0, focused
        while current != parent and depth < 64:
            nxt = user32.GetParent(wt.HWND(current))
            if not nxt:
                break
            depth += 1
            current = nxt
        if current == parent and depth > best_depth:
            best_depth, best_hwnd = depth, focused
    return best_hwnd


def background_focus_window(hwnd: int) -> int:
    """The child window that would receive keys if the app were active."""
    return focused_descendant(hwnd) or hwnd


def _message_vk_pair(hwnd: int, vk: int, *, down_only: bool = False, up_only: bool = False) -> None:
    scan = scan_code(vk)
    if not up_only:
        post_message(hwnd, WM_KEYDOWN, vk, (scan << 16) | 1)
        time.sleep(0.01)
    if not down_only:
        post_message(hwnd, WM_KEYUP, vk, (scan << 16) | 0xC0000001)
        time.sleep(0.01)


def _utf16_code_units(char: str) -> list[int]:
    """One Unicode scalar → one or two UTF-16 code units (surrogate pair)."""
    units = char.encode("utf-16-le")
    return [units[index] | (units[index + 1] << 8) for index in range(0, len(units), 2)]


def post_type_text(hwnd: int, text: str) -> dict:
    target = background_focus_window(hwnd)
    previous_was_cr = False
    for char in text:
        if char in "\r\n":
            if char == "\n" and previous_was_cr:
                previous_was_cr = False
                continue
            _message_vk_pair(target, VK_RETURN)
            previous_was_cr = char == "\r"
            continue
        previous_was_cr = False
        if char == "\t":
            _message_vk_pair(target, VK_TAB)
            continue
        # WM_CHAR wParam is a UTF-16 code unit, not a byte: iterating the
        # encoded bytes posts NULs after ASCII and mangles non-ASCII text.
        for code_unit in _utf16_code_units(char):
            post_message(target, WM_CHAR, code_unit, 0)
        time.sleep(0.01)
    return {"mode": "message", "verified": False}


def post_key(hwnd: int, key: str) -> dict:
    base_vk, modifier_vks = parse_combo(key)
    target = background_focus_window(hwnd)
    for vk in modifier_vks:
        _message_vk_pair(target, vk, down_only=True)
    _message_vk_pair(target, base_vk)
    for vk in reversed(modifier_vks):
        _message_vk_pair(target, vk, up_only=True)
    return {"mode": "message", "verified": False}


def post_scroll(root: int, sx: int, sy: int, delta_y: int, delta_x: int = 0) -> dict:
    client_x, client_y = screen_to_client(root, sx, sy)
    target = child_window_at(root, client_x, client_y)
    # WM_MOUSEWHEEL/WM_MOUSEHWHEEL lParam carry SCREEN coordinates.
    for step in _split_scroll_delta(delta_y, WHEEL_DELTA):
        post_message(
            target, WM_MOUSEWHEEL,
            (step << 16) & 0xFFFF0000,
            pack_lparam(int(sx), int(sy)),
        )
        time.sleep(0.01)
    for step in _split_scroll_delta(delta_x, WHEEL_DELTA):
        post_message(
            target, WM_MOUSEHWHEEL,
            (step << 16) & 0xFFFF0000,
            pack_lparam(int(sx), int(sy)),
        )
        time.sleep(0.01)
    return {"mode": "message", "verified": False}


def post_drag(root: int, start: tuple[float, float], end: tuple[float, float], *, button: str, steps: int, duration: float) -> dict:
    mk_flag, down_msg, _dbl, up_msg = _BUTTON_MESSAGES[button]
    sx, sy = start
    client_x, client_y = screen_to_client(root, sx, sy)
    target = child_window_at(root, client_x, client_y)

    def post_at(message: int, x: float, y: float, wparam: int) -> None:
        cx, cy = screen_to_client(target, x, y)
        post_message(target, message, wparam, pack_lparam(cx, cy))

    post_at(WM_MOUSEMOVE, sx, sy, mk_flag)
    post_at(down_msg, sx, sy, mk_flag)
    time.sleep(0.03)
    for index in range(1, max(1, steps) + 1):
        ratio = index / max(1, steps)
        post_at(
            WM_MOUSEMOVE,
            sx + (end[0] - sx) * ratio,
            sy + (end[1] - sy) * ratio,
            mk_flag,
        )
        time.sleep(max(0.0, duration) / max(1, steps))
    post_at(up_msg, end[0], end[1], 0)
    return {"mode": "message", "verified": False}


# --- transport 3: foreground -------------------------------------------------
#
# "Foreground" is a placement contract (the target owns the screen and the
# focus), NOT a transport contract. SendInput is the default transport, but
# when the health probe finds it swallowed, each action falls back to the
# best transport that survives hook filtering: pen injection (coordinate-
# routed; the target is topmost so it always lands) or window messages
# (accepted far more broadly once the target really is foreground). Key
# combos have no honest fallback and refuse.

_SENDINPUT_SWALLOWED_HINT = (
    "SendInput is filtered by hook software on this machine "
    "(windows-harness doctor reports input_health.sendinput); "
)


def _confirmed_foreground(target: int, action: str) -> None:
    if _root_ancestor(current_foreground()) != _root_ancestor(target):
        raise ForegroundError(
            f"the target window is not the foreground before {action}; "
            "refusing to inject input that would land elsewhere"
        )


def foreground_click(target: int, sx: int, sy: int, *, button: str, clicks: int, cloak: bool, hold: bool = False) -> dict:
    down_flag, up_flag = _BUTTON_SENDINPUT[button]
    with cloaked_focus(target, cloak=cloak, hold=hold) as cloaked_ok:
        _confirmed_foreground(target, "click")
        if sendinput_healthy():
            set_cursor(sx, sy)
            for _ in range(max(1, clicks)):
                send_inputs((INPUT_MOUSE, {"dwFlags": down_flag}))
                time.sleep(0.03)
                send_inputs((INPUT_MOUSE, {"dwFlags": up_flag}))
                time.sleep(0.03)
            return {"mode": "foreground", "verified": True, "cloaked": cloaked_ok}
        # The target is topmost now; coordinate-routed pen injection lands.
        if button == "middle":
            raise ForegroundError(
                _SENDINPUT_SWALLOWED_HINT
                + "middle click has no pen mapping and no honest fallback"
            )
        pen_taps(sx, sy, barrel=button == "right", count=max(1, clicks))
        return {"mode": "foreground-pen", "verified": False, "cloaked": cloaked_ok}


def foreground_type_text(target: int, text: str, *, cloak: bool, hold: bool = False) -> dict:
    with cloaked_focus(target, cloak=cloak, hold=hold) as cloaked_ok:
        _confirmed_foreground(target, "typing")
        if sendinput_healthy():
            previous_was_cr = False
            for char in text:
                if char in "\r\n":
                    if char == "\n" and previous_was_cr:
                        previous_was_cr = False
                        continue
                    # Terminals and editors honour an Enter key event, not a raw
                    # Unicode packet (keyboard.rs send_text_synthesized).
                    send_inputs(
                        _keyboard_input(VK_RETURN),
                        _keyboard_input(VK_RETURN, up=True),
                    )
                    previous_was_cr = char == "\r"
                    continue
                previous_was_cr = False
                for code_unit in _utf16_code_units(char):
                    send_inputs(
                        (INPUT_KEYBOARD, {"wScan": code_unit, "dwFlags": KEYEVENTF_UNICODE}),
                        (INPUT_KEYBOARD, {"wScan": code_unit, "dwFlags": KEYEVENTF_UNICODE | KEYEVENTF_KEYUP}),
                    )
            return {"mode": "foreground", "verified": True, "cloaked": cloaked_ok}
        # Focused and foreground: posted WM_CHAR is accepted far more broadly
        # now than in the background, and it never carries the INJECTED flag.
        result = post_type_text(target, text)
        return {
            "mode": "foreground-message",
            "verified": False,
            "cloaked": cloaked_ok,
            "target_hwnd": result.get("target_hwnd"),
        }


def foreground_key(target: int, key: str, *, cloak: bool, hold: bool = False) -> dict:
    base_vk, modifier_vks = parse_combo(key)
    with cloaked_focus(target, cloak=cloak, hold=hold) as cloaked_ok:
        _confirmed_foreground(target, f"key {key!r}")
        if not sendinput_healthy():
            if modifier_vks:
                raise ForegroundError(
                    _SENDINPUT_SWALLOWED_HINT
                    + "key combos have no honest fallback (posted modifiers never "
                    "reach GetKeyState); type text with win.type() or remove the "
                    "filtering software"
                )
            # Bare keys are honest via posted messages once the target really
            # is foreground and focused (observed: CEF accepts posted
            # VK_RETURN/VK_BACK in this state while ignoring them in the
            # background); only combos depend on GetKeyState.
            result = post_key(target, key)
            return {
                "mode": "foreground-message",
                "verified": False,
                "cloaked": cloaked_ok,
                "target_hwnd": result.get("target_hwnd"),
            }
        events = [_keyboard_input(vk) for vk in modifier_vks]
        events += [_keyboard_input(base_vk), _keyboard_input(base_vk, up=True)]
        events += [_keyboard_input(vk, up=True) for vk in reversed(modifier_vks)]
        send_inputs(*events)
        return {"mode": "foreground", "verified": True, "cloaked": cloaked_ok}


def foreground_scroll(target: int, sx: int, sy: int, delta_y: int, delta_x: int = 0, *, cloak: bool, hold: bool = False) -> dict:
    with cloaked_focus(target, cloak=cloak, hold=hold) as cloaked_ok:
        _confirmed_foreground(target, "scroll")
        if sendinput_healthy():
            set_cursor(sx, sy)
            for step in _split_scroll_delta(delta_y, 3 * WHEEL_DELTA):
                send_inputs(
                    (INPUT_MOUSE, {"dwFlags": MOUSEEVENTF_WHEEL, "mouseData": step & 0xFFFFFFFF})
                )
                time.sleep(0.01)
            for step in _split_scroll_delta(delta_x, 3 * WHEEL_DELTA):
                send_inputs(
                    (INPUT_MOUSE, {"dwFlags": MOUSEEVENTF_HWHEEL, "mouseData": step & 0xFFFFFFFF})
                )
                time.sleep(0.01)
            return {"mode": "foreground", "verified": True, "cloaked": cloaked_ok}
        result = post_scroll(target, sx, sy, delta_y, delta_x)
        return {"mode": "foreground-message", "verified": False, "cloaked": cloaked_ok, **{k: v for k, v in result.items() if k == "target_hwnd"}}


def foreground_drag(target: int, start: tuple[float, float], end: tuple[float, float], *, button: str, steps: int, duration: float, cloak: bool, hold: bool = False) -> dict:
    down_flag, up_flag = _BUTTON_SENDINPUT[button]
    with cloaked_focus(target, cloak=cloak, hold=hold) as cloaked_ok:
        _confirmed_foreground(target, "drag")
        if sendinput_healthy():
            # MOUSEEVENTF_ABSOLUTE expects dx/dy normalized to 0..65535 against
            # the primary monitor (or the virtual screen with VIRTUALDESK).
            # The harness works in physical screen pixels, so absolute moves
            # would place the drag at the wrong spot once SendInput is used.
            # Park the cursor on the start point (SetCursorPos) and drive the
            # path with RELATIVE moves instead: correct on every monitor and
            # no coordinate-space conversion.
            set_cursor(start[0], start[1])
            send_inputs((INPUT_MOUSE, {"dwFlags": down_flag}))
            current = (start[0], start[1])
            for index in range(1, max(1, steps) + 1):
                ratio = index / max(1, steps)
                target_x = start[0] + (end[0] - start[0]) * ratio
                target_y = start[1] + (end[1] - start[1]) * ratio
                dx = int(round(target_x - current[0]))
                dy = int(round(target_y - current[1]))
                if dx or dy:
                    send_inputs((INPUT_MOUSE, {"dwFlags": MOUSEEVENTF_MOVE, "dx": dx, "dy": dy}))
                    current = (current[0] + dx, current[1] + dy)
                time.sleep(max(0.0, duration) / max(1, steps))
            # If the last interpolated step landed short of `end`, close the gap.
            dx_end = int(round(end[0] - current[0]))
            dy_end = int(round(end[1] - current[1]))
            if dx_end or dy_end:
                send_inputs(
                    (INPUT_MOUSE, {"dwFlags": MOUSEEVENTF_MOVE, "dx": dx_end, "dy": dy_end})
                )
            send_inputs((INPUT_MOUSE, {"dwFlags": up_flag}))
            return {"mode": "foreground", "verified": True, "cloaked": cloaked_ok}
        pen_drag(int(start[0]), int(start[1]), int(end[0]), int(end[1]), steps=steps, duration=duration)
        return {"mode": "foreground-pen", "verified": False, "cloaked": cloaked_ok}


# --- clipboard paste: the text route that survives swallowed SendInput ------
#
# Hook software eats injected KEY events, but nothing filters the clipboard.
# Setting CF_UNICODETEXT is a plain data handoff; only the paste *trigger*
# (Ctrl+V) needs input, and even that has a posted-message fallback. This is
# the honest answer for CEF/Electron text fields on machines where the
# health probe reports sendinput == "swallowed".

CF_UNICODETEXT = 13
GMEM_MOVEABLE = 0x0002

user32.OpenClipboard.argtypes = (wt.HWND,)
user32.GetClipboardData.restype = wt.HANDLE
user32.GetClipboardData.argtypes = (wt.UINT,)
user32.SetClipboardData.restype = wt.HANDLE
user32.SetClipboardData.argtypes = (wt.UINT, wt.HANDLE)
kernel32.GlobalAlloc.restype = wt.HANDLE
kernel32.GlobalAlloc.argtypes = (wt.UINT, ctypes.c_size_t)
kernel32.GlobalLock.restype = ctypes.c_void_p
kernel32.GlobalLock.argtypes = (wt.HANDLE,)
kernel32.GlobalUnlock.argtypes = (wt.HANDLE,)
kernel32.GlobalSize.restype = ctypes.c_size_t
kernel32.GlobalSize.argtypes = (wt.HANDLE,)


def set_clipboard_text(text: str) -> None:
    """Place Unicode text on the clipboard; retry while another app holds it."""
    data = text.encode("utf-16-le") + b"\x00\x00"
    for _attempt in range(60):
        if user32.OpenClipboard(None):
            break
        time.sleep(0.05)
    else:
        raise HarnessError("OpenClipboard failed: another process holds the clipboard")
    try:
        user32.EmptyClipboard()
        handle = kernel32.GlobalAlloc(GMEM_MOVEABLE, len(data))
        if not handle:
            raise HarnessError("GlobalAlloc failed for clipboard payload")
        locked = kernel32.GlobalLock(handle)
        if not locked:
            kernel32.GlobalFree(handle)
            raise HarnessError("GlobalLock failed for clipboard payload")
        try:
            ctypes.memmove(locked, data, len(data))
        finally:
            kernel32.GlobalUnlock(handle)
        if not user32.SetClipboardData(CF_UNICODETEXT, handle):
            kernel32.GlobalFree(handle)
            raise HarnessError(
                "SetClipboardData failed: "
                f"{ctypes.FormatError(ctypes.get_last_error())}"
            )
    finally:
        user32.CloseClipboard()


def get_clipboard_text() -> str | None:
    """Read back CF_UNICODETEXT; None when the clipboard holds no text."""
    for _attempt in range(60):
        if user32.OpenClipboard(None):
            break
        time.sleep(0.05)
    else:
        return None
    try:
        handle = user32.GetClipboardData(CF_UNICODETEXT)
        if not handle:
            return None
        locked = kernel32.GlobalLock(handle)
        if not locked:
            return None
        try:
            return ctypes.wstring_at(locked)
        finally:
            kernel32.GlobalUnlock(handle)
    finally:
        user32.CloseClipboard()


def _ctrl_v_events() -> list[tuple[int, dict]]:
    vk_c, vk_v = VK_CONTROL, ord("V")
    return [
        _keyboard_input(vk_c),
        _keyboard_input(vk_v),
        _keyboard_input(vk_v, up=True),
        _keyboard_input(vk_c, up=True),
    ]


def post_paste(target: int) -> dict:
    """WM_PASTE to the focused descendant: the standard edit message, honoured
    by classic controls AND CEF hosts. A posted Ctrl+V is NOT a substitute —
    its modifier never reaches GetKeyState, so CEF degrades it to a plain
    'v' keystroke (observed end-to-end on QQMusic)."""
    field = background_focus_window(target)
    post_message(field, WM_PASTE, 0, 0)
    return {"mode": "message", "verified": False, "target_hwnd": field}


def foreground_paste(target: int, *, cloak: bool, hold: bool = False) -> dict:
    with cloaked_focus(target, cloak=cloak, hold=hold) as cloaked_ok:
        _confirmed_foreground(target, "paste")
        if sendinput_healthy():
            send_inputs(*_ctrl_v_events())
            return {"mode": "foreground", "verified": True, "cloaked": cloaked_ok}
        result = post_paste(target)
        return {
            "mode": "foreground-message",
            "verified": False,
            "cloaked": cloaked_ok,
            "target_hwnd": result.get("target_hwnd"),
        }


# --- dispatchers (tools/impl_.rs click/type/hotkey/scroll orchestration) ------


def _check_delivery(delivery_mode: str) -> None:
    if delivery_mode not in ("background", "foreground"):
        raise HarnessError("delivery must be 'background' or 'foreground'")


def hover_screen(target: int, point: tuple[float, float], *, delivery_mode: str, hold: bool = False, dwell: float = 0.6) -> dict:
    """Hover the physical cursor over a point so tooltips/hover states fire.

    Hover is coordinate-routed, not focus-routed: the target does not need
    the foreground for its tooltip to appear, only to be unoccluded at the
    point. The cursor is left in place — moving it away dismisses the
    tooltip before anyone can read it.
    """
    _check_delivery(delivery_mode)
    sx, sy = int(point[0]), int(point[1])
    if delivery_mode == "foreground":
        if not target_visible_at_point(target, sx, sy):
            raise delivery.refuse_background(
                target, delivery.MOUSE_MOVE,
                f"target is occluded at screen point ({sx},{sy}); hover "
                "would land on another window",
            )
        if not sendinput_healthy():
            # Pen INRANGE without contact is a genuine hover and survives
            # hook filtering of SendInput.
            _pen_hover(sx, sy)
            return {"mode": "foreground-pen", "verified": False}
        set_cursor(sx, sy)
        time.sleep(max(0.0, dwell))
        return {"mode": "foreground", "verified": True}
    blocked = delivery.post_message_blocked_by_uipi(target)
    if blocked:
        raise delivery.refuse_background(target, delivery.MOUSE_MOVE, blocked)
    if delivery.would_be_silently_dropped(target, delivery.MOUSE_MOVE):
        raise delivery.refuse_background(target, delivery.MOUSE_MOVE)
    child = _deepest_child_at(target, sx, sy)
    cx, cy = screen_to_client(child, sx, sy)
    post_message(child, WM_MOUSEMOVE, 0, pack_lparam(cx, cy))
    return {"mode": "message", "verified": False, "target_hwnd": child}


def _pen_hover(sx: int, sy: int) -> None:
    """Synthetic pen in range but not in contact == hover, no cursor move."""
    hover = _pen_info(sx, sy, POINTER_FLAG_INRANGE | POINTER_FLAG_UPDATE, False)

    def act(device: int) -> None:
        if not InjectSyntheticPointerInput(device, ctypes.byref(hover), 1):
            raise _PenInjectionFailed(ctypes.FormatError(ctypes.get_last_error()))

    _with_pointer_device(PT_PEN, act)


def click_screen(target: int, point: tuple[float, float], *, button: str, clicks: int, delivery_mode: str, hold: bool = False) -> dict:
    _check_delivery(delivery_mode)
    sx, sy = int(point[0]), int(point[1])
    if delivery_mode == "foreground":
        return foreground_click(target, sx, sy, button=button, clicks=clicks, cloak=True, hold=hold)

    blocked = delivery.post_message_blocked_by_uipi(target)
    if blocked:
        raise delivery.refuse_background(target, delivery.MOUSE_CLICK, blocked)
    if delivery.would_be_silently_dropped(target, delivery.MOUSE_CLICK):
        # Coordinate-routed pen injection is the honest background actuator
        # for frameworks that drop posted clicks.
        try:
            return inject_click_screen(target, sx, sy, button=button, clicks=clicks)
        except HarnessError as exc:
            raise delivery.refuse_background(target, delivery.MOUSE_CLICK, str(exc)) from exc
    return post_click_screen(target, sx, sy, button=button, clicks=clicks)


def type_text(target: int, text: str, *, delivery_mode: str, hold: bool = False) -> dict:
    _check_delivery(delivery_mode)
    if delivery_mode == "foreground":
        return foreground_type_text(target, text, cloak=True, hold=hold)
    blocked = delivery.post_message_blocked_by_uipi(target)
    if blocked:
        raise delivery.refuse_background(target, delivery.TEXT_INPUT, blocked)
    if delivery.would_be_silently_dropped(target, delivery.TEXT_INPUT):
        raise delivery.refuse_background(target, delivery.TEXT_INPUT)
    return post_type_text(target, text)


def press_key(target: int, key: str, *, delivery_mode: str, hold: bool = False) -> dict:
    _check_delivery(delivery_mode)
    if delivery_mode == "foreground":
        return foreground_key(target, key, cloak=True, hold=hold)
    _base, modifiers = parse_combo(key)
    kind = delivery.KEY_COMBO if modifiers else delivery.KEYSTROKE
    blocked = delivery.post_message_blocked_by_uipi(target)
    if blocked:
        raise delivery.refuse_background(target, kind, blocked)
    if delivery.would_be_silently_dropped(target, kind):
        raise delivery.refuse_background(target, kind)
    return post_key(target, key)


def scroll(target: int, point: tuple[float, float], delta_y: int, delta_x: int = 0, *, delivery_mode: str, hold: bool = False) -> dict:
    _check_delivery(delivery_mode)
    sx, sy = int(point[0]), int(point[1])
    if delivery_mode == "foreground":
        return foreground_scroll(target, sx, sy, delta_y, delta_x, cloak=True, hold=hold)
    blocked = delivery.post_message_blocked_by_uipi(target)
    if blocked:
        raise delivery.refuse_background(target, delivery.MOUSE_SCROLL, blocked)
    if delivery.would_be_silently_dropped(target, delivery.MOUSE_SCROLL):
        raise delivery.refuse_background(target, delivery.MOUSE_SCROLL)
    return post_scroll(target, sx, sy, delta_y, delta_x)


def paste_text(target: int, *, delivery_mode: str, hold: bool = False) -> dict:
    """Paste the current clipboard content into the target window."""
    _check_delivery(delivery_mode)
    if delivery_mode == "foreground":
        return foreground_paste(target, cloak=True, hold=hold)
    blocked = delivery.post_message_blocked_by_uipi(target)
    if blocked:
        raise delivery.refuse_background(target, delivery.TEXT_INPUT, blocked)
    if delivery.would_be_silently_dropped(target, delivery.TEXT_INPUT):
        raise delivery.refuse_background(target, delivery.TEXT_INPUT)
    return post_paste(target)


def drag(target: int, start: tuple[float, float], end: tuple[float, float], *, button: str, steps: int, duration: float, delivery_mode: str, hold: bool = False) -> dict:
    _check_delivery(delivery_mode)
    if delivery_mode == "foreground":
        return foreground_drag(target, start, end, button=button, steps=steps, duration=duration, cloak=True, hold=hold)
    blocked = delivery.post_message_blocked_by_uipi(target)
    if blocked:
        raise delivery.refuse_background(target, delivery.MOUSE_CLICK, blocked)
    if delivery.is_wpf_target_window(target):
        # WPF's Wisp stylus stack only processes injected input while the
        # window is foreground, which background must not force (cua) — a
        # pen/touch drag here would do nothing but wiggle the user's cursor.
        raise delivery.refuse_background(
            target, delivery.MOUSE_CLICK,
            "WPF processes injected pointer input only while foreground; a "
            "background drag is undeliverable",
        )
    if delivery.would_be_silently_dropped(target, delivery.MOUSE_CLICK):
        # Coordinate-routed touch/pen routes by coordinate; occlusion still refuses.
        if not target_visible_at_point(target, int(start[0]), int(start[1])):
            raise delivery.refuse_background(
                target, delivery.MOUSE_CLICK,
                f"target is occluded at ({int(start[0])},{int(start[1])})",
            )
        previous_foreground = current_foreground()
        sx, sy = int(start[0]), int(start[1])
        ex, ey = int(end[0]), int(end[1])
        with _NoActivateGuard(target):
            if button == "left":
                # Touch from a standing digitizer never drags the user's cursor.
                touch_drag(sx, sy, ex, ey, steps=steps)
                mode = "touch"
            else:
                pen_drag(sx, sy, ex, ey, steps=steps, duration=duration)
                mode = "pen"
        if previous_foreground and previous_foreground != target:
            force_foreground(previous_foreground)
            time.sleep(0.012)
            force_foreground(previous_foreground)
        return {"mode": mode, "verified": False, "cloaked": False}
    return post_drag(target, start, end, button=button, steps=steps, duration=duration)
