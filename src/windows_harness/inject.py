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
    for slot, (kind, fields) in zip(array, inputs, strict=True):
        slot.type = kind
        for name, value in fields.items():
            setattr(slot, name, value)
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
    return False


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


class _PROCESSENTRY32W(ctypes.Structure):
    _fields_ = (
        ("dwSize", wt.DWORD),
        ("cntUsage", wt.DWORD),
        ("th32ProcessID", wt.DWORD),
        ("th32DefaultHeapID", ctypes.c_size_t),
        ("th32ModuleID", wt.DWORD),
        ("cntThreads", wt.DWORD),
        ("th32ParentProcessID", wt.DWORD),
        ("pcPriClassBase", wt.LONG),
        ("dwFlags", wt.DWORD),
        ("szExeFile", wt.WCHAR * 260),
    )


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


@contextmanager
def cloaked_focus(hwnd: int, *, cloak: bool = True):
    """Temporarily own the desktop on behalf of one background window.

    Cloaks the target when the OS allows so the takeover is invisible, then
    restores the previous foreground window and cursor position. Yields
    whether cloaking actually happened — callers report it honestly.
    """
    previous_foreground = current_foreground()
    previous_cursor = get_cursor()
    was_minimized = bool(user32.IsIconic(hwnd))
    cloaked_ok = set_cloak(hwnd, True) if cloak else False
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
        yield cloaked_ok
    finally:
        # Give the foreground back BEFORE touching the target's placement so
        # the user never sees a focus jump.
        if previous_foreground and previous_foreground != hwnd:
            try:
                force_foreground(previous_foreground)
            except HarnessError:
                pass
        if was_minimized:
            user32.ShowWindow(hwnd, SW_MINIMIZE)
            time.sleep(0.02)
        if cloaked_ok:
            # Only drop the journal entry once the window is veribly visible
            # again; a failed restore stays recoverable at next startup.
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


class _POINTER_TYPE_INFO_UNION(ctypes.Union):
    _fields_ = (("penInfo", _POINTER_PEN_INFO),)


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


_pen_device: int | None = None


def _acquire_pen_device() -> int:
    """One shared synthetic pen serves every action; creating a virtual HID
    stack is PnP work, and rapid create/destroy cycles fail outright in quick
    succession (cua #1984)."""
    global _pen_device
    if _pen_device:
        return _pen_device
    device = CreateSyntheticPointerDevice(PT_PEN, 1, _POINTER_FEEDBACK_DEFAULT)
    if not device:
        raise HarnessError(
            f"CreateSyntheticPointerDevice failed: {ctypes.FormatError(ctypes.get_last_error())}"
        )
    _pen_device = device
    return device


def _discard_pen_device(device: int) -> None:
    """Destroy a possibly-wedged device so the next acquire builds a fresh one."""
    global _pen_device
    if _pen_device == device:
        _pen_device = None
    DestroySyntheticPointerDevice(device)


def _destroy_cached_pen_device() -> None:
    global _pen_device
    if _pen_device:
        DestroySyntheticPointerDevice(_pen_device)
        _pen_device = None


atexit.register(_destroy_cached_pen_device)


def _with_pen_device(act: Callable[[int], None]) -> None:
    """Run act(device); a wedged shared device is destroyed and the action
    retried once on a fresh one before giving up."""
    for attempt in (1, 2):
        device = _acquire_pen_device()
        try:
            act(device)
            return
        except _PenInjectionFailed as exc:
            _discard_pen_device(device)
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

    _with_pen_device(act)


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

    _with_pen_device(act)


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


