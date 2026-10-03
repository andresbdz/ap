"""URL construction, driven by the awkward part numbers in the real list.

The samples here were taken from data/PN_List_full.xlsx: 1,373 TXT part
numbers contain a forward slash, 529 contain a space, 28 an @, 4 a +, 3 a #
and 3 a %.
"""

from __future__ import annotations

import pytest

from txtav import urls

BASE = "https://ww2.txtav.com"


class TestNormalise:
    def test_trims_surrounding_whitespace(self):
        assert urls.normalise_pn("  6622168-22 ") == "6622168-22"

    def test_preserves_internal_space(self):
        # 529 real part numbers look like this and the space is part of the value.
        assert urls.normalise_pn("000-110105-79 U") == "000-110105-79 U"

    def test_preserves_case(self):
        # 222a2 and MS21922-4c are genuine list entries. We do not know whether
        # the portal lookup is case sensitive, so the value is sent as supplied.
        assert urls.normalise_pn("MS21922-4c") == "MS21922-4c"

    def test_dedupe_key_is_case_insensitive(self):
        assert urls.dedupe_key("ms21922-4C") == urls.dedupe_key("MS21922-4c")


class TestWhichPartNumbersNeedTheSearchBox:
    """Measured against the live portal on 2026-10-02, not assumed.

    Percent encoding carries a space, @, +, # and % through the path form
    successfully. Only a forward slash fails, with HTTP 404 whether it is raw
    or encoded, because ASP.NET refuses double escaped paths.
    """

    @pytest.mark.parametrize(
        "pn",
        [
            "000-110105-52U/P",   # 1,373 part numbers contain a slash
            "032 MIL-R-6855/1",   # slash and space together
            "063 2024T3 250/5",
        ],
    )
    def test_slash_part_numbers_need_the_search_box(self, pn):
        assert urls.needs_search_box(pn)

    @pytest.mark.parametrize(
        "pn",
        [
            "000-110105-79 U",    # verified: real part at $2,038.73
            "125LEXANF60011B@",
            "AGE-MASTER#1",
            "MP850-50.0-1%",
            "HAT-3+",
            "6622168-22",
            "9912125-2EX",
        ],
    )
    def test_everything_else_survives_percent_encoding(self, pn):
        assert not urls.needs_search_box(pn)


class TestUrlBuilders:
    def test_path_form_for_a_simple_part(self):
        assert urls.path_url("6622168-22", BASE) == (
            "https://ww2.txtav.com/Parts/PartSearch/PartsDetail/6622168-22"
        )

    def test_path_form_escapes_a_slash(self):
        # Left raw this would address .../PartsDetail/000-110105-52U/P and
        # resolve to a different route.
        assert urls.path_url("000-110105-52U/P", BASE).endswith(
            "/PartsDetail/000-110105-52U%2FP"
        )

    def test_path_form_escapes_a_hash(self):
        # Unescaped, everything after the # is a fragment and never reaches
        # the server.
        built = urls.path_url("AGE-MASTER#1", BASE)
        assert "#" not in built
        assert built.endswith("AGE-MASTER%231")

    def test_path_form_escapes_a_space(self):
        assert urls.path_url("000-110105-79 U", BASE).endswith("000-110105-79%20U")

    def test_detail_url_uses_the_path_form(self):
        assert urls.detail_url("6622168-22", BASE).endswith("/PartsDetail/6622168-22")

    def test_detail_url_routes_a_slash_part_number_through_the_search(self):
        built = urls.detail_url("000-110105-52U/P", BASE)
        assert built == (
            "https://ww2.txtav.com/Parts/PartSearch?partdesc=000-110105-52U%2FP"
        )

    def test_search_url_never_uses_the_disallowed_parameter(self):
        # robots.txt disallows /Parts/PartSearch/PartsDetail?PartNumbers=.
        # The search form is a different endpoint and is not disallowed.
        built = urls.search_url("000-110105-52U/P", BASE)
        assert "PartNumbers=" not in built
        assert "/PartsDetail?" not in built

    def test_trailing_slash_on_base_url_does_not_double_up(self):
        assert "//Parts" not in urls.path_url("6622168-22", BASE + "/")
