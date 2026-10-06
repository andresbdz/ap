"""A screenshot that is not fully masked must be impossible to grab by mistake.

The runbook sends an operator to the screenshots directory to look at images.
Nobody opens a JSON log first, so a warning that lives only in the log does not
reach the person about to attach the file to a ticket. The filename has to
carry it.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from txtav import runner
from txtav.config import Settings


class FakeNavigator:
    """Stands in for a browser, reporting whatever mask result a test needs."""

    def __init__(self, counts: dict[str, int], *, raises: Exception | None = None):
        self.counts = counts
        self.raises = raises
        self.captured: list[Path] = []

    def screenshot(self, path: Path) -> dict[str, int]:
        if self.raises is not None:
            raise self.raises
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"\x89PNG\r\n\x1a\n")
        self.captured.append(path)
        return self.counts


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(db_path=tmp_path / "t.db", screenshot_dir=tmp_path / "shots")


FULLY_MASKED = {"account holder": 1, "customer number": 1, "support contacts": 6}


class TestFullyMasked:
    def test_the_file_is_named_plainly(self, settings):
        nav = FakeNavigator(FULLY_MASKED)
        path = runner.save_failure_screenshot(nav, settings, "6622168-22", "forbidden")

        assert path is not None
        assert path.exists()
        assert not path.name.startswith("UNMASKED")
        assert "forbidden" in path.name
        assert "6622168-22" in path.name


class TestPartiallyMasked:
    def test_the_filename_names_what_leaked(self, settings):
        # The exact case measured on 2026-10-05: renaming one class left the
        # customer number readable while the total masked count stayed at 7,
        # so a check on the total alone saw nothing wrong.
        nav = FakeNavigator({"account holder": 1, "customer number": 0, "support contacts": 6})
        path = runner.save_failure_screenshot(nav, settings, "6622168-22", "captcha")

        assert path is not None
        assert path.name.startswith("UNMASKED_customer-number_")
        assert path.exists()

    def test_no_file_is_left_behind_under_the_plain_name(self, settings):
        nav = FakeNavigator({"account holder": 0, "customer number": 0, "support contacts": 0})
        path = runner.save_failure_screenshot(nav, settings, "PN-1", "rate_limited")

        files = sorted(p.name for p in Path(settings.screenshot_dir).iterdir())
        assert files == [path.name]
        assert path.name.startswith("UNMASKED_")

    def test_every_missing_group_is_named(self, settings):
        nav = FakeNavigator({"account holder": 0, "customer number": 0, "support contacts": 1})
        path = runner.save_failure_screenshot(nav, settings, "PN-1", "captcha")

        assert "account-holder" in path.name
        assert "customer-number" in path.name


class TestAwkwardInput:
    def test_a_slash_in_the_part_number_does_not_escape_the_directory(self, settings):
        nav = FakeNavigator(FULLY_MASKED)
        path = runner.save_failure_screenshot(nav, settings, "000-110105-52U/P", "forbidden")

        assert path.parent == Path(settings.screenshot_dir)
        assert "/" not in path.name

    def test_a_dead_browser_does_not_break_the_stop(self, settings):
        # Failing to capture evidence must not change the outcome of a halt.
        nav = FakeNavigator(FULLY_MASKED, raises=RuntimeError("Target page closed"))
        assert runner.save_failure_screenshot(nav, settings, "PN-1", "captcha") is None
