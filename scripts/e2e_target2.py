"""E2E target v2: DPI-aware, click observability through a log file."""

import ctypes
import sys
import tkinter as tk

try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
except Exception:
    ctypes.windll.user32.SetProcessDPIAware()

LOG = r"E:\windows_tmp\e2e_clicks.log"
mode = sys.argv[1] if len(sys.argv) > 1 else "manual"

root = tk.Tk()
root.title("WH2-TEST")
root.geometry("460x220+80+80")

entry = tk.Entry(root, font=("Consolas", 14))
entry.pack(pady=12, fill="x", padx=12)

status = tk.Label(root, text="ready:" + mode, font=("Consolas", 12))
status.pack()

counter = {"n": 0}


def log(event: str) -> None:
    counter["n"] += 1
    with open(LOG, "a", encoding="utf-8") as fh:
        fh.write(f"{event} #{counter['n']}\n")
    root.title(f"WH2-TEST #{counter['n']}")
    status.config(text=f"{event} entry={entry.get()!r}")


def on_go() -> None:
    log("go")


go = tk.Button(root, text="GO", font=("Consolas", 12), width=10, command=on_go)
go.pack(pady=12)
root.bind_all("<Button-1>", lambda e: log(f"any<{e.widget.winfo_class()}>"))
root.bind_all("<Key>", lambda e: log(f"key<{e.keysym}> entry={entry.get()!r}"))

root.mainloop()
