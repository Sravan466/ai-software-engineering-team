"""Stitch the passes into one document: head, pages, data, runtime.

The model never writes this part. Everything that makes the mockup a *site* rather than
a page — the Tailwind config carrying the design tokens, one `data-route` element per
page, the seeded records as JSON, the logic runtime — is assembled here from what the
earlier passes decided, around the sections the models wrote.

The plan and the design system travel inside the document as well, in the same JSON
block the runtime reads. An edit made weeks later is then checked against the
collections and pages this mockup actually has, not against whatever the project's
phases say now.
"""
from __future__ import annotations

import json
import re
from html import escape
from pathlib import Path
from typing import Optional
from urllib.parse import quote

from app.preview import html as H
from app.preview.design import DesignSystem, head_html
from app.preview.plan import SitePlan

#: Marks a document drawn by the multi-pass generator. Older mockups are single
#: static documents with no runtime, and editing treats them the old way.
RUNTIME_ID = "app-runtime"
DATA_ID = "app-data"

_RUNTIME_JS = (Path(__file__).with_name("runtime.js")).read_text(encoding="utf-8")


def runtime_source() -> str:
    return _RUNTIME_JS


def is_site(html: str) -> bool:
    """Whether this document carries the runtime — i.e. was built as a site."""
    return f'id="{RUNTIME_ID}"' in (html or "")


def site_data(html: str) -> Optional[dict]:
    """The JSON block a site document carries, or None for an older mockup."""
    m = re.search(
        r'<script[^>]*id="' + DATA_ID + r'"[^>]*>([\s\S]*?)</script>', html or "", re.IGNORECASE
    )
    if not m:
        return None
    try:
        data = json.loads(m.group(1))
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def _json_for_script(value: object) -> str:
    # `</` would close the <script> it sits in, and `<!--` opens a comment the HTML
    # parser honours inside script data. Escaping every `<` as `<` removes both
    # and is still valid JSON — `<\!--` is not, and a record containing it used to
    # leave the whole site with no data at all.
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).replace("<", "\\u003c")


# ── pictures ─────────────────────────────────────────────────────────────────
def placeholder(alt: str, width: int, height: int, ds: DesignSystem) -> str:
    """An inline SVG in the brand's colours, labelled with what the picture shows.

    Every <img> becomes one of these. A model's image URL is a guess — placehold.co,
    an Unsplash id it half-remembers, `/images/hero.png` — and a guess that 404s is a
    console error on load. Drawn inline, a picture cannot fail to arrive.
    """
    w = max(120, min(int(width or 800), 1600))
    h = max(90, min(int(height or 520), 1200))
    primary = ds.primary_scale
    label = escape((alt or "").strip()[:48])
    svg = (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}" viewBox="0 0 {w} {h}">'
        '<defs><linearGradient id="g" x1="0" y1="0" x2="1" y2="1">'
        f'<stop offset="0" stop-color="{primary[100]}"/><stop offset="1" stop-color="{primary[300]}"/>'
        "</linearGradient></defs>"
        f'<rect width="{w}" height="{h}" fill="url(#g)"/>'
        f'<circle cx="{w * 0.78:.0f}" cy="{h * 0.3:.0f}" r="{min(w, h) * 0.22:.0f}" fill="{primary[200]}" opacity=".7"/>'
        f'<rect x="{w * 0.1:.0f}" y="{h * 0.62:.0f}" width="{w * 0.5:.0f}" height="{h * 0.06:.0f}" rx="6" fill="{primary[600]}" opacity=".22"/>'
        f'<rect x="{w * 0.1:.0f}" y="{h * 0.74:.0f}" width="{w * 0.34:.0f}" height="{h * 0.05:.0f}" rx="6" fill="{primary[600]}" opacity=".14"/>'
        + (
            f'<text x="{w * 0.1:.0f}" y="{h * 0.5:.0f}" font-family="system-ui,sans-serif" '
            f'font-size="{max(12, min(w, h) // 16)}" font-weight="600" fill="{primary[800]}">{label}</text>'
            if label
            else ""
        )
        + "</svg>"
    )
    return "data:image/svg+xml;charset=utf-8," + quote(svg, safe="")


def draw_images(fragment: str, ds: DesignSystem) -> tuple[str, int]:
    """Replace every non-inline <img> source with a drawn placeholder."""

    def replace(src: str, tag: str) -> str:
        if src.startswith("data:"):
            return src
        alt = H.attr(tag, "alt") or ""
        width = _int(H.attr(tag, "width"), 800)
        height = _int(H.attr(tag, "height"), 520)
        return placeholder(alt, width, height, ds)

    return H.rewrite_images(fragment, replace)


def _int(value: Optional[str], default: int) -> int:
    try:
        return int(float(str(value).strip().rstrip("px")))
    except (TypeError, ValueError):
        return default


