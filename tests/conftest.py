"""Keep every test away from the real Google Sheet and the real Slack channel.

Settings reads `.env`, and `.env` names the client's live sheet and the team's
alert webhook. Any test that built Settings without overriding those wrote to
both: on 2026-10-09 the suite posted about twenty fake halts into
#txt-scraper-alerts and wrote three fixture parts over rows 2 to 4 of the
client's Output tab. Environment variables outrank `.env`, so blanking them
here takes precedence for every Settings a test builds, and the two network
exits are refused outright in case anything finds another way out.
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def never_touch_the_live_sheet_or_slack(monkeypatch):
    monkeypatch.setenv("TXTAV_SHEET_ID", "")
    monkeypatch.setenv("TXTAV_ALERT_WEBHOOK", "")

    def refuse(*args, **kwargs):
        raise AssertionError(
            "a test tried to reach the real Google Sheet or Slack; mock it instead"
        )

    # Tests that exercise these paths replace them with their own fakes, which
    # monkeypatch applies after this and so takes precedence.
    monkeypatch.setattr("txtav.alerts.urllib.request.urlopen", refuse)
    monkeypatch.setattr("txtav.sheets.build_service", refuse)
