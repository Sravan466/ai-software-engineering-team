"""Stable element ids, and the edits that need no model.

Selecting "the navbar", "that one link" or "the logo" in the Preview tab needs a name
for every element, not just every section. `tag` gives each element in `<body>` a
`data-oid`, keeping any it already has, so an id survives every later edit that does
not remove its element. The numbering continues from the highest id already present:
tagging is deterministic, so an old mockup drawn before ids existed gets the same ids
every time it is read, whether or not they were ever saved.

`apply_ops` is the other half: a change to one element's text, Tailwind classes or
`src`/`alt`/`href`, found by its id and made in place. No model is asked, because
none is needed to swap `text-4xl` for `text-5xl`.

Some elements cannot be edited this way. One inside a `<template>` is the pattern a
list draws once per record: its text is overwritten by the record, and changing its
classes would change every row at once. Those are refused with a reason the Preview
tab shows, and the change goes to the model instead, scoped to that element.
"""
from __future__ import annotations

import re
from html import escape
from typing import Iterable, List, Optional, Tuple

from app.preview import html as H

ATTR = "data-oid"
_PREFIX = "e"

#: Skipped whole: their contents are not markup a person sees or selects.
_OPAQUE = re.compile(
    r"<(script|style|textarea|title)\b[^>]*>[\s\S]*?</\1\s*>|<!--[\s\S]*?-->",
    re.IGNORECASE,
)
#: An SVG is one thing to select: its paths are drawing, not layout.
_SVG = re.compile(r"<svg\b(?:[^>\"']|\"[^\"]*\"|'[^']*')*>[\s\S]*?</svg\s*>", re.IGNORECASE)
#: Elements that are structure, not something a person picks.
_SKIP = {"template", "br", "wbr", "html", "head", "body", "meta", "link", "base", "source", "track", "param"}
_ID = re.compile(r"^" + _PREFIX + r"(\d+)$")


class PatchRefused(ValueError):
    """An op that cannot be applied without a model, and why."""

    def __init__(self, oid: str, reason: str) -> None:
        super().__init__(reason)
        self.oid = oid
        self.reason = reason


def _body_span(html: str) -> Tuple[int, int]:
    open_ = re.search(r"<body\b[^>]*>", html, re.IGNORECASE)
    close = html.lower().rfind("</body>")
    start = open_.end() if open_ else 0
    end = close if close > start else len(html)
    return start, end


def _next_index(html: str) -> int:
    top = 0
    for value in H.attr_values(html, ATTR):
        m = _ID.match(value)
        if m:
            top = max(top, int(m.group(1)))
    return top + 1


def tag(html: str) -> str:
    """The document with every selectable element in `<body>` carrying a `data-oid`."""
    if not html:
        return html
    start, end = _body_span(html)
    body = html[start:end]
    counter = [_next_index(html)]

    def tag_start(m: "re.Match") -> str:
        name = m.group(1).lower()
        if name in _SKIP or H.attr(m.group(2), ATTR) is not None:
            return m.group(0)
        oid = f"{_PREFIX}{counter[0]}"
        counter[0] += 1
        return f"<{m.group(1)}{m.group(2)} {ATTR}=\"{oid}\"{' /' if m.group(3) else ''}>"

    out: List[str] = []
    pos = 0
    # Opaque blocks are copied verbatim; an <svg> gets an id on its own tag only.
    skip = re.compile(_OPAQUE.pattern + "|" + _SVG.pattern, re.IGNORECASE)
    for block in skip.finditer(body):
        out.append(re.sub(H._TAG, tag_start, body[pos: block.start()], flags=re.DOTALL))
        chunk = block.group(0)
        if chunk[:4].lower() == "<svg":
            first = re.match(H._TAG, chunk, re.DOTALL)
            chunk = tag_start(first) + chunk[first.end():] if first else chunk
        out.append(chunk)
        pos = block.end()
    out.append(re.sub(H._TAG, tag_start, body[pos:], flags=re.DOTALL))
    return html[:start] + "".join(out) + html[end:]


def has_ids(html: str) -> bool:
    return f"{ATTR}=" in (html or "")


# ── finding one element ──────────────────────────────────────────────────────
def locate(html: str, oid: str) -> Optional[Tuple["re.Match", Tuple[int, int]]]:
    """The start-tag match and the (start, end) span of the element with this id."""
    for m in re.finditer(H._TAG, html or "", re.DOTALL):
        if H.attr(m.group(2), ATTR) == oid:
            span = H._element_span(html, m)
            return (m, span) if span else None
    return None


def outer(html: str, oid: str) -> Optional[str]:
    found = locate(html, oid)
    return html[found[1][0]: found[1][1]] if found else None


def replace(html: str, oid: str, fragment: str) -> str:
    found = locate(html, oid)
    if not found:
        return html
    start, end = found[1]
    return html[:start] + fragment + html[end:]


def _template_spans(html: str) -> List[Tuple[int, int]]:
    spans = []
    for m in re.finditer(r"<template\b[^>]*>", html, re.IGNORECASE):
        close = html.lower().find("</template>", m.end())
        if close != -1:
            spans.append((m.start(), close))
    return spans


