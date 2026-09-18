"""Pass 5 — one model call per section, each held to the contract its kind declares.

The single-shot mockup was one call for a whole product, and the model spent its
budget on a `<head>` and five thin blocks. Here every section gets a call of its own,
sized to the window of the model that will answer it, so a page is as long as its
sections are good — and one weak section is retried or replaced without touching the
others. This is the treatment `edit_section` already proved: one `data-section` at a
time is the reliable half of this feature.

What makes a section *work* is its bindings: a list is only a list if it has a
`data-list` with a `<template>`, a form only stores if it names its collection. Those
are checked, not hoped for. A section that misses them is sent back once with the
problems named; if it misses them again, the platform's template takes its place,
so the finished page always has the logic its plan promised. References the model
got slightly wrong — a link to a page that does not exist, a field the collection
does not have — are corrected in place rather than costing the section.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Optional

from app.preview import html as H
from app.preview import templates
from app.preview.brief import SiteBrief
from app.preview.design import DesignSystem
from app.preview.plan import BOUND_KINDS, CollectionPlan, SectionPlan, SitePlan
from app.schemas.llm import ChatMessage

#: The wrapper element each shared kind is drawn in; everything else is a <section>.
WRAPPER_TAG = {"nav": "header", "footer": "footer"}
#: Classes the platform puts on those wrappers — the sticky header is chrome, and a
#: model that forgot it would leave navigation scrolling off the top of every page.
WRAPPER_CLASS = {
    "nav": "sticky top-0 z-40 border-b border-line bg-surface/90 backdrop-blur",
    "footer": "border-t border-line bg-surface",
}

SYSTEM = (
    "You are a senior front-end designer building ONE section of a clickable product "
    "prototype. You write semantic, accessible, responsive HTML styled with Tailwind utility "
    "classes and the design-system classes you are given.\n\n"
    "Hard rules:\n"
    "- Output ONLY this one section's HTML. No <html>, <head>, <body>, <script> or <style>, "
    "no markdown fences, no commentary.\n"
    "- Never write JavaScript or on* attributes. The platform ships the behaviour; you wire "
    "it with the data-* attributes in the contract, exactly as written.\n"
    "- Link to pages only with href=\"#/path\" using the paths in the site map.\n"
    "- For pictures use <img src=\"\" alt=\"what it shows\" width=\"W\" height=\"H\">; the "
    "platform draws them.\n"
    "- Write specific, believable copy for this product. No lorem ipsum, no placeholders "
    "like 'Feature 1'. Do not hardcode a year."
)


@dataclass
class SectionOutcome:
    plan: SectionPlan
    html: str
    #: generated | repaired | fallback
    status: str
    problems: list[str] = field(default_factory=list)
    fixes: list[str] = field(default_factory=list)
    calls: int = 0

    def report(self) -> dict:
        return {
            "id": self.plan.id,
            "kind": self.plan.kind,
            "label": self.plan.label,
            "route": self.plan.route or None,
            "collection": self.plan.collection or None,
            "status": self.status,
            "problems": self.problems[:6],
            "fixes": self.fixes[:6],
            "bytes": len(self.html.encode("utf-8")),
        }


# ── the contract each kind is held to ────────────────────────────────────────
def _fields_line(c: CollectionPlan) -> str:
    return ", ".join(
        f"{f.name} ({f.type}{': ' + ' | '.join(f.options) if f.options else ''})" for f in c.fields
    )


def _sortable(c: CollectionPlan) -> list[str]:
    return [f.name for f in c.fields if f.type != "longtext" and f.type != "boolean"]


def contract(section: SectionPlan, site: SitePlan) -> str:
    """The binding contract for one section, with this site's names filled in."""
    kind = section.kind
    c = site.collection(section.collection) if section.collection else None
    routes = ", ".join(f'#{r.path} ("{r.title}")' for r in site.routes)

    if kind == "nav":
        form_page = next(
            (r.path for r in site.routes for s in r.sections if s.kind == "form"), site.routes[-1].path
        )
        return (
            "- The product name on the left as a link to #/ (a small logo mark beside it is welcome).\n"
            f"- A <nav> with one link per page, exactly these hrefs: {routes}. Use the page titles "
            "as link text. The runtime marks the current page's link with aria-current=\"page\"; "
            "style it with aria-[current=page]:text-primary.\n"
            f"- One primary action on the right: <a href=\"#{form_page}\" class=\"btn btn-primary\">…</a>.\n"
            "- On small screens keep the page links visible in a row that scrolls sideways "
            "(flex overflow-x-auto) — never hide navigation.\n"
            "- Only the header's content: the platform draws the sticky bar around it."
        )
    if kind == "footer":
        return (
            f"- The product name and its tagline, and links to every page: {routes}.\n"
            f"- A copyright line written exactly as: &copy; <span data-year></span> {site.product}\n"
            "- Only the footer's content: the platform draws the bar around it."
        )
    if kind == "hero":
        return (
            "- An <h1 class=\"font-display …\"> headline specific to this product, one supporting "
            "sentence, and two actions: <a class=\"btn btn-primary\" href=\"#/…\"> and "
            "<a class=\"btn btn-secondary\" href=\"#/…\"> linking to pages in the site map.\n"
            "- Beside it, a product-like visual made of HTML — a card with a few figures or a short "
            "list — or an <img> with a descriptive alt."
        )
    if kind == "features":
        return (
            "- An <h2> heading, a one-line intro, then a grid of 3–6 cards (class=\"card\"), each "
            "with an inline <svg> icon (viewBox=\"0 0 24 24\", stroke=\"currentColor\", "
            "fill=\"none\"), a title and one sentence. Draw them from the product's features."
        )
    if kind == "stats" and c:
        numeric = [f.name for f in c.fields if f.type in ("number", "currency", "percent")]
        select = next((f for f in c.fields if f.type == "select" and f.options), None)
        lines = [
            "- An <h2> heading, then 3–4 figure cards. The runtime computes each number live — "
            "write the number's element EMPTY with a data-stat attribute:",
            f'  <p class="font-display text-4xl font-bold" data-stat="{c.name}.count"></p>  → how many {c.label.lower()}',
        ]
        if numeric:
            lines.append(
                f'  data-stat="{c.name}.sum.FIELD" or "{c.name}.avg.FIELD" → total / average, '
                f"FIELD one of: {', '.join(numeric)}"
            )
        if select:
            lines.append(
                f'  data-stat="{c.name}.count" data-stat-where="{select.name}={select.options[0]}" '
                f"→ how many have that {select.label.lower()}"
            )
        lines.append("- Give every figure a label and a one-line caption.")
        return "\n".join(lines)
    if kind == "list" and c:
        select = next((f for f in c.fields if f.type == "select" and f.options), None)
        boolean = next((f for f in c.fields if f.type == "boolean"), None)
        filter_line = (
            f'  <select class="select" data-filter="{c.name}" data-filter-field="{select.name}">'
            f'<option value="">All</option>'
            + "".join(f'<option value="{o}">{o}</option>' for o in select.options)
            + "</select>\n"
            if select
            else ""
        )
        return (
            "The runtime renders one copy of the <template> per record and wires every control — "
            "you write no JavaScript.\n"
            "- An <h2> heading, then a toolbar with:\n"
            f'  <input type="search" class="input" data-filter="{c.name}" placeholder="Search {c.label.lower()}…">\n'
            + filter_line
            + f'  <select class="select" data-sort="{c.name}"><option value="">Sort by…</option>'
            '<option value="FIELD:asc">…</option><option value="FIELD:desc">…</option></select> '
            f"(FIELD one of: {', '.join(_sortable(c))})\n"
            f'- The records: <div data-list="{c.name}" class="grid gap-5 sm:grid-cols-2 lg:grid-cols-3">'
            '<template><article class="card">…</article></template></div>\n'
            "  Inside the template show values on EMPTY elements with data-field, e.g. "
            f'<h3 data-field="{c.title_field.name}"></h3>'
            + (f' <span class="badge" data-field="{select.name}"></span>' if select else "")
            + f". Only these fields exist: {_fields_line(c)}.\n"
            '- Per-record action inside the template: <button type="button" class="btn btn-ghost" '
            'data-action="remove">Remove</button>'
            + (
                f'; for {boolean.name} a checkbox <input type="checkbox" data-toggle="{boolean.name}">'
                if boolean
                else ""
            )
            + ".\n"
            f'- After the list: <p data-empty="{c.name}" hidden>No {c.label.lower()} match.</p> and a '
            f'count <span data-count="{c.name}"></span> {c.label.lower()}.'
        )
    if kind == "table" and c:
        return (
            "The runtime renders one copy of the <template> per record and wires every control — "
            "you write no JavaScript.\n"
            f'- An <h2> heading, a search box <input type="search" class="input" data-filter="{c.name}" '
            f'placeholder="Search…"> and a count <span data-count="{c.name}"></span>.\n'
            '- <table class="table"><thead><tr><th><button type="button" '
            f'data-sort="{c.name}" data-sort-field="FIELD">Label</button></th>…</tr></thead>'
            f'<tbody data-list="{c.name}"><template><tr><td data-field="FIELD"></td>…</tr>'
            "</template></tbody></table> — one sortable column per field worth showing.\n"
            f"  Only these fields exist: {_fields_line(c)}.\n"
            f'- <p data-empty="{c.name}" hidden>No {c.label.lower()} match.</p> after the table.'
        )
    if kind == "form" and c:
        list_page = next(
            (r.path for r in site.routes for s in r.sections
             if s.kind in ("list", "table") and s.collection == c.name),
            "",
        )
        redirect = f' data-redirect="{list_page}"' if list_page and list_page != section.route else ""
        controls = []
        for f in c.fields:
            control = {
                "longtext": "<textarea class=\"textarea\">",
                "email": "<input type=\"email\" class=\"input\">",
                "url": "<input type=\"url\" class=\"input\">",
                "number": "<input type=\"number\" step=\"any\" class=\"input\">",
                "currency": "<input type=\"number\" step=\"any\" min=\"0\" class=\"input\">",
                "percent": "<input type=\"number\" min=\"0\" max=\"100\" class=\"input\">",
                "date": "<input type=\"date\" class=\"input\">",
                "datetime": "<input type=\"date\" class=\"input\">",
                "select": "<select class=\"select\"> with options " + " | ".join(f.options),
                "boolean": "<input type=\"checkbox\">",
            }.get(f.type, "<input type=\"text\" class=\"input\">")
            controls.append(f"  {f.name}{' (required)' if f.required else ''}: {control}")
        return (
            f'- <form data-form="{c.name}" data-success="{c.label.rstrip("s")} added."{redirect}> — '
            "the runtime validates it, stores the record, updates every list and figure, and "
            "resets it. No action or method attributes.\n"
            "- One <label class=\"label\"> + control per field, the control's name attribute "
            "EXACTLY the field name:\n" + "\n".join(controls) + "\n"
            "- Add the required attribute to required fields, and under each control an empty "
            '<p class="form-error" data-error-for="FIELD" hidden></p> for its message.\n'
            '- A submit button: <button type="submit" class="btn btn-primary">…</button>.'
        )
    if kind == "testimonials":
        return (
            "- An <h2> heading and 3 quote cards (<figure class=\"card\"> with a <blockquote>), each "
            "naming a believable person and their role, with an initials avatar (no photo needed)."
        )
    if kind == "pricing":
        return (
            "- An <h2> heading and 3 plans in cards, the middle one highlighted (ring-2 ring-primary), "
            "each with a price, 4–5 included features taken from the product, and a button linking "
            "to a page in the site map."
        )
    if kind == "faq":
        return (
            "- An <h2> heading and 4–6 questions as native disclosure widgets: "
            "<details><summary>Question</summary><p>Answer</p></details> — no JavaScript needed."
        )
    if kind == "cta":
        return (
            "- A bold closing band (<div class=\"rounded-xl bg-primary text-on-primary …\">) with a "
            "headline, one sentence and a button (class=\"btn bg-surface text-primary\") linking to a "
            "page in the site map."
        )
    return (
        "- An <h2> heading and explanatory copy for this product — for example how it works in "
        "three numbered steps (an <ol> of cards)."
    )


