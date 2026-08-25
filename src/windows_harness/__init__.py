"""Windows Harness: six primitives, the whole desktop."""

from .browser import BrowserHarness
from .capture import HarnessError
from .delivery import BackgroundUnavailable
from .inject import ForegroundError
from .windows import FocusChangedError, Windows

__all__ = [
    "Windows",
    "BrowserHarness",
    "HarnessError",
    "FocusChangedError",
    "ForegroundError",
    "BackgroundUnavailable",
]
