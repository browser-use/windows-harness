"""Send pointer updates to the disposable overlay helper process."""

from __future__ import annotations

import atexit
import json
import os
import subprocess
import sys
from typing import Any


class LivePointerOverlay:
    """Parent-side handle for the click-through virtual pointer."""

    def __init__(self) -> None:
        self._process: subprocess.Popen[str] | None = None
        self._visible = True
        atexit.register(self.close)

    @property
    def visible(self) -> bool:
        return self._visible

    @property
    def running(self) -> bool:
        return self._process is not None and self._process.poll() is None

    def move(self, x: float, y: float, *, duration: float = 0.16) -> None:
        self._visible = True
        self._send(
            {
                "cmd": "move",
                "x": float(x),
                "y": float(y),
                "duration": max(0.0, float(duration)),
            }
        )

    def show(self, x: float, y: float) -> None:
        self._visible = True
        self._send({"cmd": "show", "x": float(x), "y": float(y)})

    def hide(self) -> None:
        self._visible = False
        if self.running:
            self._send({"cmd": "hide"}, start=False)

    def click(self) -> None:
        if self._visible:
            self._send({"cmd": "click"})

    def close(self) -> None:
        process = self._process
        self._process = None
        if process is None or process.poll() is not None:
            return
        try:
            assert process.stdin is not None
            process.stdin.write('{"cmd": "close"}\n')
            process.stdin.flush()
            try:
                process.wait(timeout=1.5)
            except subprocess.TimeoutExpired:
                process.kill()
        except (OSError, ValueError):
            process.kill()

    # --- internals ---------------------------------------------------------

    def _send(self, payload: dict[str, Any], *, start: bool = True) -> None:
        if not self.running and start:
            self._start()
        process = self._process
        if process is None or process.stdin is None:
            return
        try:
            process.stdin.write(json.dumps(payload, separators=(",", ":")) + "\n")
            process.stdin.flush()
        except (OSError, ValueError):
            self._process = None
            try:
                process.kill()
            except OSError:
                pass

    def _start(self) -> None:
        # Spawning the Tk renderer costs ~0.5 s; agents that want minimum
        # latency per action can opt out without touching any other behaviour.
        if os.environ.get("WINDOWS_HARNESS_OVERLAY", "").strip().casefold() in (
            "off", "0", "false", "no",
        ):
            return
        self._process = subprocess.Popen(
            [sys.executable, "-m", "windows_harness.overlay_helper"],
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
        )
