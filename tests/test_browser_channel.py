"""Choosing an installed browser instead of Playwright's own Chromium.

The run machine is on macOS 13, where Playwright no longer ships Chromium
("Playwright does not support chromium on mac13-arm64", 2026-10-21). Its own
Google Chrome works, so the browser became a setting.
"""

from __future__ import annotations

import sys
import types

from txtav import navigator
from txtav.config import Settings


def test_the_default_is_playwrights_own_chromium():
    assert Settings.model_fields["browser_channel"].default == ""


def test_the_channel_reaches_the_launch(monkeypatch, tmp_path):
    seen = {}

    class Context:
        pages = []

        def new_page(self):
            return types.SimpleNamespace(set_default_timeout=lambda ms: None)

        def close(self):
            pass

    class Chromium:
        def launch_persistent_context(self, profile, **kwargs):
            seen.update(kwargs)
            return Context()

    class Playwright:
        chromium = Chromium()

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    fake = types.ModuleType("playwright.sync_api")
    fake.sync_playwright = lambda: Playwright()
    monkeypatch.setitem(sys.modules, "playwright.sync_api", fake)

    with navigator.browser_session(tmp_path, 45, channel="chrome"):
        pass
    assert seen["channel"] == "chrome"

    with navigator.browser_session(tmp_path, 45):
        pass
    assert seen["channel"] is None, "empty means Playwright's own Chromium"
