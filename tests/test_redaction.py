"""Redaction tests.

A leak here is an incident, so these assert on the absence of the secret
rather than on the shape of the output.
"""

from __future__ import annotations

import re

import pytest

from txtav import logging_setup as log


class TestScrubText:
    def test_removes_a_cookie_header(self):
        out = log.scrub_text("Cookie: ASP.NET_SessionId=abc123def456; path=/")
        assert "abc123def456" not in out

    def test_removes_a_session_cookie_pair(self):
        out = log.scrub_text("got ASP.NET_SessionId=qq11ww22ee33 from response")
        assert "qq11ww22ee33" not in out

    def test_removes_a_bearer_token(self):
        out = log.scrub_text("Authorization: Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6")
        assert "eyJhbGciOiJIUzI1NiIsInR5cCI6" not in out

    def test_removes_a_password_assignment(self):
        assert "hunter2hunter2" not in log.scrub_text("password=hunter2hunter2")

    def test_removes_an_email_address(self):
        out = log.scrub_text("login failed for someone@example.com")
        assert "someone@example.com" not in out

    @pytest.mark.parametrize(
        "text",
        [
            "security code is 208805",
            "Your Textron Aviation security code is 577612",
            "verification code: 408241",
            "otp 123456 received",
            "2FA code 998877 entered",
        ],
    )
    def test_removes_a_one_time_code_when_announced_as_one(self, text):
        out = log.scrub_text(text)
        assert not re.search(r"\b\d{4,8}\b", out), out

    def test_keeps_part_numbers_readable(self):
        # Logs are useless if the part number is scrubbed out of them.
        for pn in ("6622168-22", "260-1014-33", "9912125-2EX"):
            assert pn in log.scrub_text(f"fetched {pn} ok")

    def test_keeps_numeric_part_numbers_readable(self):
        # 2,930 real part numbers are bare digit runs in exactly the shape of
        # a one time code. A pattern that redacts those is not acceptable.
        for pn in ("10003", "100057022", "35466514", "1003547"):
            assert pn in log.scrub_text(f"fetched {pn} ok")

    def test_keeps_prices_readable(self):
        # The regression this guards: a bare 4 to 8 digit rule turned
        # "7724.69" into "[REDACTED].69".
        for price in ("7724.69", "357.08", "16500.00", "24224.69"):
            assert price in log.scrub_text(f"sale_price={price}")


class TestRedactProcessor:
    def test_redacts_by_key_name(self):
        out = log.redact_processor(
            None, "info", {"event": "login", "password": "hunter2", "cookies": ["a"]}
        )
        assert out["password"] == log.REDACTED
        assert out["cookies"] == log.REDACTED

    def test_redacts_inside_nested_values(self):
        out = log.redact_processor(
            None,
            "info",
            {"event": "resp", "headers": {"set-cookie": "sid=deadbeefcafe"}},
        )
        assert "deadbeefcafe" not in str(out)

    def test_redacts_inside_a_list_of_strings(self):
        out = log.redact_processor(
            None, "info", {"event": "x", "notes": ["code is 123456"]}
        )
        assert "123456" not in str(out)

    def test_leaves_ordinary_fields_alone(self):
        out = log.redact_processor(
            None, "info", {"event": "scraped", "pn": "6622168-22", "status": "success"}
        )
        assert out["pn"] == "6622168-22"
        assert out["status"] == "success"
