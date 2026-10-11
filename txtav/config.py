"""Runtime configuration, loaded from the environment or a .env file.

Nothing in here holds a credential. The portal session is established by a
human in a real browser window and persists in the Playwright profile
directory, so the only secret-shaped value this app ever reads is an optional
alert webhook.
"""

from __future__ import annotations

from datetime import time
from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# 109,589 TXT part numbers spread over a 14 day cycle of continuous running
# works out to 11.0 seconds between requests. The default below matches the
# agreed scope; it is deliberately a setting rather than a constant so the
# pace can be reduced without a code change.
DEFAULT_INTERVAL_SECONDS = 11.0


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="TXTAV_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Paths
    db_path: Path = Path("data/txtav.db")
    profile_dir: Path = Path(".pw-profile")
    parts_xlsx: Path = Path("data/PN_List_full.xlsx")
    fixtures_dir: Path = Path("fixtures")
    log_dir: Path = Path("logs")
    screenshot_dir: Path = Path("screenshots")
    # Pages of rows flagged for review, other than for multiple alternates.
    # Under fixtures/, which is gitignored: these pages carry account details.
    review_pages_dir: Path = Path("fixtures/review")

    # Pacing
    request_interval_seconds: float = DEFAULT_INTERVAL_SECONDS
    jitter_seconds: float = 2.0
    # When recent pages take this many times longer than the portal's own normal,
    # the interval is stretched by the same ratio, so a struggling portal is
    # asked for less. It never shortens the interval. 0 turns it off.
    slowdown_threshold: float = 2.0
    # However slow the portal gets, the gap does not grow past this. Beyond it
    # the run is effectively stalled, and the schedule alert says so.
    max_interval_seconds: float = 60.0
    cycle_days: int = 14
    window_start: time | None = None
    window_end: time | None = None

    # Reliability
    max_network_retries: int = 3
    # Retry waits use exponential backoff with full jitter on top of the pacer:
    # a random wait between 0 and min(cap, base * 2^attempt).
    backoff_base_seconds: float = 5.0
    backoff_cap_seconds: float = 60.0
    # A 503 that names a Retry-After is honoured up to this long. Asked to stay
    # away for longer, the run stops for a person rather than sitting on it.
    retry_after_cap_seconds: float = 300.0
    # Consecutive failed parts before the run halts for a human. A string of
    # failures with no single cause is the shape of silent throttling, so this
    # has to be low; but every halt is sticky and costs a person, and at 3 a
    # brief network wobble during the night was enough to stop the run until
    # morning. Each failure is already three exhausted attempts of its own.
    consecutive_failure_halt: int = 8
    page_timeout_seconds: int = 45

    # Portal
    base_url: str = "https://ww2.txtav.com"

    # Output spreadsheet. A native Google Sheet, not an uploaded .xlsx: the
    # Sheets API cannot write into an uploaded workbook, and the first copy of
    # this file was one.
    sheet_id: str = ""
    sheet_tab: str = "Output"
    google_client_secrets: Path = Path("secrets/google-oauth-client.json")
    google_token: Path = Path("secrets/google-token.json")

    # Alerting
    alert_webhook: str = ""
    alert_min_level: str = "warning"

    @field_validator("window_start", "window_end", mode="before")
    @classmethod
    def _blank_is_none(cls, v: object) -> object:
        if isinstance(v, str) and not v.strip():
            return None
        return v

    @field_validator("request_interval_seconds")
    @classmethod
    def _interval_floor(cls, v: float) -> float:
        # A hard floor so a typo in .env cannot turn this into a hammer.
        if v < 2.0:
            raise ValueError(
                "request_interval_seconds below 2.0 is not permitted; "
                "the agreed pace is one part every 11 seconds"
            )
        return v


def load_settings() -> Settings:
    return Settings()