# ── checking a fragment against its contract ─────────────────────────────────
_ROUTE_HREF = re.compile(r"""href\s*=\s*(["'])#(/[^"']*)\1""", re.IGNORECASE)
_DATA_TO = re.compile(r"""data-to\s*=\s*(["'])([^"']*)\1""", re.IGNORECASE)
_HARD_YEAR = re.compile(r"(©|&copy;|\(c\))\s*(19|20)\d\d\b", re.IGNORECASE)


def _norm_path(path: str) -> str:
    p = "/" + path.strip().strip("/")
    return p if p != "//" else "/"


def check(section: SectionPlan, inner: str, site: SitePlan) -> tuple[list[str], list[str]]:
    """(fatal, fixable) problems with one fragment.

    Fatal ones cost the section its place if a repair does not fix them: a list with no
    template is not a list. Fixable ones are wrong references the platform can correct
    itself — named to the model first, because the model's correction keeps its copy.
    """
    fatal: list[str] = []
    fixable: list[str] = []
    kind = section.kind

    if not inner.strip() or len(H.visible_text(inner).split()) < 3:
        fatal.append("the section is empty — write its full content")
        return fatal, fixable
    if not H.tags_balance(inner):
        fatal.append("the markup does not balance — close every tag you open")

    paths = {r.path for r in site.routes}
    linked = {_norm_path(m.group(2)) for m in _ROUTE_HREF.finditer(inner)}
    linked |= {_norm_path(m.group(2)) for m in _DATA_TO.finditer(inner)}
    unknown = sorted(p for p in linked if p not in paths)
    if unknown:
        fixable.append(
            f"links to pages that do not exist ({', '.join('#' + p for p in unknown)}); "
            f"use only {', '.join('#' + p for p in sorted(paths))}"
        )

    if kind == "nav":
        missing = [r.path for r in site.routes if r.path not in linked]
        if missing:
            fatal.append(
                "the header must link to every page — missing "
                + ", ".join(f'href="#{p}"' for p in missing)
            )
    if kind == "footer" and _HARD_YEAR.search(inner):
        fixable.append("the copyright year is hardcoded — write <span data-year></span> instead")

    names = {c.name for c in site.collections}
    for attr_name in ("data-list", "data-form", "data-filter", "data-sort", "data-count", "data-empty"):
        bad = sorted({v for v in H.attr_values(inner, attr_name) if v not in names})
        if bad:
            fixable.append(f"{attr_name} names collections that do not exist ({', '.join(bad)})")

    c = site.collection(section.collection) if section.collection else None
    if c is not None:
        valid = {f.name for f in c.fields}
        bad_fields = sorted({v for v in H.attr_values(inner, "data-field") if v not in valid})
        bad_fields += sorted(
            {v for v in H.attr_values(inner, "data-filter-field") if v not in valid} - set(bad_fields)
        )
        if bad_fields:
            fixable.append(
                f"uses fields {c.name} does not have ({', '.join(bad_fields)}); "
                f"the fields are {', '.join(sorted(valid))}"
            )

    if kind in ("list", "table") and c is not None:
        body = H.element_inner(inner, "data-list", c.name)
        if body is None:
            fatal.append(f'there is no element with data-list="{c.name}" holding the records')
        elif "<template" not in body.lower():
            fatal.append(f'the data-list="{c.name}" element needs a <template> for one record')
        else:
            shown = [v for v in H.attr_values(body, "data-field") if v in {f.name for f in c.fields}]
            if not shown:
                fatal.append("the <template> shows no record values — add data-field elements")
        controls = [v for v in H.attr_values(inner, "data-filter") + H.attr_values(inner, "data-sort")
                    if v == c.name]
        if not controls:
            fatal.append(
                f'there is no search, filter or sort control — add data-filter="{c.name}" and '
                f'data-sort="{c.name}" controls'
            )

    if kind == "form" and c is not None:
        body = H.element_inner(inner, "data-form", c.name)
        if body is None:
            fatal.append(f'there is no <form data-form="{c.name}">')
        else:
            named = {v for v in H.attr_values(body, "name")}
            if not named & {f.name for f in c.fields}:
                fatal.append(
                    f"the form has no controls named after the fields ({', '.join(f.name for f in c.fields)})"
                )
            if 'type="submit"' not in body.replace("'", '"').lower() and "<button" not in body.lower():
                fatal.append('the form needs a <button type="submit">')

    if kind == "stats" and c is not None:
        stats = [v for v in H.attr_values(inner, "data-stat") if v.split(".")[0] == c.name]
        if not stats:
            fatal.append(f'no figure is computed — add empty elements with data-stat="{c.name}.count"')

    return fatal, fixable


