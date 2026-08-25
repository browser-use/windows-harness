"""Lazy bridge to the Browser Harness Python SDK."""

from __future__ import annotations

import os
from types import ModuleType
from typing import Any


class BrowserHarness:
    """Expose Browser Harness helpers without connecting until first use."""

    def __init__(self, *, name: str | None = None, wait: float = 60.0) -> None:
        self.name = name
        self.wait = wait
        self._helpers: ModuleType | None = None

    def connect(self) -> BrowserHarness:
        if self._helpers is not None:
            return self

        if self.name:
            os.environ["BU_NAME"] = self.name

        try:
            from browser_harness import admin, helpers
        except ImportError as exc:  # pragma: no cover - packaging failure
            raise RuntimeError(
                "Browser Harness is unavailable. Reinstall windows-harness so its "
                "browser dependency is present."
            ) from exc

        admin.ensure_daemon(wait=self.wait, name=self.name)
        if self.name:
            helpers.NAME = self.name
        self._helpers = helpers
        return self

    def __getattr__(self, name: str) -> Any:
        if name.startswith("_"):
            raise AttributeError(name)
        self.connect()
        assert self._helpers is not None
        try:
            return getattr(self._helpers, name)
        except AttributeError as exc:
            raise AttributeError(f"Browser Harness has no helper {name!r}") from exc

    def __repr__(self) -> str:
        status = "connected" if self._helpers is not None else "lazy"
        suffix = f", name={self.name!r}" if self.name else ""
        return f"BrowserHarness({status}{suffix})"
