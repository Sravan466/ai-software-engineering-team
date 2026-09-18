"""The platform's own version of every section kind — the floor under a model's.

A section the model could not write with its bindings intact, after a repair round
with the problems named, is drawn from here instead. These are written against the
design-system tokens and the runtime contract, so a fallback section is on-brand and
fully wired: the list still filters, the form still stores. What a fallback costs is
copy specificity, which is why the model gets first go at every section.

Every template returns the section's *inner* markup; the wrapper element, its
`data-*` identity and its vertical rhythm belong to the document assembler.
"""
from __future__ import annotations

from html import escape
from typing import Callable, Optional

from app.preview.brief import SiteBrief
from app.preview.plan import CollectionPlan, FieldPlan, SectionPlan, SitePlan

_ICONS = (
    '<path d="M4 12l5 5L20 6"/>',
    '<path d="M12 3v18M3 12h18"/>',
    '<path d="M4 6h16M4 12h10M4 18h7"/>',
    '<circle cx="12" cy="12" r="8"/><path d="M12 8v4l3 2"/>',
    '<path d="M5 19V9m7 10V5m7 14v-7"/>',
    '<path d="M12 21s-7-4.5-7-11a7 7 0 0114 0c0 6.5-7 11-7 11z"/><circle cx="12" cy="10" r="2.5"/>',
)


def icon(index: int) -> str:
    return (
        '<svg aria-hidden="true" viewBox="0 0 24 24" width="22" height="22" fill="none" '
        'stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">'
        f"{_ICONS[index % len(_ICONS)]}</svg>"
    )


def _e(text: object) -> str:
    return escape(str(text or ""), quote=True)


def _heading(title: str, sub: str = "", center: bool = False) -> str:
    align = " text-center mx-auto" if center else ""
    sub_html = f'<p class="mt-3 text-lg text-muted max-w-2xl{align}">{_e(sub)}</p>' if sub else ""
    return (
        f'<div class="max-w-2xl{align}">'
        f'<h2 class="font-display text-3xl font-bold text-ink md:text-4xl">{_e(title)}</h2>'
        f"{sub_html}</div>"
    )


def _route_for(site: SitePlan, kinds: tuple[str, ...], collection: str = "") -> str:
    for route in site.routes:
        for section in route.sections:
            if section.kind in kinds and (not collection or section.collection == collection):
                return route.path
    return site.routes[-1].path if site.routes else "/"


def _noun(collection: CollectionPlan) -> str:
    label = collection.label
    if label.lower().endswith("ies"):
        return label[:-3] + "y"
    return label[:-1] if label.lower().endswith("s") else label


def _first(collection: CollectionPlan, *types: str) -> Optional[FieldPlan]:
    return next((f for f in collection.fields if f.type in types), None)


# ── shared chrome ────────────────────────────────────────────────────────────
def nav(section: SectionPlan, site: SitePlan, brief: SiteBrief) -> str:
    links = "".join(
        f'<a href="#{_e(r.path)}" class="whitespace-nowrap rounded-md px-3 py-2 text-sm font-medium '
        f'text-muted transition hover:bg-primary/5 hover:text-ink aria-[current=page]:text-primary">'
        f"{_e(r.title)}</a>"
        for r in site.routes
    )
    main = site.collections[0]
    form_route = _route_for(site, ("form",), main.name)
    initial = _e((site.product or "A")[:1].upper())
    return (
        '<div class="container-page flex items-center justify-between gap-4 py-3">'
        f'<a href="#/" class="flex items-center gap-2.5 font-display text-lg font-bold text-ink">'
        f'<span class="grid h-9 w-9 place-items-center rounded-lg bg-primary text-sm text-on-primary">{initial}</span>'
        f"{_e(site.product)}</a>"
        f'<nav aria-label="Primary" class="hidden items-center gap-1 md:flex">{links}</nav>'
        f'<a href="#{_e(form_route)}" class="btn btn-primary">Add {_e(_noun(main).lower())}</a>'
        "</div>"
        f'<nav aria-label="Primary (mobile)" class="container-page flex gap-1 overflow-x-auto pb-2 md:hidden">{links}</nav>'
    )