def repair_in_place(section: SectionPlan, inner: str, site: SitePlan) -> tuple[str, list[str]]:
    """Correct the references a model got wrong without asking it again."""
    fixes: list[str] = []
    paths = {r.path for r in site.routes}
    names = {c.name for c in site.collections}
    home = site.routes[0].path if site.routes else "/"

    def fix_href(m: "re.Match") -> str:
        path = _norm_path(m.group(2))
        if path in paths:
            return m.group(0)
        fixes.append(f"pointed a link to #{path} at #{home}")
        return f'href={m.group(1)}#{home}{m.group(1)}'

    inner = _ROUTE_HREF.sub(fix_href, inner)

    def fix_to(m: "re.Match") -> str:
        path = _norm_path(m.group(2))
        if path in paths:
            return m.group(0)
        fixes.append(f"pointed a button to {path} at {home}")
        return f"data-to={m.group(1)}{home}{m.group(1)}"

    inner = _DATA_TO.sub(fix_to, inner)

    target = section.collection or (site.collections[0].name if site.collections else "")
    for attr_name in ("data-list", "data-form", "data-filter", "data-sort", "data-count", "data-empty"):
        def fix_collection(m: "re.Match", _attr: str = attr_name) -> str:
            if m.group(2) in names or not target:
                return m.group(0)
            fixes.append(f"{_attr}={m.group(2)} → {target}")
            return f"{_attr}={m.group(1)}{target}{m.group(1)}"

        inner = re.sub(
            r"""(?<=\s)""" + re.escape(attr_name) + r"""\s*=\s*(["'])(.*?)\1""", fix_collection, inner
        )

    c = site.collection(section.collection) if section.collection else None
    if c is not None:
        valid = {f.name for f in c.fields}

        def fix_field(m: "re.Match") -> str:
            if m.group(2) in valid:
                return m.group(0)
            fixes.append(f"dropped unknown field {m.group(2)}")
            return ""

        inner = re.sub(r"""\s(?:data-field|data-filter-field)\s*=\s*(["'])(.*?)\1""", fix_field, inner)

    if _HARD_YEAR.search(inner):
        inner = _HARD_YEAR.sub(lambda m: f"{m.group(1)} <span data-year></span>", inner)
        fixes.append("replaced a hardcoded year")
    return inner, fixes


