"""Section-level HTML surgery on our own generated preview documents.

The preview generator tags every editable block as ``<tag data-section="kebab-id" …>``.
These helpers locate, extract and replace one such element by id so a single section can
be regenerated without touching the rest of the page. We deliberately avoid an HTML-parser
dependency (e.g. BeautifulSoup) — the markup is *ours* and well-formed enough that a
balanced-tag scan over the section's own tag name is reliable and keeps the runtime lean.

The same scan now also serves generation, which assembles a site from one fragment per
section: pages are ``data-route`` elements, a section knows which page it is on, and a
model's fragment is cleaned of anything that could run code or leave the sandbox before
it is stitched in.
"""
from __future__ import annotations
from typing import Dict, List, Optional, Tuple

import re

# Tags that never have a closing partner.
_VOID = {
    "area", "base", "br", "col", "embed", "hr", "img", "input",
    "link", "meta", "param", "source", "track", "wbr",
}

# A start tag: <name attrs...> where attrs may contain quoted strings holding '>'.
_TAG = r'<([a-zA-Z][\w-]*)((?:[^>"\']|"[^"]*"|\'[^\']*\')*?)(/?)>'


def _data_attr(attrs: str, name: str) -> Optional[str]:
    # Anchored on a boundary so `data-route` is not read out of `data-route-title`.
    m = re.search(r'(?:^|\s)' + re.escape(name) + r'\s*=\s*(["\'])(.*?)\1', attrs or "", re.DOTALL)
    return m.group(2) if m else None


def _element_span(html: str, opening: "re.Match") -> Optional[Tuple[int, int]]:
    """The balanced (start, end) span of the element that `opening` starts."""
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


def route_spans(html: str) -> List[dict]:
    """``[{"path", "title", "start", "end"}]`` for every ``data-route`` page, in order."""
    out: List[dict] = []
    for m in re.finditer(_TAG, html or "", re.DOTALL):
        path = _data_attr(m.group(2), "data-route")
        if path is None:
            continue
        span = _element_span(html, m)
        if span is None:
            continue
        out.append(
            {
                "path": path,
                "title": _data_attr(m.group(2), "data-route-title") or path,
                "start": span[0],
                "end": span[1],
            }
        )
    return out


def scan_sections(html: str) -> List[dict]:
    """``[{"id", "label", "route", "kind"}]`` for every ``data-section`` element, in order.

    Duplicate ids are collapsed (first wins); a missing ``data-label`` falls back to a
    title-cased version of the id. ``route`` is the page the section sits on, and None
    for one every page shares (the header, the footer) — and for any section of a
    document drawn before mockups had pages.
    """
    out: List[dict] = []
    seen = set()
    routes = route_spans(html)
    for m in re.finditer(_TAG, html or "", re.DOTALL):
        sid = _data_attr(m.group(2), "data-section")
        if not sid or sid in seen:
            continue
        seen.add(sid)
        label = _data_attr(m.group(2), "data-label") or sid.replace("-", " ").title()
        route = next((r["path"] for r in routes if r["start"] <= m.start() < r["end"]), None)
        out.append(
            {
                "id": sid,
                "label": label,
                "route": route,
                "kind": _data_attr(m.group(2), "data-kind"),
            }
        )
    return out


def _bounds(html: str, section_id: str) -> Optional[Tuple[int, int]]:
    """(start, end) character span of the element whose ``data-section`` == section_id."""
    for m in re.finditer(_TAG, html, re.DOTALL):
        if _data_attr(m.group(2), "data-section") == section_id:
            return _element_span(html, m)
    return None


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


# ── one fragment, as a model returns it ──────────────────────────────────────
def outer_element(fragment: str) -> Optional[Tuple[str, str, str]]:
    """``(tag, attrs, inner)`` when the fragment is exactly one element, else None."""
    text = (fragment or "").strip()
    m = re.match(_TAG, text, re.DOTALL)
    if not m or m.start() != 0:
        return None
    span = _element_span(text, m)
    if span is None or span[1] != len(text):
        return None
    tag = m.group(1).lower()
    inner = text[m.end(): span[1]]
    inner = re.sub(r"</\s*" + re.escape(tag) + r"\s*>\s*$", "", inner, flags=re.IGNORECASE)
    return tag, m.group(2), inner


def attr(attrs: str, name: str) -> Optional[str]:
    """One attribute's value out of an attribute string."""
    return _data_attr(attrs, name)