def post_click_screen(root: int, sx: int, sy: int, *, button: str, clicks: int) -> dict:
    mk_flag, down_msg, dbl_msg, up_msg = _BUTTON_MESSAGES[button]
    client_x, client_y = screen_to_client(root, sx, sy)
    target = child_window_at(root, client_x, client_y)
    if target != root:
        client_x, client_y = screen_to_client(target, sx, sy)

    post_message(target, WM_MOUSEMOVE, mk_flag, pack_lparam(client_x, client_y))
    for click_count in range(1, max(1, clicks) + 1):
        down = dbl_msg if click_count > 1 else down_msg
        post_message(target, down, mk_flag, pack_lparam(client_x, client_y))
        time.sleep(0.03)
        post_message(target, up_msg, 0, pack_lparam(client_x, client_y))
        time.sleep(0.03)
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


def background_focus_window(hwnd: int) -> int:
    """The child window that would receive keys if the app were active."""
    thread_id = user32.GetWindowThreadProcessId(hwnd, None)
    info = _GUITHREADINFO()
    info.cbSize = ctypes.sizeof(info)
    if thread_id and user32.GetGUIThreadInfo(thread_id, ctypes.byref(info)):
        return info.hwndFocus or hwnd
    return hwnd


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


def foreground_click(target: int, sx: int, sy: int, *, button: str, clicks: int, cloak: bool) -> dict:
    down_flag, up_flag = _BUTTON_SENDINPUT[button]
    with cloaked_focus(target, cloak=cloak) as cloaked_ok:
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


def foreground_type_text(target: int, text: str, *, cloak: bool) -> dict:
    with cloaked_focus(target, cloak=cloak) as cloaked_ok:
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


def foreground_key(target: int, key: str, *, cloak: bool) -> dict:
    base_vk, modifier_vks = parse_combo(key)
    with cloaked_focus(target, cloak=cloak) as cloaked_ok:
        _confirmed_foreground(target, f"key {key!r}")
        if not sendinput_healthy():
            raise ForegroundError(
                _SENDINPUT_SWALLOWED_HINT
                + "key combos have no honest fallback (posted modifiers never "
                "reach GetKeyState); type text with win.type() or remove the "
                "filtering software"
            )
        events = [_keyboard_input(vk) for vk in modifier_vks]
        events += [_keyboard_input(base_vk), _keyboard_input(base_vk, up=True)]
        events += [_keyboard_input(vk, up=True) for vk in reversed(modifier_vks)]
        send_inputs(*events)
        return {"mode": "foreground", "verified": True, "cloaked": cloaked_ok}


def foreground_scroll(target: int, sx: int, sy: int, delta_y: int, delta_x: int = 0, *, cloak: bool) -> dict:
    with cloaked_focus(target, cloak=cloak) as cloaked_ok:
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


def foreground_drag(target: int, start: tuple[float, float], end: tuple[float, float], *, button: str, steps: int, duration: float, cloak: bool) -> dict:
    down_flag, up_flag = _BUTTON_SENDINPUT[button]
    move_flag = MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE
    with cloaked_focus(target, cloak=cloak) as cloaked_ok:
        _confirmed_foreground(target, "drag")
        if sendinput_healthy():
            send_inputs((INPUT_MOUSE, {"dwFlags": down_flag}))
            for index in range(1, max(1, steps) + 1):
                ratio = index / max(1, steps)
                send_inputs(
                    (INPUT_MOUSE, {"dwFlags": move_flag, "dx": int(start[0] + (end[0] - start[0]) * ratio),
                                   "dy": int(start[1] + (end[1] - start[1]) * ratio)})
                )
                time.sleep(max(0.0, duration) / max(1, steps))
            send_inputs((INPUT_MOUSE, {"dwFlags": up_flag}))
            return {"mode": "foreground", "verified": True, "cloaked": cloaked_ok}
        pen_drag(int(start[0]), int(start[1]), int(end[0]), int(end[1]), steps=steps, duration=duration)
        return {"mode": "foreground-pen", "verified": False, "cloaked": cloaked_ok}


# --- dispatchers (tools/impl_.rs click/type/hotkey/scroll orchestration) ------