# ── turning a response into a section element ────────────────────────────────
_PY = re.compile(r"(?:^|\s)(?:sm:|md:|lg:|xl:)?(?:py|pt|pb)-\S+")
#: Attributes the runtime acts on. An element carrying one is never just a wrapper.
_BINDINGS = (
    "data-list", "data-form", "data-filter", "data-sort", "data-stat", "data-modal",
    "data-empty", "data-count", "data-to",
)


def unwrap(fragment: str, section: SectionPlan) -> tuple[str, str]:
    """(inner markup, wrapper classes) from whatever shape the model returned.

    Asked for one <section>; given, variously, that section, a <div> around the lot,
    or bare children. Whatever wraps the whole reply is unwrapped and its classes kept,
    minus vertical padding — the section rhythm belongs to the design system.
    """
    outer = H.outer_element(fragment)
    if outer is not None:
        tag, attrs, inner = outer
        bindings = {name: H.attr(attrs, name) for name in _BINDINGS if H.attr(attrs, name) is not None}
        classes = _PY.sub(" ", f" {H.attr(attrs, 'class') or ''}").strip()
        if H.attr(attrs, "data-section") is not None:
            # The element the model was asked for: the platform draws that wrapper
            # itself. A binding it carried — `data-form` on the section — moves onto
            # an element inside it, so neither the binding nor the section id is lost
            # or doubled.
            if bindings:
                inner = H.wrap(tag if tag != "section" else "div", H.with_attrs("", bindings), inner)
            return inner.strip(), classes
        # An outer element that *is* a binding — the `data-list` grid itself — is
        # content, not a wrapper; unwrapping it took the binding with it.
        if not bindings and tag in ("section", "header", "footer", "div", "nav", "main", "article") and (
            tag != "nav" or section.kind != "nav"
        ):
            return inner.strip(), classes
    return fragment.strip(), ""


