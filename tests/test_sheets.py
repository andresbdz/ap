"""Sheet writing helpers.

No network: the API client is a stand in that records what it was asked to do.
What is under test is the shape of the request, because the failures that
matter here are silent ones, a range that is too narrow or a value the sheet
stores as text.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from txtav import exporter, sheets


class TestColumnLetter:
    @pytest.mark.parametrize(
        "count,expected", [(1, "A"), (11, "K"), (26, "Z"), (27, "AA"), (52, "AZ")]
    )
    def test_maps_a_count_to_a_letter(self, count, expected):
        assert sheets.column_letter(count) == expected

    def test_the_clients_layout_ends_at_k(self):
        assert sheets.column_letter(len(exporter.COLUMNS)) == "K"

    def test_zero_is_rejected(self):
        with pytest.raises(ValueError):
            sheets.column_letter(0)


class TestCellConversion:
    def test_decimal_becomes_a_number_the_sheet_can_total(self):
        # JSON has no decimal type, and a spreadsheet stores every number as a
        # double. Sending the Decimal straight through raises halfway into a
        # 109,589 row write.
        assert sheets._cell(Decimal("7724.69")) == pytest.approx(7724.69)
        assert not isinstance(sheets._cell(Decimal("7724.69")), str)

    def test_zero_survives_as_a_number(self):
        # The scope requires 0 in Core Value for an outright part, and
        # Decimal("0") is falsy, which is how it got blanked once before.
        assert sheets._cell(Decimal("0")) == 0

    def test_none_becomes_an_empty_cell_not_the_word_none(self):
        assert sheets._cell(None) == ""

    @pytest.mark.parametrize("value", ["NE", 46, 2.5, True, ""])
    def test_ordinary_values_pass_through(self, value):
        assert sheets._cell(value) == value


class FakeValues:
    def __init__(self, recorder):
        self.recorder = recorder

    def update(self, **kwargs):
        self.recorder.append(kwargs)
        return FakeRequest({"updatedCells": len(kwargs["body"]["values"]) * 11})

    def get(self, **kwargs):
        self.recorder.append(kwargs)
        return FakeRequest({"values": [list(exporter.COLUMNS)]})


class FakeRequest:
    def __init__(self, result):
        self.result = result

    def execute(self):
        return self.result


class FakeSpreadsheets:
    def __init__(self, recorder):
        self.recorder = recorder

    def values(self):
        return FakeValues(self.recorder)


class FakeService:
    def __init__(self):
        self.calls: list[dict] = []

    def spreadsheets(self):
        return FakeSpreadsheets(self.calls)


@pytest.fixture
def service() -> FakeService:
    return FakeService()


def rows(count: int) -> list[list[object]]:
    return [
        [f"PN-{i}", "", "DESC", "NE", i, Decimal("1.00"), Decimal("1.00"),
         Decimal("0"), "OEM Outright", "TXT", "2026-10-06"]
        for i in range(count)
    ]


class TestWriteRows:
    def test_the_range_spans_every_column_written(self, service):
        sheets.write_rows(service, "sid", "Output", rows(3))
        assert service.calls[0]["range"] == "Output!A2:K4"

    def test_data_starts_at_row_two_leaving_the_header(self, service):
        sheets.write_rows(service, "sid", "Output", rows(1))
        assert service.calls[0]["range"].startswith("Output!A2")

    def test_large_writes_are_split_into_blocks(self, service):
        sheets.write_rows(service, "sid", "Output", rows(2500), block_rows=1000)
        ranges = [call["range"] for call in service.calls]
        assert ranges == ["Output!A2:K1001", "Output!A1002:K2001", "Output!A2002:K2501"]

    def test_blocks_are_contiguous_with_no_gap_or_overlap(self, service):
        sheets.write_rows(service, "sid", "Output", rows(2500), block_rows=1000)
        written = sum(len(call["body"]["values"]) for call in service.calls)
        assert written == 2500

    def test_values_are_sent_as_numbers(self, service):
        sheets.write_rows(service, "sid", "Output", rows(1))
        first = service.calls[0]["body"]["values"][0]
        assert first[5] == pytest.approx(1.0)
        assert first[7] == 0

    def test_user_entered_so_the_sheet_parses_numbers(self, service):
        sheets.write_rows(service, "sid", "Output", rows(1))
        assert service.calls[0]["valueInputOption"] == "USER_ENTERED"


class FakeHttpError(Exception):
    def __init__(self, status: int, message: str = ""):
        super().__init__(message or f"HTTP {status}")
        self.resp = type("Resp", (), {"status": status})()


class FlakyRequest:
    """Fails with the given exceptions, then succeeds."""

    def __init__(self, failures: list[Exception], result=None):
        self.failures = list(failures)
        self.result = result if result is not None else {"updatedCells": 1}
        self.attempts = 0

    def execute(self):
        self.attempts += 1
        if self.failures:
            raise self.failures.pop(0)
        return self.result


class TestRateLimitHandling:
    def test_recognises_the_quota_response(self):
        assert sheets.is_rate_limited(FakeHttpError(429))
        assert sheets.is_rate_limited(Exception("RATE_LIMIT_EXCEEDED for writes"))

    def test_other_errors_are_not_rate_limits(self):
        assert not sheets.is_rate_limited(FakeHttpError(403))
        assert not sheets.is_rate_limited(ValueError("bad range"))

    def test_waits_out_the_window_and_then_succeeds(self):
        # Measured on 2026-10-06: 110 requests of 1,000 rows hit the 60 writes
        # per minute limit at around row 61,000.
        slept: list[float] = []
        request = FlakyRequest([FakeHttpError(429)])
        result = sheets.execute_with_retry(request, sleep=slept.append)

        assert result == {"updatedCells": 1}
        assert request.attempts == 2
        assert slept == [sheets.RATE_LIMIT_WAIT_SECONDS]

    def test_a_permission_error_is_not_retried(self):
        # Retrying a 403 would hide a real problem behind a minute of waiting.
        request = FlakyRequest([FakeHttpError(403, "caller lacks permission")])
        with pytest.raises(FakeHttpError):
            sheets.execute_with_retry(request, sleep=lambda _: None)
        assert request.attempts == 1

    def test_gives_up_rather_than_waiting_forever(self):
        request = FlakyRequest([FakeHttpError(429)] * 10)
        with pytest.raises(FakeHttpError):
            sheets.execute_with_retry(request, sleep=lambda _: None)
        assert request.attempts == sheets.MAX_RATE_LIMIT_RETRIES


class TestBlockSizeStaysInsideTheQuota:
    def test_a_full_table_fits_in_one_quota_window(self):
        # 109,589 parts at the default block size must come to fewer than the
        # 60 writes per minute the API allows.
        import math

        requests = math.ceil(109_589 / sheets.BLOCK_ROWS)
        assert requests < 60, f"{requests} requests would hit the rate limit again"


class TestHeaderCheck:
    def test_a_matching_header_reports_no_problems(self, service):
        assert sheets.check_header(service, "sid", "Output", exporter.COLUMNS) == []

    def test_a_renamed_column_is_reported_with_its_letter(self, service):
        changed = list(exporter.COLUMNS)
        changed[8] = "Part Type"
        problems = sheets.check_header(service, "sid", "Output", changed)
        assert any("column I" in p for p in problems)
