"""The guard in conftest.py, pinned so it cannot quietly stop working."""

from __future__ import annotations

import pytest

from txtav import alerts, live_sheet
from txtav.config import Settings


def test_settings_in_a_test_never_name_the_live_sheet_or_webhook():
    settings = Settings()
    assert settings.sheet_id == ""
    assert settings.alert_webhook == ""


def test_a_live_sheet_cannot_be_built_from_a_test():
    assert live_sheet.build(Settings()) is None


def test_an_alert_cannot_leave_a_test_even_with_a_webhook():
    with pytest.raises(AssertionError, match="real Google Sheet or Slack"):
        alerts.urllib.request.urlopen("https://hooks.slack.com/services/x")