def wrap(section: SectionPlan, inner: str, classes: str = "") -> str:
    tag = WRAPPER_TAG.get(section.kind, "section")
    platform = WRAPPER_CLASS.get(section.kind, "")
    merged = " ".join(dict.fromkeys(f"{platform} {classes}".split()))
    attrs = H.with_attrs(
        "",
        {
            "data-section": section.id,
            "data-label": section.label,
            "data-kind": section.kind,
            "data-collection": section.collection or None,
            "class": merged or None,
        },
    )
    if section.kind == "nav":
        attrs += ' role="banner"'
    return H.wrap(tag, attrs, inner)


# ── the prompt ───────────────────────────────────────────────────────────────
def _context(section: SectionPlan, site: SitePlan, brief: SiteBrief, rows: dict) -> str:
    """What this section needs to know beyond its contract, most relevant first."""
    parts = [brief.product_block()]
    if section.kind in ("features", "pricing", "faq", "testimonials", "hero", "cta", "content"):
        parts.append(brief.features_block())
    if section.kind in ("content", "testimonials", "faq"):
        parts.append(brief.stories_block())
    c = site.collection(section.collection) if section.collection else None
    if c is not None:
        sample = (rows.get(c.name) or [])[:2]
        parts.append(
            f"Collection {c.name} ({c.label}) — fields: {_fields_line(c)}\n"
            f"Sample records: {json.dumps(sample, ensure_ascii=False)}"
        )
    return "\n\n".join(p for p in parts if p)


