"""Building a part detail URL.

Measured against the live portal on 2026-10-02, not assumed:

- The path form with every character percent encoded works, including for the
  529 part numbers containing a space. `000-110105-79 U` is a real part at
  $2,038.73, distinct from `000-110105-79` at $1,650.67, so that suffix
  carries meaning and must not be normalised away.
- The `?PartNumbers=` query form never works. It serves a generic page titled
  "Back Button" for every input, valid or not. It is also the exact pattern
  the site's robots.txt disallows. It is not used.
- The 1,373 part numbers containing a forward slash return HTTP 404 in the
  path form whether the slash is raw or percent encoded, which is ASP.NET
  refusing double escaped paths. They need the site's own search box, handled
  in navigator.py.
"""

from __future__ import annotations

from urllib.parse import quote

PATH_PREFIX = "/Parts/PartSearch/PartsDetail"

# The site's own search form: GET /Parts/PartSearch?partdesc=<pn>. It redirects
# to the canonical detail page, and for a slash bearing part number the portal
# writes that slash as a hyphen: 000-110105-52U/P resolves to
# /PartsDetail/000-110105-52U-P. We go through the search rather than
# performing that substitution ourselves, because a part literally named
# 000-110105-52U-P could exist and we would silently read the wrong one.
# Unlike the ?PartNumbers= form, this path is not disallowed by robots.txt.
SEARCH_PREFIX = "/Parts/PartSearch"
SEARCH_PARAM = "partdesc"

# The only character that no amount of escaping gets through the portal's
# routing. Everything else survives percent encoding.
PATH_HOSTILE = {"/"}


def normalise_pn(pn: str) -> str:
    """Trim surrounding whitespace only.

    Case is deliberately preserved. Two entries in the list are lower case
    (222a2 and MS21922-4c) and we do not know whether the portal's lookup is
    case sensitive, so the value is sent exactly as the client supplied it.
    Case folding happens only for duplicate detection, in the importer.
    """
    return pn.strip()


def dedupe_key(pn: str) -> str:
    """Case insensitive key used to spot duplicate rows in the source sheet."""
    return normalise_pn(pn).upper()


def needs_search_box(pn: str) -> bool:
    """True when no URL will reach this part and the search box must be used."""
    return any(ch in PATH_HOSTILE for ch in normalise_pn(pn))


def path_url(pn: str, base_url: str) -> str:
    """The /PartsDetail/<pn> form, with the part number fully escaped."""
    escaped = quote(normalise_pn(pn), safe="")
    return f"{base_url.rstrip('/')}{PATH_PREFIX}/{escaped}"


def search_url(pn: str, base_url: str) -> str:
    """The site's search form as a URL. Redirects to the canonical detail page."""
    escaped = quote(normalise_pn(pn), safe="")
    return f"{base_url.rstrip('/')}{SEARCH_PREFIX}?{SEARCH_PARAM}={escaped}"


def detail_url(pn: str, base_url: str) -> str:
    """How to reach this part: the direct path, or the search for a slash."""
    if needs_search_box(pn):
        return search_url(pn, base_url)
    return path_url(pn, base_url)
