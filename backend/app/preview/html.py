"""Section-level HTML surgery on our own generated preview documents.

The preview generator tags every editable block as ``<tag data-section="kebab-id" …>``.
These helpers locate, extract and replace one such element by id so a single section can
be regenerated without touching the rest of the page. We deliberately avoid an HTML-parser
dependency (e.g. BeautifulSoup) — the markup is *ours* and well-formed enough that a
balanced-tag scan over the section's own tag name is reliable and keeps the runtime lean.
"""
from __future__ import annotations
from typing import List, Optional, Tuple

import re

# Tags that never have a closing partner.
_VOID = {
    "area", "base", "br", "col", "embed", "hr", "img", "input",
    "link", "meta", "param", "source", "track", "wbr",
}

# A start tag: <name attrs...> where attrs may contain quoted strings holding '>'.
_TAG = r'<([a-zA-Z][\w-]*)((?:[^>"\']|"[^"]*"|\'[^\']*\')*?)(/?)>'


def _data_attr(attrs: str, name: str) -> Optional[str]:
    m = re.search(name + r'\s*=\s*["\']([^"\']*)["\']', attrs)
    return m.group(1) if m else None


def scan_sections(html: str) -> List[dict]:
    """Return ``[{"id", "label"}]`` for every ``data-section`` element, in document order.

    Duplicate ids are collapsed (first wins); a missing ``data-label`` falls back to a
    title-cased version of the id.
    """
    out: List[dict] = []
    seen = set()
    for m in re.finditer(_TAG, html or "", re.DOTALL):
        sid = _data_attr(m.group(2), "data-section")
        if not sid or sid in seen:
            continue
        seen.add(sid)
        label = _data_attr(m.group(2), "data-label") or sid.replace("-", " ").title()
        out.append({"id": sid, "label": label})
    return out


def _bounds(html: str, section_id: str) -> Optional[Tuple[int, int]]:
    """(start, end) character span of the element whose ``data-section`` == section_id."""
    opening = None
    for m in re.finditer(_TAG, html, re.DOTALL):
        if _data_attr(m.group(2), "data-section") == section_id:
            opening = m
            break
    if opening is None:
        return None

    tag = opening.group(1).lower()
    # Self-closing or void element: the open tag *is* the whole element.
    if opening.group(3) == "/" or tag in _VOID:
        return (opening.start(), opening.end())

    depth = 1
    same_tag = re.compile(
        r'<(/?)(' + re.escape(tag) + r')\b((?:[^>"\']|"[^"]*"|\'[^\']*\')*?)(/?)>',
        re.IGNORECASE | re.DOTALL,
    )
    for t in same_tag.finditer(html, opening.end()):
        if t.group(1) == "/":  # closing tag
            depth -= 1
            if depth == 0:
                return (opening.start(), t.end())
        elif t.group(4) != "/":  # nested open (ignore self-closing)
            depth += 1
    return None  # unbalanced markup — caller treats as "not found"


def extract_section(html: str, section_id: str) -> Optional[str]:
    """Return the outer HTML of the section, or None if it isn't present/balanced."""
    span = _bounds(html or "", section_id)
    return html[span[0]:span[1]] if span else None


def replace_section(html: str, section_id: str, new_fragment: str) -> str:
    """Return ``html`` with the section's element swapped for ``new_fragment``.

    If the section can't be located the original html is returned unchanged.
    """
    span = _bounds(html or "", section_id)
    if not span:
        return html
    return html[: span[0]] + new_fragment + html[span[1]:]