def footer(section: SectionPlan, site: SitePlan, brief: SiteBrief) -> str:
    links = "".join(
        f'<li><a href="#{_e(r.path)}" class="text-sm text-muted hover:text-ink">{_e(r.title)}</a></li>'
        for r in site.routes
    )
    return (
        '<div class="container-page grid gap-10 py-12 md:grid-cols-3">'
        f'<div><p class="font-display text-lg font-bold text-ink">{_e(site.product)}</p>'
        f'<p class="mt-2 max-w-xs text-sm text-muted">{_e(site.tagline or brief.idea)}</p></div>'
        f'<div><p class="text-xs font-semibold uppercase tracking-wider text-muted">Pages</p>'
        f'<ul class="mt-3 space-y-2">{links}</ul></div>'
        '<div class="md:text-right"><p class="text-sm text-muted">'
        f'&copy; <span data-year></span> {_e(site.product)}. All rights reserved.</p></div>'
        "</div>"
    )


# ── page sections ────────────────────────────────────────────────────────────
def hero(section: SectionPlan, site: SitePlan, brief: SiteBrief) -> str:
    main = site.collections[0]
    list_route = _route_for(site, ("list", "table"), main.name)
    form_route = _route_for(site, ("form",), main.name)
    title = main.title_field
    headline = site.tagline or brief.product
    sub = brief.problem or brief.idea
    return (
        '<div class="container-page grid items-center gap-12 lg:grid-cols-2">'
        "<div>"
        f'<span class="badge">{_e(site.product)}</span>'
        f'<h1 class="mt-5 font-display text-4xl font-bold tracking-tight text-ink md:text-6xl">{_e(headline)}</h1>'
        f'<p class="mt-5 max-w-xl text-lg text-muted">{_e(sub)}</p>'
        '<div class="mt-8 flex flex-wrap gap-3">'
        f'<a href="#{_e(list_route)}" class="btn btn-primary">Browse {_e(main.label.lower())}</a>'
        f'<a href="#{_e(form_route)}" class="btn btn-secondary">Add {_e(_noun(main).lower())}</a>'
        "</div></div>"
        '<div class="card p-0 overflow-hidden">'
        '<div class="flex items-center justify-between border-b border-line px-5 py-3">'
        f'<p class="text-sm font-semibold text-ink">Latest {_e(main.label.lower())}</p>'
        f'<span class="badge"><span data-stat="{_e(main.name)}.count"></span>&nbsp;total</span></div>'
        f'<ul data-list="{_e(main.name)}" data-limit="4" class="divide-y divide-line">'
        '<template><li class="flex items-center justify-between gap-3 px-5 py-3">'
        f'<span class="font-medium text-ink" data-field="{_e(title.name)}"></span>'
        + _secondary_badge(main)
        + "</li></template></ul></div></div>"
    )


def _secondary_badge(collection: CollectionPlan) -> str:
    field = _first(collection, "select", "date", "currency", "number")
    if field is None:
        return ""
    return f'<span class="badge" data-field="{_e(field.name)}"></span>'


def features(section: SectionPlan, site: SitePlan, brief: SiteBrief) -> str:
    items = brief.features[:6] or [
        {"name": c.label, "description": f"Keep every {_noun(c).lower()} in one place."}
        for c in site.collections
    ]
    cards = "".join(
        '<div class="card">'
        f'<span class="grid h-11 w-11 place-items-center rounded-lg bg-primary/10 text-primary">{icon(i)}</span>'
        f'<h3 class="mt-4 font-display text-lg font-semibold text-ink">{_e(f.get("name"))}</h3>'
        f'<p class="mt-2 text-sm text-muted">{_e(f.get("description") or section.brief)}</p>'
        "</div>"
        for i, f in enumerate(items)
    )
    return (
        '<div class="container-page">'
        + _heading(section.label, section.brief)
        + f'<div class="mt-10 grid gap-5 sm:grid-cols-2 lg:grid-cols-3">{cards}</div></div>'
    )