# ── the document ─────────────────────────────────────────────────────────────
def assemble(
    site: SitePlan,
    ds: DesignSystem,
    sections: dict[str, str],
    rows: dict[str, list[dict]],
) -> str:
    """The finished document, from the plan and one element per section."""
    data = {
        "product": site.product,
        "tagline": site.tagline,
        "currency": "USD",
        "routes": [{"path": r.path, "title": r.title} for r in site.routes],
        "collections": {
            c.name: {**c.as_dict(), "rows": rows.get(c.name, [])} for c in site.collections
        },
        "sections": [s.as_dict() for s in site.all_sections()],
        "design": ds.as_dict(),
        "version": 2,
    }

    pages = []
    for index, route in enumerate(site.routes):
        body = "\n".join(sections[s.id] for s in route.sections if s.id in sections)
        state = ' class="is-active"' if index == 0 else " hidden"
        pages.append(
            f'<div data-route="{escape(route.path)}" data-route-title="{escape(route.title)}"{state}>\n'
            f"{body}\n</div>"
        )

    return (
        "<!doctype html>\n"
        '<html lang="en">\n<head>\n'
        '<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        f"<title>{escape(site.product)}</title>\n"
        f'<meta name="description" content="{escape(site.tagline or site.product)}">\n'
        f"{head_html(ds)}\n"
        f'<script type="application/json" id="{DATA_ID}">{_json_for_script(data)}</script>\n'
        "</head>\n"
        '<body class="min-h-screen bg-bg font-sans text-ink antialiased">\n'
        '<a href="#main" class="sr-only focus:not-sr-only focus:fixed focus:left-4 focus:top-4 '
        'focus:z-50 focus:rounded-md focus:bg-surface focus:px-4 focus:py-2 focus:shadow">Skip to content</a>\n'
        f"{sections.get(site.nav.id, '')}\n"
        '<main id="main">\n' + "\n".join(pages) + "\n</main>\n"
        f"{sections.get(site.footer.id, '')}\n"
        f'<script id="{RUNTIME_ID}">\n{_RUNTIME_JS}\n</script>\n'
        "</body>\n</html>\n"
    )


def replace_data(html: str, data: dict) -> str:
    """Swap the document's JSON block — used when an edit changes what it records."""
    return re.sub(
        r'(<script[^>]*id="' + DATA_ID + r'"[^>]*>)[\s\S]*?(</script>)',
        lambda m: m.group(1) + _json_for_script(data) + m.group(2),
        html,
        count=1,
        flags=re.IGNORECASE,
    )


# ── site style ───────────────────────────────────────────────────────────────
#: Everything `head_html` writes, from the font preconnect to the base stylesheet.
_HEAD_BLOCK = re.compile(
    r'<link rel="preconnect" href="https://fonts\.googleapis\.com">[\s\S]*?<style id="ds-base">[\s\S]*?</style>'
)
#: The tokens a person may change from Site style. Voice and tagline are copy, not style.
THEME_KEYS = ("primary", "accent", "tint", "font_pair", "radius", "shadow", "density")


class ThemeRefused(ValueError):
    pass


def apply_theme(html: str, changes: dict) -> str:
    """The document restyled: new tokens in `<head>`, and recorded in its data block.

    The design system already binds every section through tokens and the remapped
    palettes, so swapping the head is enough to change the font or brand colour on
    every page — no section is rewritten and no model is asked. A primary that white
    text cannot sit on is darkened, exactly as it is when a build chooses one.
    """
    from app.preview import design as D

    data = site_data(html)
    if data is None or not _HEAD_BLOCK.search(html or ""):
        raise ThemeRefused(
            "This mockup was drawn before site styles existed. Rebuild it to change fonts and colours here."
        )
    current = D.from_dict(data.get("design"), str(data.get("product") or "")).as_dict()
    merged = {**current, **{k: v for k, v in changes.items() if k in THEME_KEYS and v is not None}}
    for key in ("primary", "accent"):
        if merged.get(key) and D._hex(merged[key]) is None:
            raise ThemeRefused(f"'{merged[key]}' isn't a colour. Use #rrggbb.")
    if merged.get("font_pair") not in D.FONT_PAIRS:
        raise ThemeRefused(f"'{merged.get('font_pair')}' isn't one of the font pairings.")
    for key, allowed in (("tint", D._TINTS), ("radius", D._RADIUS), ("shadow", D._SHADOW), ("density", D._DENSITY)):
        if merged.get(key) not in allowed:
            raise ThemeRefused(f"'{merged.get(key)}' isn't a {key} the mockup offers.")
    ds = D.from_dict(merged, str(data.get("product") or ""))
    primary, _ = D._readable_on_white(ds.primary)
    ds = D.DesignSystem(**{**ds.as_dict(), "primary": primary, "notes": tuple(ds.notes)})
    head = D.head_html(ds)
    out = _HEAD_BLOCK.sub(lambda _m: head, html, count=1)
    return replace_data(out, {**data, "design": ds.as_dict()})
