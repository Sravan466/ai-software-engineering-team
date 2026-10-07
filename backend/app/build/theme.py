"""Site style for the generated app (#78): fonts, colours and shape, in the code.

On the sketch, Site style rewrote the mockup's `<head>`. On the running app it has to
change the code that ships — and the one file the platform owns that every page reads
is `tailwind.config.js`. So the style chosen on the Preview tab is kept with the
Frontend attempt (`output["app_theme"]`), and the scaffold writes it into that config:

  * **colours** — generated code says `bg-indigo-600`, not "primary". The palette family
    the code uses most is its primary, the next its accent, and its commonest grey its
    neutral; each is re-defined with the chosen colour's ramp (the one the sketch's
    design system derives, with the brand colour at 600), so every class that names
    that family takes the new colour, on every page;
  * **fonts** — `fontFamily.sans` is the body font, headings get the display font, and
    the global stylesheet imports both from Google Fonts;
  * **corners and shadows** — Tailwind's radius and shadow scales, re-defined.

Density has no single place in generated code, so it isn't offered for the app.
"""
from __future__ import annotations

import json
import re
from collections import Counter
from typing import Optional

#: Tailwind v3's own 600s — the colour a family shows before anyone restyles it.
TAILWIND_600 = {
    "slate": "#475569", "gray": "#4b5563", "zinc": "#52525b", "neutral": "#525252", "stone": "#57534e",
    "red": "#dc2626", "orange": "#ea580c", "amber": "#d97706", "yellow": "#ca8a04", "lime": "#65a30d",
    "green": "#16a34a", "emerald": "#059669", "teal": "#0d9488", "cyan": "#0891b2", "sky": "#0284c7",
    "blue": "#2563eb", "indigo": "#4f46e5", "violet": "#7c3aed", "purple": "#9333ea", "fuchsia": "#c026d3",
    "pink": "#db2777", "rose": "#e11d48",
}
NEUTRALS = ("slate", "gray", "zinc", "neutral", "stone")
CHROMATIC = tuple(k for k in TAILWIND_600 if k not in NEUTRALS)
#: What the app preview's Site style offers. Density isn't one: see the module.
KEYS = ("primary", "accent", "tint", "font_pair", "radius", "shadow")

_UTIL = (
    r"(?:bg|text|border(?:-[trblxy])?|ring|ring-offset|from|via|to|fill|stroke|outline|decoration|accent|caret|"
    r"divide|placeholder|shadow)"
)
_CLASS = re.compile(rf"(?<![\w-]){_UTIL}-({'|'.join(TAILWIND_600)})-(?:50|[1-9]00|950)\b")


def families(files: dict[str, str]) -> dict[str, Optional[str]]:
    """The palette families the frontend's classes use most: primary, accent, neutral."""
    counts: Counter = Counter()
    for path, content in files.items():
        if path.endswith((".tsx", ".jsx", ".js", ".ts", ".html", ".vue", ".svelte", ".css")):
            counts.update(m.group(1) for m in _CLASS.finditer(content))
    chroma = [f for f, _ in counts.most_common() if f in CHROMATIC]
    greys = [f for f, _ in counts.most_common() if f in NEUTRALS]
    return {
        "primary": chroma[0] if chroma else None,
        "accent": chroma[1] if len(chroma) > 1 else None,
        "neutral": greys[0] if greys else "gray",
    }


def current(theme: Optional[dict], files: dict[str, str]) -> dict:
    """The style the app has now: what was chosen, else what its classes already say."""
    theme = theme if isinstance(theme, dict) else {}
    fam = theme["families"] if isinstance(theme.get("families"), dict) else families(files)
    return {
        "primary": theme.get("primary") or TAILWIND_600.get(fam.get("primary") or "indigo", "#4f46e5"),
        "accent": theme.get("accent") or TAILWIND_600.get(fam.get("accent") or "amber", "#d97706"),
        "tint": theme.get("tint") or "neutral",
        "font_pair": theme.get("font_pair") or "",
        "radius": theme.get("radius") or "",
        "shadow": theme.get("shadow") or "",
        "density": "",
    }


class Refused(ValueError):
    """A style the app can't take, with the reason."""