def _check_delivery(delivery_mode: str) -> None:
    if delivery_mode not in ("background", "foreground"):
        raise HarnessError("delivery must be 'background' or 'foreground'")


def click_screen(target: int, point: tuple[float, float], *, button: str, clicks: int, delivery_mode: str) -> dict:
    _check_delivery(delivery_mode)
    sx, sy = int(point[0]), int(point[1])
    if delivery_mode == "foreground":
        return foreground_click(target, sx, sy, button=button, clicks=clicks, cloak=True)

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


def type_text(target: int, text: str, *, delivery_mode: str) -> dict:
    _check_delivery(delivery_mode)
    if delivery_mode == "foreground":
        return foreground_type_text(target, text, cloak=True)
    blocked = delivery.post_message_blocked_by_uipi(target)
    if blocked:
        raise delivery.refuse_background(target, delivery.TEXT_INPUT, blocked)
    if delivery.would_be_silently_dropped(target, delivery.TEXT_INPUT):
        raise delivery.refuse_background(target, delivery.TEXT_INPUT)
    return post_type_text(target, text)


def press_key(target: int, key: str, *, delivery_mode: str) -> dict:
    _check_delivery(delivery_mode)
    if delivery_mode == "foreground":
        return foreground_key(target, key, cloak=True)
    _base, modifiers = parse_combo(key)
    kind = delivery.KEY_COMBO if modifiers else delivery.KEYSTROKE
    blocked = delivery.post_message_blocked_by_uipi(target)
    if blocked:
        raise delivery.refuse_background(target, kind, blocked)
    if delivery.would_be_silently_dropped(target, kind):
        raise delivery.refuse_background(target, kind)
    return post_key(target, key)


def scroll(target: int, point: tuple[float, float], delta_y: int, delta_x: int = 0, *, delivery_mode: str) -> dict:
    _check_delivery(delivery_mode)
    sx, sy = int(point[0]), int(point[1])
    if delivery_mode == "foreground":
        return foreground_scroll(target, sx, sy, delta_y, delta_x, cloak=True)
    blocked = delivery.post_message_blocked_by_uipi(target)
    if blocked:
        raise delivery.refuse_background(target, delivery.MOUSE_SCROLL, blocked)
    if delivery.would_be_silently_dropped(target, delivery.MOUSE_SCROLL):
        raise delivery.refuse_background(target, delivery.MOUSE_SCROLL)
    return post_scroll(target, sx, sy, delta_y, delta_x)


def drag(target: int, start: tuple[float, float], end: tuple[float, float], *, button: str, steps: int, duration: float, delivery_mode: str) -> dict:
    _check_delivery(delivery_mode)
    if delivery_mode == "foreground":
        return foreground_drag(target, start, end, button=button, steps=steps, duration=duration, cloak=True)
    blocked = delivery.post_message_blocked_by_uipi(target)
    if blocked:
        raise delivery.refuse_background(target, delivery.MOUSE_CLICK, blocked)
    if delivery.would_be_silently_dropped(target, delivery.MOUSE_CLICK):
        # Pen drag routes by coordinate; occlusion still refuses.
        if not target_visible_at_point(target, int(start[0]), int(start[1])):
            raise delivery.refuse_background(
                target, delivery.MOUSE_CLICK,
                f"target is occluded at ({int(start[0])},{int(start[1])})",
            )
        previous_foreground = current_foreground()
        sx, sy = int(start[0]), int(start[1])
        ex, ey = int(end[0]), int(end[1])
        with _NoActivateGuard(target):
            pen_drag(sx, sy, ex, ey, steps=steps, duration=duration)
        if previous_foreground and previous_foreground != target:
            force_foreground(previous_foreground)
            time.sleep(0.012)
            force_foreground(previous_foreground)
        return {"mode": "pen", "verified": False, "cloaked": False}
    return post_drag(target, start, end, button=button, steps=steps, duration=duration)
