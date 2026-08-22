"""A throwaway tkinter target for end-to-end harness verification.

The window title doubles as the assertion surface: every successful button
click appends ``#N`` to it, so the test program can verify actions
semantically through UIA/window titles instead of trusting return values.
"""

import sys
import tkinter as tk

mode = sys.argv[1] if len(sys.argv) > 1 else "manual"

root = tk.Tk()
root.title("WH-TEST")
root.geometry("460x220+80+80")

entry = tk.Entry(root, font=("Consolas", 14))
entry.pack(pady=12, fill="x", padx=12)

status = tk.Label(root, text="ready:" + mode, font=("Consolas", 12))
status.pack()

counter = {"n": 0}


def on_go() -> None:
    counter["n"] += 1
    root.title(f"WH-TEST #{counter['n']}")
    status.config(text="clicked with entry=" + repr(entry.get()))


go = tk.Button(root, text="GO", font=("Consolas", 12), width=10, command=on_go)
go.pack(pady=12)

root.mainloop()
