"""Click-through virtual pointer renderer.

A disposable child process owning one layered, transparent, non-activating
Tk window. Reads JSON commands on stdin:

    {"cmd": "move", "x": 100, "y": 200, "duration": 0.16}
    {"cmd": "show", "x": 100, "y": 200}
    {"cmd": "hide"}
    {"cmd": "click"}
    {"cmd": "close"}

The physical cursor is never touched; this window only draws.
"""

from __future__ import annotations

import base64
import io
import json
import queue
import sys
import threading
import tkinter as tk

from .capture import ensure_dpi_awareness
from .pointer import POINTER_HEIGHT, POINTER_HOTSPOT, POINTER_WIDTH, pointer_points


def _arrow_image(*, pressed: bool) -> bytes:
    from PIL import Image, ImageDraw

    scale = 1.0
    points = [
        (x * scale, y * scale)
        for x, y in pointer_points(pressed=pressed)
    ]
    image = Image.new("RGBA", (int(POINTER_WIDTH), int(POINTER_HEIGHT)), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    shadow = [(x + 2, y + 2) for x, y in points]
    draw.polygon(shadow, fill=(0, 0, 0, 70))
    draw.polygon(points, fill=(10, 10, 10, 255), outline=(255, 255, 255, 245), width=2)
    draw.line([*points, points[0]], fill=(0, 0, 0, 230), width=1, joint="curve")
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


class PointerOverlay:
    def __init__(self) -> None:
        ensure_dpi_awareness()
        self._root = tk.Tk()
        self._root.overrideredirect(True)
        self._root.attributes("-topmost", True)
        self._root.config(bg="black")
        try:
            self._root.attributes("-transparentcolor", "#010101")
            background = "#010101"
        except tk.TclError:
            background = "black"
        self._canvas = tk.Canvas(
            self._root,
            width=int(POINTER_WIDTH),
            height=int(POINTER_HEIGHT),
            bg=background,
            highlightthickness=0,
            bd=0,
        )
        self._canvas.pack()
        self._normal = tk.PhotoImage(
            data=base64.b64encode(_arrow_image(pressed=False)), master=self._root
        )
        self._pressed = tk.PhotoImage(
            data=base64.b64encode(_arrow_image(pressed=True)), master=self._root
        )
        self._sprite = self._canvas.create_image(0, 0, anchor="nw", image=self._normal)
        self._position = (0.0, 0.0)
        self._animation_id: str | None = None
        self._press_id: str | None = None
        self._root.update_idletasks()
        self._make_click_through()

    def _make_click_through(self) -> None:
        import ctypes

        GWL_EXSTYLE = -20
        WS_EX_LAYERED = 0x80000
        WS_EX_TRANSPARENT = 0x20
        WS_EX_NOACTIVATE = 0x8000000
        WS_EX_TOOLWINDOW = 0x80
        # winfo_id() returns the Tk inner child; the ex-style must land on the
        # actual top-level wrapper for click-through to take effect.
        hwnd = self._root.winfo_id()
        wrapper = ctypes.windll.user32.GetParent(hwnd)
        target = wrapper or hwnd
        style = ctypes.windll.user32.GetWindowLongW(target, GWL_EXSTYLE)
        ctypes.windll.user32.SetWindowLongW(
            target,
            GWL_EXSTYLE,
            style | WS_EX_LAYERED | WS_EX_TRANSPARENT | WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW,
        )

    # --- animation ---------------------------------------------------------

    def move(self, x: float, y: float, duration: float = 0.16) -> None:
        if self._animation_id is not None:
            self._root.after_cancel(self._animation_id)
            self._animation_id = None
        start_x, start_y = self._position
        distance = max(abs(x - start_x), abs(y - start_y))
        if duration <= 0 or distance < 1:
            self._place(x, y)
            return
        steps = max(2, min(24, int(distance / 12)))
        interval = max(8, int(duration * 1000 / steps))

        def step(index: int = 0) -> None:
            ratio = (index + 1) / steps
            current = (
                start_x + (x - start_x) * ratio,
                start_y + (y - start_y) * ratio,
            )
            self._place(*current)
            if index + 1 < steps:
                self._animation_id = self._root.after(interval, step, index + 1)
            else:
                self._animation_id = None

        step()

    def _place(self, x: float, y: float) -> None:
        self._position = (float(x), float(y))
        hot_x, hot_y = POINTER_HOTSPOT
        left = int(round(x - hot_x))
        top = int(round(y - hot_y))
        self._root.geometry(f"+{left}+{top}")

    def show(self, x: float, y: float) -> None:
        self._place(x, y)
        self._root.deiconify()

    def hide(self) -> None:
        self._root.withdraw()

    def click(self) -> None:
        if self._press_id is not None:
            return
        self._canvas.itemconfig(self._sprite, image=self._pressed)

        def release() -> None:
            self._canvas.itemconfig(self._sprite, image=self._normal)
            self._press_id = None

        self._press_id = self._root.after(110, release)

    def run(self) -> None:
        self._root.withdraw()  # hidden until the first show/move

        # Reading stdin on the UI thread would park the Tk event loop inside a
        # blocking readline between commands: every after() callback (move
        # animation steps, the click-release timer) freezes mid-flight. A
        # reader thread feeds a queue; the loop drains it on a short timer.
        lines: queue.Queue[str | None] = queue.Queue()

        def read_stdin() -> None:
            for line in sys.stdin:
                lines.put(line)
            lines.put(None)  # EOF: parent closed the pipe or exited

        threading.Thread(target=read_stdin, daemon=True).start()

        def pump_stdin() -> None:
            while True:
                try:
                    line = lines.get_nowait()
                except queue.Empty:
                    self._root.after(5, pump_stdin)
                    return
                if line is None:
                    self._root.after(10, self._root.destroy)
                    return
                try:
                    command = json.loads(line)
                    self._handle(command)
                except (json.JSONDecodeError, KeyError):
                    pass

        self._root.after(5, pump_stdin)
        self._root.mainloop()

    def _handle(self, command: dict) -> None:
        name = command["cmd"]
        if name == "move":
            self.show(float(command["x"]), float(command["y"]))
            self.move(
                float(command["x"]), float(command["y"]),
                float(command.get("duration", 0.16)),
            )
        elif name == "show":
            self.show(float(command["x"]), float(command["y"]))
        elif name == "hide":
            self.hide()
        elif name == "click":
            self.click()
        elif name == "close":
            self._root.after(10, self._root.destroy)


def main() -> None:
    PointerOverlay().run()


if __name__ == "__main__":
    main()