def stats(section: SectionPlan, site: SitePlan, brief: SiteBrief) -> str:
    c = site.collection(section.collection) or site.collections[0]
    figures = [(f"{c.name}.count", "", f"Total {c.label.lower()}", "Every record, updated as you add more.")]
    money = _first(c, "currency")
    number = _first(c, "number", "percent")
    select = _first(c, "select")
    if money:
        figures.append((f"{c.name}.sum.{money.name}", "", f"Total {money.label.lower()}", "Summed across every record."))
    if number:
        figures.append((f"{c.name}.avg.{number.name}", "", f"Average {number.label.lower()}", "Mean across every record."))
    if select and select.options:
        figures.append((f"{c.name}.count", f"{select.name}={select.options[0]}",
                        f"{select.options[0]}", f"{c.label} with {select.label.lower()} {select.options[0]}."))
    cards = "".join(
        '<div class="card">'
        f'<p class="text-sm font-medium text-muted">{_e(label)}</p>'
        f'<p class="mt-2 font-display text-4xl font-bold text-ink" data-stat="{_e(stat)}"'
        + (f' data-stat-where="{_e(where)}"' if where else "")
        + f"></p><p class=\"mt-1 text-sm text-muted\">{_e(caption)}</p></div>"
        for stat, where, label, caption in figures[:4]
    )
    return (
        '<div class="container-page">'
        + _heading(section.label, section.brief)
        + f'<div class="mt-10 grid gap-5 sm:grid-cols-2 lg:grid-cols-4">{cards}</div></div>'
    )


def _toolbar(c: CollectionPlan, with_sort_select: bool = True) -> str:
    select = _first(c, "select")
    filter_html = ""
    if select:
        options = "".join(f'<option value="{_e(o)}">{_e(o)}</option>' for o in select.options)
        filter_html = (
            f'<label class="sr-only" for="f-{_e(c.name)}-{_e(select.name)}">{_e(select.label)}</label>'
            f'<select id="f-{_e(c.name)}-{_e(select.name)}" class="select sm:w-48" data-filter="{_e(c.name)}" '
            f'data-filter-field="{_e(select.name)}"><option value="">All {_e(select.label.lower())}</option>{options}</select>'
        )
    sort_html = ""
    if with_sort_select:
        options = []
        for f in c.fields:
            if f.type in ("date", "datetime"):
                options.append((f"{f.name}:desc", f"Newest {f.label.lower()}"))
                options.append((f"{f.name}:asc", f"Oldest {f.label.lower()}"))
            elif f.type in ("currency", "number", "percent"):
                options.append((f"{f.name}:desc", f"Highest {f.label.lower()}"))
                options.append((f"{f.name}:asc", f"Lowest {f.label.lower()}"))
        title = c.title_field
        options.append((f"{title.name}:asc", f"{title.label} A–Z"))
        sort_html = (
            f'<label class="sr-only" for="s-{_e(c.name)}">Sort</label>'
            f'<select id="s-{_e(c.name)}" class="select sm:w-52" data-sort="{_e(c.name)}">'
            '<option value="">Sort by…</option>'
            + "".join(f'<option value="{_e(v)}">{_e(t)}</option>' for v, t in options[:6])
            + "</select>"
        )
    return (
        '<div class="mt-8 flex flex-col gap-3 sm:flex-row sm:items-center">'
        f'<label class="sr-only" for="q-{_e(c.name)}">Search {_e(c.label.lower())}</label>'
        f'<input id="q-{_e(c.name)}" type="search" class="input sm:max-w-xs" data-filter="{_e(c.name)}" '
        f'placeholder="Search {_e(c.label.lower())}…">'
        f"{filter_html}{sort_html}"
        f'<p class="text-sm text-muted sm:ml-auto"><span data-count="{_e(c.name)}"></span> {_e(c.label.lower())}</p>'
        "</div>"
    )


