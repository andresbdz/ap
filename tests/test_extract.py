"""Extractor tests against the real saved pages.

The fixtures were captured from the live portal on 2026-10-02 with a signed in
session. Values were also read off the screen by hand at capture time, so these
assertions are checked against two independent readings.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from txtav import extract

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"

pytestmark = pytest.mark.skipif(
    not (FIXTURES / "6622168-22.html").exists(),
    reason="fixtures not captured; run scripts/capture_fixtures.py",
)


def load(pn: str):
    return extract.extract((FIXTURES / f"{pn}.html").read_text(encoding="utf-8"), pn)


class TestSimplePartNoCoreDeposit:
    """6622168-22 BLEED AIR STOP-RH. Dual currency price, no condition line."""

    @pytest.fixture
    def record(self):
        return load("6622168-22")

    def test_identifies_the_part(self, record):
        assert record.found
        assert record.pn_resolved == "6622168-22"
        assert record.description == "BLEED AIR STOP-RH"

    def test_takes_the_usd_price_not_the_euro_one(self, record):
        # The page renders "$357.08 / 308,22 EUR".
        assert record.sale_price == Decimal("357.08")
        assert record.list_price == Decimal("357.08")

    def test_quantity(self, record):
        assert record.quantity == 2

    def test_derived_fields(self, record):
        assert record.condition_code == "NE"
        assert record.core_deposit is None
        assert record.core_value == Decimal("0")
        assert record.outright_price == Decimal("357.08")
        assert record.part_type == "OEM Outright"

    def test_no_alternate_part_and_no_tabs(self, record):
        assert record.alt_pn is None
        assert record.tabs == {}

    def test_nothing_flagged(self, record):
        assert record.conflicts == []


class TestSimplePartUsdOnly:
    """260-1014-33 ICE CHEST LID."""

    @pytest.fixture
    def record(self):
        return load("260-1014-33")

    def test_fields(self, record):
        assert record.found
        assert record.description == "ICE CHEST LID"
        assert record.sale_price == Decimal("110.53")
        assert record.quantity == 2
        assert record.condition_code == "NE"
        assert record.outright_price == Decimal("110.53")
        assert record.part_type == "OEM Outright"
        assert record.conflicts == []


class TestExchangePartWithCoreDeposit:
    """9912125-2EX STARTER GENERATOR. Core deposit, three sibling part numbers."""

    @pytest.fixture
    def record(self):
        return load("9912125-2EX")

    def test_identifies_the_part(self, record):
        assert record.found
        assert record.pn_resolved == "9912125-2EX"
        assert record.description == "STARTER GENERATOR"

    def test_prices(self, record):
        assert record.sale_price == Decimal("7724.69")
        assert record.core_deposit == Decimal("16500.00")

    def test_outright_is_sale_plus_core(self, record):
        assert record.outright_price == Decimal("24224.69")

    def test_condition_and_type(self, record):
        assert record.page_condition == "EXCHANGE"
        assert record.condition_code == "OH"
        assert record.core_value == Decimal("16500.00")
        assert record.part_type == "OEM Exchange"

    def test_quantity_is_read_from_the_stock_line(self, record):
        assert record.quantity == 46

    def test_siblings_are_part_numbers_not_tabs(self, record):
        assert record.tabs == {
            "NEW": "9912125-2",
            "EXCHANGE": "9912125-2EX",
            "REPAIR SERVICES": "9912125-2REP",
        }

    def test_active_sibling_is_the_part_that_was_requested(self, record):
        assert record.tab_active == "EXCHANGE"
        assert record.tabs[record.tab_active] == record.pn_requested

    def test_takes_the_visible_alternate_part_number(self, record):
        assert record.alt_pn == "9912125-2"
        assert record.alt_label == "NEW"

    def test_records_the_hidden_alternate_rather_than_using_it(self, record):
        # 9912125-2W sits in a block marked display:none. Reading the hidden
        # inputs alone would have picked it up.
        assert ("ALTERNATE PART NUMBER", "9912125-2W") in record.alt_hidden
        assert record.alt_pn != "9912125-2W"

    def test_nothing_flagged(self, record):
        assert record.conflicts == []


class TestDiscontinuedPart:
    """000-110105-52U/P. The portal says "No longer available".

    Captured on 2026-10-02. The page keeps its description and offers a
    replacement part number, but shows no price and no stock line, and its
    hidden YourPriceUS_0 is "0". Writing $0.00 into a pricing sheet would be
    worse than writing blank, and routing every such part to manual review
    would bury the rows that genuinely need a person.
    """

    VERIFY = FIXTURES / "verify" / "000-110105-52U_P.html"

    @pytest.fixture
    def record(self):
        if not self.VERIFY.exists():
            pytest.skip("run scripts/verify_sample.py to capture this page")
        return extract.extract(self.VERIFY.read_text(encoding="utf-8"), "000-110105-52U/P")

    def test_it_is_found_but_marked_discontinued(self, record):
        assert record.found
        assert record.availability == extract.DISCONTINUED

    def test_the_placeholder_zero_is_not_recorded_as_a_price(self, record):
        assert record.sale_price is None
        assert record.sale_price_rendered is None

    def test_it_still_carries_description_and_replacement(self, record):
        assert record.description == "SPLICE- NOSE-W.S. 47.00 LEADIN"
        assert record.alt_pn == "000-110105-92"

    def test_the_expected_absences_are_not_flagged(self, record):
        joined = " ".join(record.conflicts)
        assert "no USD price" not in joined
        assert "no stock line" not in joined

    def test_an_available_part_missing_its_price_is_still_flagged(self):
        html = (FIXTURES / "6622168-22.html").read_text(encoding="utf-8")
        stripped = html.replace("Your Price", "Price").replace("2 IN STOCK", "")
        record = extract.extract(stripped, "6622168-22")
        assert record.availability == extract.AVAILABLE
        assert any("no USD price" in c for c in record.conflicts)
        assert any("no stock line" in c for c in record.conflicts)


class TestQuoteOnlyPart:
    """Parts sold on quotation: "Please contact us for assistance with pricing".

    Captured on 2026-10-02 from a 50 part run, where 2 of 50 were in this
    state. The page renders no price and no stock line and its hidden
    YourPriceUS_0 reads "0", exactly like a discontinued part, but it is a
    different thing and the client will want to tell them apart.
    """

    @pytest.mark.parametrize(
        "name,pn",
        [
            ("000-110106-60", "000-110106-60"),
            (".025_AMS3315_BLK", ".025 AMS3315 BLK"),
        ],
    )
    def test_recognised_as_price_on_request(self, name, pn):
        path = FIXTURES / "verify" / f"{name}.html"
        if not path.exists():
            pytest.skip("run scripts/verify_sample.py to capture this page")
        record = extract.extract(path.read_text(encoding="utf-8"), pn)

        assert record.found
        assert record.availability == extract.QUOTE_ONLY
        assert record.sale_price is None
        assert record.quantity is None
        # The expected absences must not reach a human's review queue.
        assert record.conflicts == []
        assert record.description


class TestPartNotFound:
    """ZZZZ-9999 does not exist. The portal still returns HTTP 200."""

    @pytest.fixture
    def record(self):
        return load("ZZZZ-9999")

    def test_reports_not_found_rather_than_raising(self, record):
        assert record.found is False

    def test_carries_no_invented_values(self, record):
        assert record.sale_price is None
        assert record.quantity is None
        assert record.description is None


class TestCrossChecks:
    """Every way a page could be lying has to reach `conflicts`."""

    def test_hidden_price_matches_the_displayed_one_on_every_fixture(self):
        # Nothing in the fixtures proves YourPriceUS_0 is the account price
        # rather than the list price, because this account has a 0% discount
        # and the two are equal. This is the check that would catch it.
        for pn in ("6622168-22", "260-1014-33", "9912125-2EX"):
            record = load(pn)
            assert record.sale_price == record.sale_price_rendered, pn

    def test_a_price_mismatch_is_flagged(self):
        html = (FIXTURES / "6622168-22.html").read_text(encoding="utf-8")
        tampered = html.replace('id="YourPriceUS_0" value="357.08"', 'id="YourPriceUS_0" value="99.99"')
        record = extract.extract(tampered, "6622168-22")
        assert any("does not match" in c for c in record.conflicts)

    def test_being_served_a_different_part_is_flagged(self):
        html = (FIXTURES / "6622168-22.html").read_text(encoding="utf-8")
        record = extract.extract(html, "SOME-OTHER-PN")
        assert any("asked for" in c for c in record.conflicts)

    def test_case_and_whitespace_do_not_count_as_a_different_part(self):
        html = (FIXTURES / "9912125-2EX.html").read_text(encoding="utf-8")
        record = extract.extract(html, " 9912125-2ex ")
        assert not any("asked for" in c for c in record.conflicts)

    def test_a_missing_description_is_flagged(self):
        html = (FIXTURES / "6622168-22.html").read_text(encoding="utf-8")
        stripped = html.replace("BLEED AIR STOP-RH", "")
        record = extract.extract(stripped, "6622168-22")
        assert any("no description" in c for c in record.conflicts)

    def test_a_missing_stock_line_is_flagged(self):
        html = (FIXTURES / "6622168-22.html").read_text(encoding="utf-8")
        record = extract.extract(html.replace("2 IN STOCK", ""), "6622168-22")
        assert record.quantity is None
        assert any("no stock line" in c for c in record.conflicts)

    def test_a_second_visible_alternate_is_flagged_not_dropped(self):
        html = (FIXTURES / "9912125-2EX.html").read_text(encoding="utf-8")
        both_visible = html.replace('style="display:none"', 'style=""')
        record = extract.extract(both_visible, "9912125-2EX")
        assert len(record.alt_visible) == 2
        assert any("more than one alternate" in c for c in record.conflicts)


class TestRefusesBadInput:
    def test_signed_out_page_is_an_error_not_a_blank_record(self):
        # A signed out page renders the description and dimensions but hides
        # price and stock. Treating it as a part with no price would overwrite
        # good data with blanks across the whole run.
        html = "<html><body>Hello, Log In! My Account Log In Register</body></html>"
        with pytest.raises(extract.ExtractionError, match="signed out"):
            extract.extract(html, "6622168-22")

    def test_page_without_a_part_number_is_an_error(self):
        html = "<html><body><p>Service temporarily unavailable</p></body></html>"
        with pytest.raises(extract.ExtractionError, match="no part number"):
            extract.extract(html, "6622168-22")
