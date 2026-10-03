"""Field logic, pinned to the three parts verified by hand on the live portal.

6622168-22, 260-1014-33 and 9912125-2EX were opened in the browser on
2026-10-01 and their values read off the page. If a change to rules.py breaks
one of these, the change is wrong.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from txtav import rules


class TestParseUsdPrice:
    def test_plain_usd(self):
        assert rules.parse_usd_price("Your Price: $110.53") == Decimal("110.53")

    def test_thousands_separator(self):
        assert rules.parse_usd_price("$7,724.69") == Decimal("7724.69")

    def test_core_deposit(self):
        assert rules.parse_usd_price("Core Deposit: $16,500.00") == Decimal("16500.00")

    def test_dual_currency_takes_the_dollar_amount(self):
        # The euro amount uses a comma decimal separator. Reading left to right
        # without anchoring on the dollar sign would give 30822 here.
        assert rules.parse_usd_price("$357.08 / 308,22 EUR") == Decimal("357.08")

    def test_absent_price_is_none(self):
        assert rules.parse_usd_price(None) is None
        assert rules.parse_usd_price("   ") is None

    def test_unparseable_price_raises(self):
        # A page that renders something where a price belongs is a broken
        # selector, and must never be recorded as "no price".
        with pytest.raises(rules.FieldError):
            rules.parse_usd_price("Request Access to View Pricing")


class TestParseQuantity:
    @pytest.mark.parametrize(
        "text,expected",
        [("2 IN STOCK", 2), ("45 IN STOCK", 45), ("1,250 in stock", 1250)],
    )
    def test_reads_stock_line(self, text, expected):
        assert rules.parse_quantity(text) == expected

    def test_absent_is_none(self):
        assert rules.parse_quantity(None) is None
        assert rules.parse_quantity("") is None

    def test_unparseable_raises(self):
        with pytest.raises(rules.FieldError):
            rules.parse_quantity("No Service Center Stock Available")


class TestExchangeSuffix:
    @pytest.mark.parametrize(
        "pn", ["00033190-2EX", "9912125-2EX", "C662502-0101EX", "0020BG1EX"]
    )
    def test_real_exchange_part_numbers(self, pn):
        # None of these carry a hyphen directly before EX, which is why the
        # scope document's "-EX" wording cannot be implemented literally.
        assert rules.has_exchange_suffix(pn)

    @pytest.mark.parametrize("pn", ["6622168-22", "260-1014-33", "9912125-2"])
    def test_ordinary_part_numbers(self, pn):
        assert not rules.has_exchange_suffix(pn)


class TestConditionCode:
    def test_page_says_exchange_and_suffix_agrees(self):
        result = rules.condition_code("EXCHANGE", "9912125-2EX")
        assert result.code == rules.CONDITION_OVERHAUL
        assert not result.conflict

    def test_page_says_new_and_suffix_agrees(self):
        result = rules.condition_code("NEW", "6622168-22")
        assert result.code == rules.CONDITION_NEW
        assert not result.conflict

    def test_no_condition_on_page_falls_back_to_suffix(self):
        # 6622168-22 and 260-1014-33 have no Condition line at all.
        assert rules.condition_code(None, "6622168-22").code == rules.CONDITION_NEW
        assert rules.condition_code(None, "9912125-2EX").code == rules.CONDITION_OVERHAUL

    def test_suffix_wins_over_the_page_but_flags_the_disagreement(self):
        # The client's rule is explicit: a part number ending in EX is OH. If
        # the page disagrees the client's rule still decides the written value,
        # and the row is flagged so a human can look at it.
        result = rules.condition_code("NEW", "SOMEPARTEX")
        assert result.code == rules.CONDITION_OVERHAUL
        assert result.conflict

    def test_page_saying_exchange_on_a_non_ex_part_is_flagged(self):
        result = rules.condition_code("EXCHANGE", "6622168-22")
        assert result.code == rules.CONDITION_NEW
        assert result.conflict

    def test_unrecognised_condition_is_a_conflict(self):
        result = rules.condition_code("REFURBISHED", "6622168-22")
        assert result.conflict


class TestDerivedPrices:
    def test_core_value_defaults_to_zero(self):
        assert rules.core_value(None) == Decimal("0")

    def test_core_value_is_the_deposit(self):
        assert rules.core_value(Decimal("16500.00")) == Decimal("16500.00")

    def test_outright_without_core_equals_sale(self):
        assert rules.outright_price(Decimal("357.08"), None) == Decimal("357.08")

    def test_outright_with_core_adds_the_deposit(self):
        assert rules.outright_price(
            Decimal("7724.69"), Decimal("16500.00")
        ) == Decimal("24224.69")

    def test_type_follows_the_core_deposit(self):
        assert rules.part_type(None) == rules.TYPE_OUTRIGHT
        assert rules.part_type(Decimal("16500.00")) == rules.TYPE_EXCHANGE


class TestVerifiedLivePages:
    """End to end over the field rules for the three hand checked parts."""

    def test_6622168_22_bleed_air_stop(self):
        sale = rules.parse_usd_price("Your Price: $357.08 / 308,22 EUR")
        core = rules.parse_usd_price(None)
        assert sale == Decimal("357.08")
        assert rules.parse_quantity("2 IN STOCK") == 2
        assert rules.condition_code(None, "6622168-22").code == "NE"
        assert rules.core_value(core) == Decimal("0")
        assert rules.outright_price(sale, core) == Decimal("357.08")
        assert rules.part_type(core) == "OEM Outright"

    def test_260_1014_33_ice_chest_lid(self):
        sale = rules.parse_usd_price("Your Price: $110.53")
        core = rules.parse_usd_price(None)
        assert sale == Decimal("110.53")
        assert rules.parse_quantity("2 IN STOCK") == 2
        assert rules.condition_code(None, "260-1014-33").code == "NE"
        assert rules.outright_price(sale, core) == Decimal("110.53")
        assert rules.part_type(core) == "OEM Outright"

    def test_9912125_2ex_starter_generator(self):
        sale = rules.parse_usd_price("Your Price: $7,724.69")
        core = rules.parse_usd_price("Core Deposit: $16,500.00")
        assert sale == Decimal("7724.69")
        assert rules.parse_quantity("45 IN STOCK") == 45
        assert rules.condition_code("EXCHANGE", "9912125-2EX").code == "OH"
        assert rules.core_value(core) == Decimal("16500.00")
        assert rules.outright_price(sale, core) == Decimal("24224.69")
        assert rules.part_type(core) == "OEM Exchange"
