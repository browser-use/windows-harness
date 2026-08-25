from __future__ import annotations

import sys
from types import ModuleType

from windows_harness.browser import BrowserHarness


def test_browser_harness_connects_lazily_and_only_once(monkeypatch) -> None:
    calls = []
    package = ModuleType("browser_harness")
    admin = ModuleType("browser_harness.admin")
    helpers = ModuleType("browser_harness.helpers")

    def ensure_daemon(*, wait, name):
        calls.append((wait, name))

    def page_info():
        return {"url": "https://example.com"}

    admin.ensure_daemon = ensure_daemon
    helpers.page_info = page_info
    package.admin = admin
    package.helpers = helpers
    monkeypatch.setitem(sys.modules, "browser_harness", package)

    browser = BrowserHarness(name="release-test", wait=3)
    assert "lazy" in repr(browser)
    assert browser.page_info() == {"url": "https://example.com"}
    assert browser.page_info() == {"url": "https://example.com"}
    assert calls == [(3, "release-test")]
    assert helpers.NAME == "release-test"
    assert "connected" in repr(browser)