def in_template(html: str, position: int) -> bool:
    return any(s <= position < e for s, e in _template_spans(html))


def ancestors(html: str, oid: str) -> List[str]:
    """The start tags that enclose the element, outermost first — context for a model."""
    found = locate(html, oid)
    if not found:
        return []
    at = found[1][0]
    out = []
    for m in re.finditer(H._TAG, html[:at], re.DOTALL):
        name = m.group(1).lower()
        if name in {"html", "head"} or name in H._VOID or m.group(3) == "/":
            continue
        span = H._element_span(html, m)
        if span and span[0] < at < span[1]:
            out.append(m.group(0))
    return out[-6:]


# ── the ops ──────────────────────────────────────────────────────────────────
#: A Tailwind class: utilities, variants, arbitrary values and opacity modifiers — and
#: nothing that could close the attribute or open another.
_CLASS = re.compile(r"^[A-Za-z0-9_:\-\[\]/\.#%(),!+*]{1,80}$")
_URL_ATTRS = {"href", "src"}
_ATTRS = {"href", "src", "alt", "title", "aria-label", "placeholder"}
#: Text the runtime writes on load: typing over it would be undone on the next render.
_FILLED = re.compile(r"\sdata-(field|stat|count|year)\b")
#: A text edit replaces an element's words. It may hold inline formatting — that is
#: flattened — but not structure a person would lose without noticing.
_BLOCK_CHILD = re.compile(
    r"<(div|section|article|ul|ol|li|nav|header|footer|main|form|table|img|svg|button|a|input|select|textarea)\b",
    re.IGNORECASE,
)


def _safe_url(value: str) -> bool:
    v = value.strip().lower()
    if v.startswith(("javascript:", "vbscript:")):
        return False
    if v.startswith("data:"):
        return v.startswith("data:image/")
    return True


def _set_text(html: str, m: "re.Match", span: Tuple[int, int], text: str) -> str:
    inner_start = m.end()
    close = html.rfind("</", inner_start, span[1])
    if close == -1:
        raise PatchRefused(H.attr(m.group(2), ATTR) or "", "That element has no text to change.")
    inner = html[inner_start:close]
    if _BLOCK_CHILD.search(inner):
        raise PatchRefused(
            H.attr(m.group(2), ATTR) or "",
            "That element holds other elements. Select the words themselves to change them.",
        )
    return html[:inner_start] + escape(text, quote=False) + html[close:]


def _set_classes(m: "re.Match", add: Iterable[str], remove: Iterable[str]) -> str:
    attrs = m.group(2)
    current = (H.attr(attrs, "class") or "").split()
    drop = set(remove)
    kept = [c for c in current if c not in drop]
    for c in add:
        if c not in kept:
            kept.append(c)
    new_attrs = H.with_attrs(attrs, {"class": " ".join(kept) or None})
    return f"<{m.group(1)}{new_attrs}{' /' if m.group(3) else ''}>"


def _set_attr(m: "re.Match", name: str, value: Optional[str]) -> str:
    new_attrs = H.with_attrs(m.group(2), {name: value})
    return f"<{m.group(1)}{new_attrs}{' /' if m.group(3) else ''}>"


def apply_ops(html: str, ops: List[dict]) -> str:
    """Every op applied in order, or `PatchRefused` naming the first that cannot be."""
    for op in ops:
        oid = str(op.get("oid") or "")
        kind = op.get("kind")
        found = locate(html, oid)
        if not found:
            raise PatchRefused(oid, "That element isn't in the current mockup any more. Reload and select it again.")
        m, span = found
        if in_template(html, m.start()):
            raise PatchRefused(
                oid,
                "This is drawn once per record from a list's template, so it can't be changed directly. "
                "Describe the change and the crew will update every row.",
            )
        if kind == "text":
            if _FILLED.search(m.group(0)):
                raise PatchRefused(oid, "This text is filled in from the mockup's data, so it can't be typed over.")
            html = _set_text(html, m, span, str(op.get("text") or ""))
        elif kind == "classes":
            add = [str(c) for c in op.get("add") or []]
            remove = [str(c) for c in op.get("remove") or []]
            bad = [c for c in add + remove if not _CLASS.match(c)]
            if bad:
                raise PatchRefused(oid, f"'{bad[0][:40]}' isn't a class name the mockup can use.")
            html = html[: m.start()] + _set_classes(m, add, remove) + html[m.end():]
        elif kind == "attr":
            name = str(op.get("name") or "").lower()
            if name not in _ATTRS:
                raise PatchRefused(oid, f"The '{name[:30]}' attribute can't be changed here.")
            value = op.get("value")
            value = None if value is None else str(value)[:2000]
            if value is not None and name in _URL_ATTRS and not _safe_url(value):
                raise PatchRefused(oid, "That address can't be used in the mockup.")
            html = html[: m.start()] + _set_attr(m, name, value) + html[m.end():]
        else:
            raise PatchRefused(oid, f"Unknown change '{kind}'.")
    return html