def fixed_block(section: SectionPlan, site: SitePlan, ds: DesignSystem) -> str:
    """The part of the prompt that is never cut: the design system, map and contract."""
    route = next((r for r in site.routes if r.path == section.route), None)
    site_map = "\n".join(f"- #{r.path} — {r.title}: {r.purpose}" for r in site.routes)
    page = f"{route.title} (#{route.path}) — {route.purpose}" if route else "Every page (shared)"
    tag = WRAPPER_TAG.get(section.kind, "section")
    return (
        f"{ds.vocabulary()}\n\n"
        f"# Site map\n{site_map}\n\n"
        f"# This page\n{page}\n\n"
        f"# Your section\nid: {section.id} · kind: {section.kind} · title: {section.label}\n"
        f"Brief: {section.brief}\n\n"
        f"# Contract\n{contract(section, site)}\n\n"
        f"# Output\nReturn one element: <{tag} data-section=\"{section.id}\"> … </{tag}>. Make it "
        "polished, specific to the product and responsive (mobile first, with md: and lg: "
        "breakpoints)."
    )


def messages(
    section: SectionPlan,
    site: SitePlan,
    brief: SiteBrief,
    ds: DesignSystem,
    rows: dict,
    char_budget: int,
) -> list[ChatMessage]:
    fixed = fixed_block(section, site, ds)
    room = max(char_budget - len(SYSTEM) - len(fixed) - 40, 0)
    context = _context(section, site, brief, rows)
    if len(context) > room:
        context = context[:room]
    user = (f"# Product\n{context}\n\n" if context else "") + fixed
    return [ChatMessage(role="system", content=SYSTEM), ChatMessage(role="user", content=user)]


def repair_messages(
    base: list[ChatMessage], attempt: str, problems: list[str], char_budget: int
) -> list[ChatMessage]:
    """The same ask, the previous answer, and what is wrong with it — inside the window."""
    listed = "\n".join(f"- {p}" for p in problems[:8])
    instruction = (
        "That section has these problems:\n"
        f"{listed}\n\n"
        "Return the corrected section in full — the whole element, not a patch."
    )
    used = sum(len(m.content) for m in base) + len(instruction)
    echo = attempt[: max(char_budget - used, 0)]
    out = list(base)
    if echo:
        out.append(ChatMessage(role="assistant", content=echo))
    out.append(ChatMessage(role="user", content=instruction))
    return out


def fallback(section: SectionPlan, site: SitePlan, brief: SiteBrief) -> str:
    return wrap(section, templates.render(section, site, brief))


def is_bound(section: SectionPlan) -> bool:
    return section.kind in BOUND_KINDS
