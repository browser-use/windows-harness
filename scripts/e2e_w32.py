"""Pure Win32 target: logs every received message to a file.

Isolates whether injected input reaches a minimal Win32 window at all,
with no framework in between. Clicks on the native BUTTON child post
WM_COMMAND to the parent, also logged.
"""

import ctypes
import ctypes.wintypes as wt
import sys
import threading

LOG = r"E:\windows_tmp\e2e_w32.log"

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32")

WM_APP_LOG_FIRST = 0x8000  # WM_APP


def write(line: str) -> None:
    with open(LOG, "a", encoding="utf-8") as fh:
        fh.write(line + "\n")


WNDPROC = ctypes.WINFUNCTYPE(
    ctypes.c_longlong, wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM
)
clicked = {"n": 0}


def wndproc(hwnd, msg, wparam, lparam):
    names = {
        0x0005: "WM_SIZE",
        0x0200: "WM_MOUSEMOVE",
        0x0201: "WM_LBUTTONDOWN",
        0x0202: "WM_LBUTTONUP",
        0x0084: "",
        0x0085: "",
        0x0112: "",
        0x0211: "",
    }
    name = names.get(msg)
    if name:
        write(f"hwnd={hwnd:#x} {name}")
    if msg == 0x0111 and (wparam >> 16) == 0 and (wparam & 0xFFFF) == 2001:  # BN_CLICKED id 2001
        clicked["n"] += 1
        write(f"WM_COMMAND BN_CLICKED #{clicked['n']}")
        user32.SetWindowTextW(wt.HWND(hwnd), f"W32-TEST #{clicked['n']}")
    if msg == 0x0010:  # WM_CLOSE
        user32.PostQuitMessage(0)
    return user32.DefWindowProcW(wt.HWND(hwnd), msg, wparam, lparam)


_wndproc_ref = WNDPROC(wndproc)


class WNDCLASSW(ctypes.Structure):
    _fields_ = (
        ("style", wt.UINT),
        ("lpfnWndProc", WNDPROC),
        ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int),
        ("hInstance", wt.HINSTANCE),
        ("hIcon", wt.HICON),
        ("hCursor", ctypes.c_void_p),
        ("hbrBackground", ctypes.c_void_p),
        ("lpszMenuName", wt.LPCWSTR),
        ("lpszClassName", wt.LPCWSTR),
    )


def run() -> None:
    user32.CreateWindowExW.argtypes = (
        wt.DWORD, wt.LPCWSTR, wt.LPCWSTR, wt.DWORD,
        ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
        wt.HWND, wt.HMENU, wt.HINSTANCE, wt.LPVOID,
    )
    user32.CreateWindowExW.restype = wt.HWND
    hinstance = kernel32.GetModuleHandleW(None)
    classname = "WH_W32_TEST"
    wc = WNDCLASSW()
    wc.hInstance = wt.HINSTANCE(hinstance)
    wc.lpszClassName = classname
    wc.lpfnWndProc = _wndproc_ref
    wc.hCursor = user32.LoadCursorW(None, 32512)
    wc.hbrBackground = ctypes.c_void_p(6)  # COLOR_BTNFACE+1
    atom = user32.RegisterClassW(ctypes.byref(wc))
    if not atom:
        write(f"RegisterClassW failed err={ctypes.get_last_error()}")
        return

    hwnd = user32.CreateWindowExW(
        0, classname, "W32-TEST",
        0x10CF0000,  # WS_OVERLAPPEDWINDOW | WS_VISIBLE
        700, 300, 420, 200, None, None, hinstance, None,
    )
    err_main = ctypes.get_last_error()
    btn = None
    if hwnd:
        btn = user32.CreateWindowExW(
            0, "BUTTON", "GO",
            0x50010000,  # WS_CHILD | WS_VISIBLE | BS_PUSHBUTTON
            130, 80, 140, 44, wt.HWND(hwnd), wt.HMENU(2001), hinstance, None,
        )
        err_btn = ctypes.get_last_error()
    else:
        err_btn = "-"
    write(f"started hwnd={hwnd} btn={btn} err_main={err_main} err_btn={err_btn}")

    msg = wt.MSG()
    while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
        user32.TranslateMessage(ctypes.byref(msg))
        user32.DispatchMessageW(ctypes.byref(msg))
    write("quit")


if __name__ == "__main__":
    try:
        run()
    except Exception as exc:  # noqa: BLE001
        write(f"ERROR {exc}")