def merge(theme: Optional[dict], changes: dict, files: dict[str, str]) -> dict:
    """The app's style with `changes` applied, checked. Remembers which families it
    re-defines the first time, so later changes keep landing on the same classes."""
    from app.preview import design as D

    out = dict(theme) if isinstance(theme, dict) else {}
    if "density" in changes and changes["density"]:
        raise Refused("Density has no single place in the app's code, so it can't be changed here. Ask the crew instead.")
    for key in ("primary", "accent"):
        value = changes.get(key)
        if value:
            colour = D._hex(value)
            if colour is None:
                raise Refused(f"'{value}' isn't a colour. Use #rrggbb.")
            if key == "primary":
                colour, _ = D._readable_on_white(colour)
            out[key] = colour
    for key, allowed in (("tint", D._TINTS), ("radius", D._RADIUS), ("shadow", D._SHADOW), ("font_pair", D.FONT_PAIRS)):
        value = changes.get(key)
        if value:
            if value not in allowed:
                raise Refused(f"'{value}' isn't a {key.replace('_', ' ')} the app can take.")
            out[key] = value
    if not isinstance(out.get("families"), dict):
        out["families"] = families(files)
    fam = out["families"]
    if out.get("primary") and not fam.get("primary"):
        raise Refused("The app's code uses no colour family for its brand colour, so there's nothing to recolour. Ask the crew instead.")
    if out.get("accent") and not fam.get("accent"):
        out.pop("accent", None)
        if changes.get("accent"):
            raise Refused("The app's code uses only one colour family, so there's no accent to change. Ask the crew instead.")
    return out


def _px(value: str) -> float:
    m = re.match(r"([\d.]+)px", value or "")
    return float(m.group(1)) if m else 0.0


def _shadow(value: str) -> str:
    return value.replace("var(--c-ink)", "15 23 42").replace("var(--c-line)", "226 232 240")


def extend(theme: Optional[dict]) -> tuple[dict, Optional[str]]:
    """(Tailwind `theme.extend`, the display font stack for headings) for a style."""
    from app.preview import design as D

    if not isinstance(theme, dict):
        return {}, None
    fam = theme.get("families") if isinstance(theme.get("families"), dict) else {}
    out: dict = {}
    colours: dict = {}
    if theme.get("primary") and fam.get("primary"):
        colours[fam["primary"]] = {str(k): v for k, v in D._scale(theme["primary"]).items()}
    if theme.get("accent") and fam.get("accent"):
        colours[fam["accent"]] = {str(k): v for k, v in D._scale(theme["accent"]).items()}
    if theme.get("tint") and fam.get("neutral"):
        base = theme.get("primary") or TAILWIND_600.get(fam.get("primary") or "", "#4f46e5")
        colours[fam["neutral"]] = {str(k): v for k, v in D._neutral_scale(base, theme["tint"]).items()}
    if colours:
        out["colors"] = colours
    display = None
    pair = D.FONT_PAIRS.get(theme.get("font_pair") or "")
    if pair:
        fallback = [f.strip() for f in pair["fallback"].split(",")]
        out["fontFamily"] = {
            "sans": [pair["body"], *fallback],
            "display": [pair["display"], *fallback],
        }
        display = json.dumps([pair["display"], *fallback])
    radius = D._RADIUS.get(theme.get("radius") or "")
    if radius:
        small, base, large, xl = radius
        out["borderRadius"] = {
            "sm": small, "DEFAULT": base, "md": base, "lg": large, "xl": xl,
            "2xl": f"{round(_px(xl) * 1.3)}px", "3xl": f"{round(_px(xl) * 1.6)}px",
        }
    shadow = D._SHADOW.get(theme.get("shadow") or "")
    if shadow:
        sm, md, lg = (_shadow(s) for s in shadow)
        out["boxShadow"] = {"sm": sm, "DEFAULT": md, "md": md, "lg": lg, "xl": lg, "2xl": lg}
    return out, display


def tailwind_block(theme: Optional[dict]) -> Optional[str]:
    """The `theme:` and `plugins:` lines of tailwind.config.js for a style, or None."""
    ext, display = extend(theme)
    if not ext:
        return None
    plugin = (
        "[function ({ addBase }) { addBase({ 'h1, h2, h3, h4': { fontFamily: " + display + " } }); }]"
        if display
        else "[]"
    )
    return (
        "  // The site style chosen on the Preview tab (#78).\n"
        f"  theme: {{ extend: {json.dumps(ext, indent=2).replace(chr(10), chr(10) + '  ')} }},\n"
        f"  plugins: {plugin},\n"
    )


def font_import(theme: Optional[dict]) -> Optional[str]:
    from app.preview import design as D

    pair = D.FONT_PAIRS.get((theme or {}).get("font_pair") or "") if isinstance(theme, dict) else None
    if not pair:
        return None
    return f"@import url('https://fonts.googleapis.com/css2?{pair['query']}&display=swap');\n"