def list_(section: SectionPlan, site: SitePlan, brief: SiteBrief) -> str:
    c = site.collection(section.collection) or site.collections[0]
    title = c.title_field
    select = _first(c, "select")
    meta = [f for f in c.fields if f is not title and f is not select and f.type != "longtext"][:2]
    body = _first(c, "longtext")
    toggle = _first(c, "boolean")
    card = (
        '<article class="card flex flex-col gap-3">'
        '<div class="flex items-start justify-between gap-3">'
        f'<h3 class="font-display text-lg font-semibold text-ink" data-field="{_e(title.name)}"></h3>'
        + (f'<span class="badge shrink-0" data-field="{_e(select.name)}"></span>' if select else "")
        + "</div>"
        + (f'<p class="text-sm text-muted" data-field="{_e(body.name)}"></p>' if body else "")
        + '<dl class="mt-auto grid grid-cols-2 gap-2 border-t border-line pt-3 text-sm">'
        + "".join(
            f'<div><dt class="text-xs text-muted">{_e(f.label)}</dt>'
            f'<dd class="font-medium text-ink" data-field="{_e(f.name)}"></dd></div>'
            for f in meta
        )
        + "</dl>"
        + '<div class="flex items-center justify-between">'
        + (
            f'<label class="flex items-center gap-2 text-sm text-muted"><input type="checkbox" '
            f'data-toggle="{_e(toggle.name)}"> {_e(toggle.label)}</label>'
            if toggle
            else "<span></span>"
        )
        + '<button type="button" class="btn btn-ghost" data-action="remove">Remove</button></div>'
        + "</article>"
    )
    return (
        '<div class="container-page">'
        + _heading(section.label, section.brief)
        + _toolbar(c)
        + f'<div data-list="{_e(c.name)}" class="mt-6 grid gap-5 sm:grid-cols-2 lg:grid-cols-3">'
        f"<template>{card}</template></div>"
        f'<p data-empty="{_e(c.name)}" hidden class="mt-6 rounded-lg border border-dashed border-line p-8 '
        f'text-center text-muted">No {_e(c.label.lower())} match — try a different search or filter.</p>'
        "</div>"
    )


def table(section: SectionPlan, site: SitePlan, brief: SiteBrief) -> str:
    c = site.collection(section.collection) or site.collections[0]
    columns = [f for f in c.fields if f.type != "longtext"][:5]
    head = "".join(
        f'<th scope="col"><button type="button" class="font-semibold uppercase" data-sort="{_e(c.name)}" '
        f'data-sort-field="{_e(f.name)}">{_e(f.label)}</button></th>'
        for f in columns
    )
    cells = "".join(
        f'<td><span class="badge" data-field="{_e(f.name)}"></span></td>'
        if f.type == "select"
        else f'<td class="{"font-medium text-ink" if i == 0 else "text-muted"}" data-field="{_e(f.name)}"></td>'
        for i, f in enumerate(columns)
    )
    return (
        '<div class="container-page">'
        + _heading(section.label, section.brief)
        + _toolbar(c, with_sort_select=False)
        + '<div class="card mt-6 overflow-x-auto p-0">'
        f'<table class="table"><thead><tr>{head}<th scope="col"><span class="sr-only">Actions</span></th></tr></thead>'
        f'<tbody data-list="{_e(c.name)}"><template><tr>{cells}'
        '<td class="text-right"><button type="button" class="btn btn-ghost" data-action="remove">Remove</button></td>'
        "</tr></template></tbody></table></div>"
        f'<p data-empty="{_e(c.name)}" hidden class="mt-6 text-center text-muted">No {_e(c.label.lower())} match.</p>'
        "</div>"
    )


def _control(c: CollectionPlan, f: FieldPlan) -> str:
    fid = f"in-{c.name}-{f.name}"
    required = " required" if f.required else ""
    star = ' <span class="text-primary" aria-hidden="true">*</span>' if f.required else ""
    label = f'<label class="label" for="{_e(fid)}">{_e(f.label)}{star}</label>'
    error = f'<p class="form-error" data-error-for="{_e(f.name)}" hidden></p>'
    if f.type == "boolean":
        return (
            f'<div class="flex items-center gap-2 sm:col-span-2"><input id="{_e(fid)}" type="checkbox" '
            f'name="{_e(f.name)}" class="h-4 w-4 accent-[rgb(var(--c-primary))]">'
            f'<label for="{_e(fid)}" class="text-sm text-ink">{_e(f.label)}</label></div>'
        )
    if f.type == "longtext":
        return (
            f'<div class="sm:col-span-2">{label}<textarea id="{_e(fid)}" name="{_e(f.name)}" class="textarea" '
            f'rows="4"{required}></textarea>{error}</div>'
        )
    if f.type == "select":
        options = "".join(f'<option value="{_e(o)}">{_e(o)}</option>' for o in f.options)
        return (
            f'<div>{label}<select id="{_e(fid)}" name="{_e(f.name)}" class="select"{required}>'
            f"{options}</select>{error}</div>"
        )
    kind = {
        "email": "email", "url": "url", "date": "date", "datetime": "date",
        "number": "number", "currency": "number", "percent": "number",
    }.get(f.type, "text")
    extra = ' step="any" min="0"' if kind == "number" else ""
    if f.type == "text":
        extra = ' minlength="2"'
    return (
        f'<div>{label}<input id="{_e(fid)}" type="{kind}" name="{_e(f.name)}" class="input"{extra}{required}>'
        f"{error}</div>"
    )