def _escape_attr(value: str) -> str:
    return (
        str(value)
        .replace("&", "&amp;")
        .replace('"', "&quot;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def with_attrs(attrs: str, updates: Dict[str, Optional[str]]) -> str:
    """``attrs`` with each named attribute set (or removed, for None), others kept."""
    out = attrs or ""
    for name, value in updates.items():
        out = re.sub(
            r'\s+' + re.escape(name) + r'(\s*=\s*("[^"]*"|\'[^\']*\'|[^\s>]+))?(?=\s|/?$)',
            "",
            out,
        )
        if value is not None:
            out = f'{out} {name}="{_escape_attr(value)}"'
    return out


def wrap(tag: str, attrs: str, inner: str) -> str:
    return f"<{tag}{attrs}>{inner}</{tag}>"


# ── cleaning what a model wrote ──────────────────────────────────────────────
_FENCE = re.compile(r"```(?:html|HTML)?\s*([\s\S]*?)```")
#: Elements a section must never contain. Scripts would be model-written logic — the
#: runtime is the logic — and a stray <style> or <link> restyles the whole site.
_STRIP_BLOCKS = re.compile(
    r"<(script|style|iframe|object|embed|noscript)\b[\s\S]*?</\1\s*>|"
    r"<(script|style|iframe|object|embed|link|meta|base)\b[^>]*/?>",
    re.IGNORECASE,
)
_DOC_TAGS = re.compile(r"</?(html|head|body)\b[^>]*>|<!doctype[^>]*>", re.IGNORECASE)
_ON_ATTR = re.compile(r"""\s+on[a-z]+\s*=\s*("[^"]*"|'[^']*'|[^\s>]+)""", re.IGNORECASE)
_JS_URL = re.compile(r"""(href|src|action|formaction)\s*=\s*(["'])\s*javascript:[^"']*\2""", re.IGNORECASE)
_FORM_ACTION = re.compile(r"""(<form\b[^>]*?)\s+(action|method|target)\s*=\s*("[^"]*"|'[^']*'|[^\s>]+)""", re.IGNORECASE)
#: Every other way a fragment can ask the network for a picture. Each is a guess at a
#: URL, and a guess that 404s is a console error on load — <img src> is redrawn by
#: the platform; these have no platform version, so they go.
_SRCSET = re.compile(r"""\s+(srcset|sizes|poster)\s*=\s*("[^"]*"|'[^']*'|[^\s>]+)""", re.IGNORECASE)
_MEDIA = re.compile(r"<(video|audio|picture)\b[\s\S]*?</\1\s*>|<source\b[^>]*/?>", re.IGNORECASE)
_CSS_URL = re.compile(r"""url\(\s*(['"]?)(?!data:)[^)'"]*\1\s*\)""", re.IGNORECASE)


def clean_fragment(text: str) -> str:
    """Trim a model response to markup, and remove anything that could run or escape.

    Cleaned, not rejected: a hero with a stray `onclick` is a perfectly good hero once
    the handler is gone, and the sandbox would have blocked it anyway — with a console
    error, which is the one thing the finished page must not have.
    """
    t = (text or "").strip()
    fence = _FENCE.search(t)
    if fence:
        t = fence.group(1).strip()
    first, last = t.find("<"), t.rfind(">")
    if first == -1 or last <= first:
        return ""
    t = t[first: last + 1]
    t = _STRIP_BLOCKS.sub("", t)
    t = _DOC_TAGS.sub("", t)
    t = _ON_ATTR.sub("", t)
    t = _JS_URL.sub(r'\1="#"', t)
    t = _MEDIA.sub("", t)
    t = _SRCSET.sub("", t)
    t = _CSS_URL.sub("none", t)
    while True:
        stripped = _FORM_ACTION.sub(r"\1", t)
        if stripped == t:
            break
        t = stripped
    return t.strip()


def tags_balance(fragment: str) -> bool:
    """Whether every non-void open tag in the fragment has its close, in order."""
    stack: List[str] = []
    for m in re.finditer(r'<(/?)([a-zA-Z][\w-]*)((?:[^>"\']|"[^"]*"|\'[^\']*\')*?)(/?)>', fragment or ""):
        closing, name, _attrs, selfclose = m.group(1), m.group(2).lower(), m.group(3), m.group(4)
        if name in _VOID or selfclose:
            continue
        if not closing:
            stack.append(name)
            continue
        if name not in stack:
            return False
        # Browsers close intervening elements implicitly (a <p> left open inside a
        # <div>); tolerate that, but not a close tag nothing opened.
        while stack and stack[-1] != name:
            stack.pop()
        stack.pop()
    return not stack


def attr_values(html: str, name: str) -> List[str]:
    """Every value of attribute `name` in the markup, in order."""
    return [
        m.group(2)
        for m in re.finditer(r'(?:\s)' + re.escape(name) + r'\s*=\s*(["\'])(.*?)\1', html or "", re.DOTALL)
    ]


def elements_with(html: str, name: str) -> List[Tuple[str, str]]:
    """``(tag, attrs)`` for every start tag carrying attribute `name`."""
    out: List[Tuple[str, str]] = []
    for m in re.finditer(_TAG, html or "", re.DOTALL):
        if _data_attr(m.group(2), name) is not None:
            out.append((m.group(1).lower(), m.group(2)))
    return out


def element_inner(html: str, name: str, value: str) -> Optional[str]:
    """Inner markup of the first element whose attribute `name` equals `value`."""
    for m in re.finditer(_TAG, html or "", re.DOTALL):
        if _data_attr(m.group(2), name) == value:
            span = _element_span(html, m)
            if span is None:
                return None
            body = html[m.end(): span[1]]
            return re.sub(r"</\s*" + re.escape(m.group(1)) + r"\s*>\s*$", "", body, flags=re.IGNORECASE)
    return None


def visible_text(html: str) -> str:
    """The words a person would see, roughly — enough to tell an empty section."""
    text = re.sub(r"<template\b[\s\S]*?</template>", " ", html or "", flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", text)
    return " ".join(text.split())


_IMG_SRC = re.compile(r"""(<img\b[^>]*?\bsrc\s*=\s*)(["'])(.*?)\2""", re.IGNORECASE | re.DOTALL)


def rewrite_images(html: str, replace) -> Tuple[str, int]:
    """Pass every ``<img src>`` through ``replace(src, tag) -> new src``. Returns the count."""
    count = 0

    def sub(m: "re.Match") -> str:
        nonlocal count
        new = replace(m.group(3), m.group(0))
        if new == m.group(3):
            return m.group(0)
        count += 1
        return f"{m.group(1)}{m.group(2)}{new}{m.group(2)}"

    return _IMG_SRC.sub(sub, html or ""), count
