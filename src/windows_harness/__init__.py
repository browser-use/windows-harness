"""Windows Harness: six primitives, the whole desktop."""

from .capture import HarnessError
from .delivery import BackgroundUnavailable
from .inject import ForegroundError
from .windows import FocusChangedError, Windows

__all__ = [
    "Windows",
    "HarnessError",
    "FocusChangedError",
    "ForegroundError",
    "BackgroundUnavailable",
]