def form(section: SectionPlan, site: SitePlan, brief: SiteBrief) -> str:
    c = site.collection(section.collection) or site.collections[0]
    list_route = _route_for(site, ("list", "table"), c.name)
    redirect = f' data-redirect="{_e(list_route)}"' if list_route != section.route else ""
    controls = "".join(_control(c, f) for f in c.fields)
    return (
        '<div class="container-page grid gap-10 lg:grid-cols-5">'
        '<div class="lg:col-span-2">'
        + _heading(section.label, section.brief)
        + '<p class="mt-6 text-sm text-muted">Fields marked <span class="text-primary">*</span> are required. '
        "New records appear in every list and figure straight away.</p></div>"
        f'<form data-form="{_e(c.name)}" data-success="{_e(_noun(c))} added."{redirect} '
        'class="card grid gap-5 sm:grid-cols-2 lg:col-span-3">'
        f"{controls}"
        '<div class="flex gap-3 sm:col-span-2">'
        f'<button type="submit" class="btn btn-primary">Add {_e(_noun(c).lower())}</button>'
        '<button type="reset" class="btn btn-secondary">Clear</button></div>'
        "</form></div>"
    )


def testimonials(section: SectionPlan, site: SitePlan, brief: SiteBrief) -> str:
    people = (("Maya Chen", 0), ("Daniel Okafor", 1), ("Priya Raman", 2))
    roles = brief.target_users or ["Early customer", "Team lead", "Operations manager"]
    feats = brief.features or [{"name": site.product, "description": brief.idea}]
    cards = "".join(
        '<figure class="card flex flex-col gap-4">'
        f'<blockquote class="text-ink">&ldquo;{_e(_quote(feats[i % len(feats)], site.product))}&rdquo;</blockquote>'
        '<figcaption class="mt-auto flex items-center gap-3">'
        f'<span class="grid h-10 w-10 place-items-center rounded-full bg-primary/10 font-semibold text-primary">'
        f'{_e("".join(w[0] for w in name.split()[:2]))}</span>'
        f'<span><span class="block font-semibold text-ink">{_e(name)}</span>'
        f'<span class="block text-sm text-muted">{_e(str(roles[i % len(roles)])[:60])}</span></span>'
        "</figcaption></figure>"
        for name, i in people
    )
    return (
        '<div class="container-page">'
        + _heading(section.label, section.brief)
        + f'<div class="mt-10 grid gap-5 md:grid-cols-3">{cards}</div></div>'
    )


def _quote(feature: dict, product: str) -> str:
    name = str(feature.get("name") or product)
    return f"{name} is the part of {product} we use every day — it replaced a spreadsheet and two chat threads."


def pricing(section: SectionPlan, site: SitePlan, brief: SiteBrief) -> str:
    feats = [str(f.get("name")) for f in brief.features] or [c.label for c in site.collections]
    form_route = _route_for(site, ("form",))
    plans = (
        ("Starter", "0", "For trying it out", feats[:2]),
        ("Pro", "19", "For people who rely on it", feats[:4]),
        ("Team", "49", "For whole teams", feats[:5]),
    )
    cards = ""
    for i, (name, price, blurb, items) in enumerate(plans):
        featured = i == 1
        ring = " ring-2 ring-primary" if featured else ""
        badge = '<span class="badge">Most popular</span>' if featured else ""
        lis = "".join(
            f'<li class="flex gap-2"><span class="text-primary">{icon(0)}</span>{_e(item)}</li>' for item in items
        )
        cards += (
            f'<div class="card flex flex-col{ring}"><div class="flex items-center justify-between">'
            f'<h3 class="font-display text-xl font-semibold text-ink">{_e(name)}</h3>{badge}</div>'
            f'<p class="mt-1 text-sm text-muted">{_e(blurb)}</p>'
            f'<p class="mt-5 font-display text-4xl font-bold text-ink">${_e(price)}'
            '<span class="text-base font-medium text-muted">/mo</span></p>'
            f'<ul class="mt-6 space-y-3 text-sm text-ink">{lis}</ul>'
            f'<a href="#{_e(form_route)}" class="btn {"btn-primary" if featured else "btn-secondary"} mt-8">'
            f"Choose {_e(name)}</a></div>"
        )
    return (
        '<div class="container-page">'
        + _heading(section.label, section.brief, center=True)
        + f'<div class="mt-12 grid gap-6 md:grid-cols-3">{cards}</div></div>'
    )


