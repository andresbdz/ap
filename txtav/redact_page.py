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

# What is hidden, grouped by what it reveals. Taken from the live portal markup
# on 2026-10-02.
#
# Grouped rather than one flat list so a miss can be named. A single total
# would still look healthy if the portal renamed one class: the other groups
# would keep matching, the count would stay above zero, and that one value
# would quietly appear in every screenshot from then on.
MASKED = {
    "account holder": ("a.profile",),
    "customer number": ("a.customer-number",),
    "support contacts": ("a[href^='mailto:']", "a[href^='tel:']"),
}

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
  const [groups, headings, styleId, css] = args;
  if (!document.getElementById(styleId)) {
    const style = document.createElement('style');
    style.id = styleId;
    style.textContent = css;
    document.head.appendChild(style);
  }
  const counts = {};
  for (const [label, selectors] of groups) {
    let n = 0;
    for (const selector of selectors) {
      for (const node of document.querySelectorAll(selector)) {
        node.classList.add('txtav-redacted');
        n++;
      }
    }
    counts[label] = n;
  }
  for (const heading of headings) {
    for (const node of document.querySelectorAll('h1,h2,h3,h4')) {
      if (!node.textContent.toUpperCase().includes(heading)) continue;
      // The nearest div is a layout wrapper holding only the heading; the
      // contact details live further up, in the module that encloses it.
      const block = node.closest('.module') || node.closest('div') || node.parentElement;
      if (block) {
        block.classList.add('txtav-redacted');
        counts['support contacts'] = (counts['support contacts'] || 0) + 1;
      }
    }
  }
  return counts;
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


def mask(page) -> dict[str, int]:
    """Hide identifying details. Returns how many elements matched per group.

    Callers check `missed()` on the result: a group that matched nothing means
    the portal's markup changed, and the value that group covers would
    otherwise appear in the image.
    """
    groups = [[label, list(selectors)] for label, selectors in MASKED.items()]
    counts = page.evaluate(
        _APPLY, [groups, list(MASKED_BLOCK_HEADINGS), STYLE_ID, _CSS]
    )
    return {label: int(counts.get(label, 0)) for label in MASKED}


def missed(counts: dict[str, int]) -> list[str]:
    """Groups that matched nothing. Empty means everything expected was hidden."""
    return [label for label, count in counts.items() if count == 0]


def unmask(page) -> None:
    """Put the page back as it was."""
    page.evaluate(_REMOVE, STYLE_ID)
