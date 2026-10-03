"""Turn a rendered part detail page into a record.

Operates on an HTML string so the whole extractor is testable against the saved
fixtures without a browser. At run time the caller passes `page.content()`.

Where the portal offers a hidden input it is preferred over the rendered text:
`YourPriceUS_0` carries the USD amount as a plain decimal, which avoids parsing
a string like "$357.08 / 308,22 EUR" where the euro figure uses a comma as its
decimal separator.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import Decimal

from lxml import html as LH

from txtav import rules

NOT_FOUND_MARKER = "part not found"
LOGGED_OUT_MARKER = "hello, log in"

# A discontinued part. The page still carries the description and usually a
# replacement part number, but no price and no stock line. This is an ordinary
# business state, not a fault: treating it as one would route a large share of
# a 109,589 part list into manual review and bury the rows that really need a
# human.
DISCONTINUED_MARKER = "no longer available"

# A part sold on quotation. The page renders no price and no stock line, and
# its hidden YourPriceUS_0 reads "0", exactly like a discontinued part. Also an
# ordinary business state and, in a 109,589 part list, not a rare one.
QUOTE_ONLY_MARKER = "contact us for assistance with pricing"

AVAILABLE = "available"
DISCONTINUED = "no_longer_available"
QUOTE_ONLY = "price_on_request"

_STOCK = re.compile(r"\b([0-9][0-9,]*)\s*IN\s+STOCK\b", re.IGNORECASE)


class ExtractionError(ValueError):
    """The page loaded but does not look like a part detail page at all."""


@dataclass
class PartRecord:
    pn_requested: str
    pn_resolved: str | None = None
    found: bool = False
    availability: str = AVAILABLE
    description: str | None = None
    quantity: int | None = None
    sale_price: Decimal | None = None
    sale_price_rendered: Decimal | None = None
    list_price: Decimal | None = None
    core_deposit: Decimal | None = None
    page_condition: str | None = None
    alt_pn: str | None = None
    alt_label: str | None = None
    alt_visible: list[tuple[str, str]] = field(default_factory=list)
    alt_hidden: list[tuple[str, str]] = field(default_factory=list)
    tab_active: str | None = None
    tabs: dict[str, str] = field(default_factory=dict)
    conflicts: list[str] = field(default_factory=list)

    @property
    def condition_code(self) -> str:
        return rules.condition_code(self.page_condition, self.pn_requested).code

    @property
    def core_value(self) -> Decimal:
        return rules.core_value(self.core_deposit)

    @property
    def outright_price(self) -> Decimal | None:
        if self.sale_price is None:
            return None
        return rules.outright_price(self.sale_price, self.core_deposit)

    @property
    def part_type(self) -> str:
        return rules.part_type(self.core_deposit)


def _text(node) -> str:
    return re.sub(r"\s+", " ", node.text_content()).strip()


def visible_text(page_html: str) -> str:
    """What a person would read on the page, with script and style stripped.

    This matters more than it looks. The portal loads reCAPTCHA on every page,
    so the word "captcha" appears 69 times in the markup of an ordinary part
    page. Judging a page by its raw HTML would halt the run on the very first
    part, every time. The guard must only ever be shown this.
    """
    doc = LH.fromstring(page_html)
    for node in doc.xpath("//script|//style|//noscript"):
        node.getparent().remove(node)
    body = doc.body if doc.body is not None else doc
    return _text(body)


def _first(doc, xpath: str) -> str | None:
    found = doc.xpath(xpath)
    if not found:
        return None
    value = found[0]
    return value.strip() if isinstance(value, str) else _text(value)


def _money(doc, xpath: str) -> Decimal | None:
    raw = _first(doc, xpath)
    return None if raw is None else rules.parse_usd_price(raw)


def extract(page_html: str, pn_requested: str) -> PartRecord:
    """Read one part detail page. Never guesses: anything odd lands in conflicts."""
    doc = LH.fromstring(page_html)
    record = PartRecord(pn_requested=pn_requested)

    body = _text(doc.body).lower() if doc.body is not None else ""
    if LOGGED_OUT_MARKER in body:
        raise ExtractionError("page was rendered while signed out")

    record.pn_resolved = _first(
        doc, "//h1[contains(@class,'part-detail-title')]"
    ) or _first(doc, "//input[@id='ProductPartNumber_0']/@value")

    if record.pn_resolved is None:
        raise ExtractionError("no part number on the page")

    if NOT_FOUND_MARKER in body:
        record.found = False
        return record
    record.found = True

    if DISCONTINUED_MARKER in body:
        record.availability = DISCONTINUED
    elif QUOTE_ONLY_MARKER in body:
        record.availability = QUOTE_ONLY

    record.description = _first(doc, "//*[@id='partDescription_0']")

    # The hidden input is preferred because it needs no currency parsing, but
    # it is only trusted when the page actually displays a price to corroborate
    # it. A discontinued part carries YourPriceUS_0="0" with nothing rendered,
    # and writing $0.00 into a pricing sheet would be worse than writing blank.
    record.sale_price_rendered = _money(doc, "//h2[contains(.,'Your Price')]")
    raw_usd = _first(doc, "//input[@id='YourPriceUS_0']/@value")
    if raw_usd and record.sale_price_rendered is not None:
        record.sale_price = Decimal(raw_usd.replace(",", ""))
    record.list_price = _money(doc, "//h3[contains(.,'List Price')]")
    record.core_deposit = _money(doc, "//li[starts-with(normalize-space(.),'Core Deposit:')]")
    record.page_condition = _first(
        doc, "//li[starts-with(normalize-space(.),'Condition:')]/span"
    )

    stock_nodes = doc.xpath(
        "//*[contains(translate(text(),'instock','INSTOCK'),'IN STOCK')]"
    )
    for node in stock_nodes:
        match = _STOCK.search(_text(node))
        if match:
            record.quantity = int(match.group(1).replace(",", ""))
            break

    _read_tabs(doc, record)
    _read_alternates(doc, record)

    _check(record, pn_requested)
    return record


def _same_pn(left: str, right: str) -> bool:
    return left.strip().upper() == right.strip().upper()


def _check(record: PartRecord, pn_requested: str) -> None:
    """Every way this page could be lying, written down.

    Each one appends to `conflicts`, which routes the row to manual_review
    rather than into the sheet. Nothing here silently corrects a value.
    """
    verdict = rules.condition_code(record.page_condition, pn_requested)
    if verdict.conflict:
        record.conflicts.append(verdict.reason)

    # The portal can resolve a search to a different part, which would write
    # one part's price under another part's number.
    if record.pn_resolved and not _same_pn(record.pn_resolved, pn_requested):
        record.conflicts.append(
            f"asked for {pn_requested!r} but the page is for {record.pn_resolved!r}"
        )

    # A discontinued part and a quote only part both legitimately show no price
    # and no stock. Those absences are expected and are not worth a human's
    # attention; they are recorded as an availability instead, and counted in
    # the daily summary so the client can see how much of the list is affected.
    if record.availability == AVAILABLE:
        if record.sale_price is None:
            record.conflicts.append("part page rendered but carries no USD price")
        elif (
            record.sale_price_rendered is not None
            and record.sale_price_rendered != record.sale_price
        ):
            record.conflicts.append(
                f"hidden price {record.sale_price} does not match the displayed "
                f"Your Price {record.sale_price_rendered}"
            )
        if record.quantity is None:
            record.conflicts.append("part page rendered but carries no stock line")

    if not record.description:
        record.conflicts.append("part page rendered but carries no description")

    if len(record.alt_visible) > 1:
        record.conflicts.append(
            "more than one alternate part number is visible: "
            + ", ".join(f"{label}={value}" for label, value in record.alt_visible)
        )


def _read_tabs(doc, record: PartRecord) -> None:
    """NEW / EXCHANGE / REPAIR SERVICES are sibling part numbers, not tabs.

    Each button swaps the page to a different part number, so the one that is
    active is simply the part number that was requested. The active button
    carries `btn-outline`; the inactive ones carry `btn-outline-off-white`.
    """
    for button in doc.xpath("//button[contains(@onclick,'ChangePNandSubmit')]"):
        label = _text(button)
        target = re.search(r"ChangePNandSubmit\(\s*\d+\s*,\s*'([^']*)'", button.get("onclick", ""))
        if not label or not target:
            continue
        record.tabs[label] = target.group(1)
        classes = button.get("class", "")
        if "btn-outline" in classes and "btn-outline-off-white" not in classes:
            record.tab_active = label


def _read_alternates(doc, record: PartRecord) -> None:
    """Alternate part numbers, skipping the ones the page hides.

    A part can carry more than one block and the extra ones are marked
    `display:none`, so reading the hidden inputs alone would pick up a value no
    operator ever sees. The scope says to take the field under the Create List
    button, which is the visible one.
    """
    for item in doc.xpath("//li[contains(@class,'apo-item')]"):
        link = item.xpath(".//a[contains(@class,'external-link')]")
        if not link:
            continue
        value = _text(link[0])
        label = _text(item).replace(value, "").strip()
        if "display:none" in (item.get("style") or "").replace(" ", ""):
            record.alt_hidden.append((label, value))
            continue
        record.alt_visible.append((label, value))
        if record.alt_pn is None:
            record.alt_pn, record.alt_label = value, label
