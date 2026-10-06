"""Pure field logic from the scope document.

Every function here is total and side effect free so the whole of the business
logic can be tested without a browser. Money is Decimal throughout: these
values are written into a pricing sheet and float rounding is not acceptable.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

CONDITION_NEW = "NE"
CONDITION_OVERHAUL = "OH"

TYPE_EXCHANGE = "OEM Exchange"
TYPE_OUTRIGHT = "OEM Outright"

PRICE_REFERENCE = "TXT"

# The portal sometimes renders two currencies in one string, for example
# "$357.08 / 308,22 EUR". Only the USD amount is wanted, and the euro amount
# uses a comma as its decimal separator, so a naive number scan would read
# 308,22 as 30822. Anchoring on the dollar sign avoids that.
_USD = re.compile(r"\$\s*([0-9][0-9,]*(?:\.[0-9]{1,2})?)")

# "2 IN STOCK", "45 IN STOCK". Public because the extractor matches the same
# shape against individual page nodes; two copies of this pattern in two modules
# is two places for the portal's wording to be half updated.
STOCK = re.compile(r"\b([0-9][0-9,]*)\s*IN\s+STOCK\b", re.IGNORECASE)


class FieldError(ValueError):
    """Raised when a value is present but cannot be read as the expected type."""


def parse_usd_price(text: str | None) -> Decimal | None:
    """Return the USD amount in `text`, or None when there is no price at all.

    Raises FieldError when text is present but holds no dollar amount, because
    a page that renders a price we cannot read is a broken selector, not a
    part without a price.
    """
    if text is None:
        return None
    stripped = text.strip()
    if not stripped:
        return None
    m = _USD.search(stripped)
    if not m:
        raise FieldError(f"no USD amount found in {stripped!r}")
    try:
        return Decimal(m.group(1).replace(",", ""))
    except InvalidOperation as exc:  # pragma: no cover - guarded by the regex
        raise FieldError(f"cannot parse {m.group(1)!r} as a decimal") from exc


def parse_quantity(text: str | None) -> int | None:
    """Return the stock count, or None when the page shows no stock line."""
    if text is None:
        return None
    stripped = text.strip()
    if not stripped:
        return None
    m = STOCK.search(stripped)
    if not m:
        raise FieldError(f"no stock count found in {stripped!r}")
    return int(m.group(1).replace(",", ""))


def has_exchange_suffix(pn: str) -> bool:
    """True when the part number ends in the exchange marker.

    The scope document describes this as a "-EX" suffix, but no part number in
    the list actually carries a hyphen immediately before it: the live examples
    are 00033190-2EX, 9912125-2EX and C662502-0101EX. The real rule is a
    trailing EX.
    """
    return pn.strip().upper().endswith("EX")


@dataclass(frozen=True)
class ConditionResult:
    code: str
    conflict: bool
    reason: str


def condition_code(page_condition: str | None, pn: str) -> ConditionResult:
    """Decide NE or OH from the part number suffix, cross checked against the page.

    The client's rule, stated verbatim in the scope document: "Condition codes
    are only ever NE or OH. And if the part number has the EX at the end, it's
    OH". The suffix therefore governs the value written to the sheet.

    The page's own Condition line is read anyway and a disagreement is recorded,
    because a part number ending in those two letters for an unrelated reason
    would be mislabelled and nobody would know. The row is flagged for review
    rather than silently resolved either way.
    """
    suffix = has_exchange_suffix(pn)
    code = CONDITION_OVERHAUL if suffix else CONDITION_NEW
    stated = (page_condition or "").strip().upper()

    if not stated:
        return ConditionResult(code, False, "no condition on page, used part number suffix")

    if stated.startswith("EXCHANGE"):
        page_says = CONDITION_OVERHAUL
    elif stated.startswith("NEW"):
        page_says = CONDITION_NEW
    else:
        return ConditionResult(code, True, f"unrecognised condition {stated!r} on page")

    if page_says != code:
        return ConditionResult(
            code,
            True,
            f"part number suffix says {'exchange' if suffix else 'new'} "
            f"but page says {stated!r}",
        )
    return ConditionResult(code, False, "page and part number suffix agree")


def core_value(core_deposit: Decimal | None) -> Decimal:
    """Core Value is the deposit shown on the page, or zero when none is shown."""
    return Decimal("0") if core_deposit is None else core_deposit


def outright_price(sale_price: Decimal, core_deposit: Decimal | None) -> Decimal:
    """Sale price plus the core deposit, or the sale price alone when there is none."""
    return sale_price + core_value(core_deposit)


def part_type(core_deposit: Decimal | None) -> str:
    """OEM Exchange when a core deposit is charged, OEM Outright otherwise."""
    return TYPE_OUTRIGHT if core_deposit is None else TYPE_EXCHANGE