def faq(section: SectionPlan, site: SitePlan, brief: SiteBrief) -> str:
    pairs = [
        (f"What is {site.product}?", brief.idea),
        (f"Who is {site.product} for?", "; ".join(brief.target_users[:3]) or "Anyone with the problem it solves."),
    ]
    for f in brief.features[:3]:
        pairs.append((f"How does {str(f.get('name')).lower()} work?", f.get("description") or ""))
    items = "".join(
        '<details class="group border-b border-line py-5">'
        '<summary class="flex items-center justify-between gap-4 font-semibold text-ink">'
        f'{_e(q)}<span class="text-primary transition group-open:rotate-45">+</span></summary>'
        f'<p class="mt-3 text-muted">{_e(a)}</p></details>'
        for q, a in pairs
        if a
    )
    return (
        '<div class="container-page grid gap-10 lg:grid-cols-3">'
        + _heading(section.label, section.brief)
        + f'<div class="lg:col-span-2">{items}</div></div>'
    )


def cta(section: SectionPlan, site: SitePlan, brief: SiteBrief) -> str:
    form_route = _route_for(site, ("form",))
    return (
        '<div class="container-page">'
        '<div class="rounded-xl bg-primary px-8 py-14 text-center text-on-primary shadow-lg md:px-16">'
        f'<h2 class="font-display text-3xl font-bold md:text-4xl">{_e(section.label)}</h2>'
        f'<p class="mx-auto mt-4 max-w-xl text-on-primary/80">{_e(section.brief)}</p>'
        f'<a href="#{_e(form_route)}" class="btn mt-8 bg-surface text-primary hover:bg-surface/90">Get started</a>'
        "</div></div>"
    )


def content(section: SectionPlan, site: SitePlan, brief: SiteBrief) -> str:
    steps = [
        (s.get("i_want") or "", s.get("so_that") or "") for s in brief.stories[:3]
    ] or [(str(f.get("name")), str(f.get("description") or "")) for f in brief.features[:3]]
    if not steps:
        steps = [("Describe it", brief.idea), ("Organise it", ""), ("Share it", "")]
    items = "".join(
        '<li class="card">'
        f'<span class="font-display text-sm font-bold text-primary">Step {i + 1}</span>'
        f'<h3 class="mt-2 font-display text-lg font-semibold text-ink">{_e(str(title)[:1].upper() + str(title)[1:])}</h3>'
        + (f'<p class="mt-2 text-sm text-muted">{_e(detail)}</p>' if detail else "")
        + "</li>"
        for i, (title, detail) in enumerate(steps)
    )
    return (
        '<div class="container-page">'
        + _heading(section.label, section.brief)
        + f'<ol class="mt-10 grid gap-5 md:grid-cols-3">{items}</ol></div>'
    )


TEMPLATES: dict[str, Callable[[SectionPlan, SitePlan, SiteBrief], str]] = {
    "nav": nav,
    "footer": footer,
    "hero": hero,
    "features": features,
    "stats": stats,
    "list": list_,
    "table": table,
    "form": form,
    "testimonials": testimonials,
    "pricing": pricing,
    "faq": faq,
    "cta": cta,
    "content": content,
}


def render(section: SectionPlan, site: SitePlan, brief: SiteBrief) -> str:
    return TEMPLATES.get(section.kind, content)(section, site, brief)
