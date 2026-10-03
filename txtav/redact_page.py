"""Hide identifying details on a page before it is photographed.

A failure screenshot is the fastest way to work out what went wrong, and also
the easiest way to leak the client's identity into a ticket, a chat thread or a
bug report. The account holder's name, the customer number and the regional
support contacts all sit in the viewport of every part page.

Masking happens in the page, before the shutter, so the sensitive values never
reach the image file at all. Nothing is removed: the elements keep their size
so the layout in the screenshot still matches what an operator would see.
"""

from __future__ import annotations

# Selectors whose text is replaced with blocks before a screenshot. Taken from
# the live portal markup on 2026-10-02.
MASKED = (
    "a.profile",           # the signed in account holder's name
    "a.customer-number",   # "AIRCRAFT PARTS DISTRIBUTION IN <number>"
    "a[href^='mailto:']",  # support email addresses
    "a[href^='tel:']",     # support phone numbers
)

# Blocks containing a heading match these: the whole block goes, because the
# contact details inside it are not individually addressable.
MASKED_BLOCK_HEADINGS = ("REGIONAL SUPPORT",)

STYLE_ID = "txtav-redaction"

# visibility: hidden, not color: transparent. Transparent text is still text:
# it renders, it can be selected, and a background that fails to paint leaves
# it legible. Hidden text is not drawn at all, and the element keeps its box so
# the layout in the screenshot still matches what an operator sees. The ::after
# block is re-shown on top to make clear that something was deliberately
# removed rather than missing.
_CSS = """
.txtav-redacted {
  visibility: hidden !important;
  position: relative !important;
}
.txtav-redacted::after {
  content: "";
  visibility: visible !important;
  position: absolute;
  inset: 0;
  min-height: 1em;
  background: #444;
  border-radius: 2px;
}
"""

# Applied in the page. Marks matching elements rather than deleting them, so
# the layout stays recognisable and unmask() can put it all back.
_APPLY = """
(args) => {
  const [selectors, headings, styleId, css] = args;
  if (!document.getElementById(styleId)) {
    const style = document.createElement('style');
    style.id = styleId;
    style.textContent = css;
    document.head.appendChild(style);
  }
  let marked = 0;
  for (const selector of selectors) {
    for (const node of document.querySelectorAll(selector)) {
      node.classList.add('txtav-redacted');
      marked++;
    }
  }
  for (const heading of headings) {
    for (const node of document.querySelectorAll('h1,h2,h3,h4')) {
      if (!node.textContent.toUpperCase().includes(heading)) continue;
      // The nearest div is a layout wrapper holding only the heading; the
      // contact details live further up, in the module that encloses it.
      const block = node.closest('.module') || node.closest('div') || node.parentElement;
      if (block) { block.classList.add('txtav-redacted'); marked++; }
    }
  }
  return marked;
}
"""

_REMOVE = """
(styleId) => {
  for (const node of document.querySelectorAll('.txtav-redacted')) {
    node.classList.remove('txtav-redacted');
  }
  document.getElementById(styleId)?.remove();
}
"""


def mask(page) -> int:
    """Hide identifying details. Returns how many elements were masked."""
    return int(
        page.evaluate(_APPLY, [list(MASKED), list(MASKED_BLOCK_HEADINGS), STYLE_ID, _CSS])
    )


def unmask(page) -> None:
    """Put the page back as it was."""
    page.evaluate(_REMOVE, STYLE_ID)
