"""Pass 1 — the design system: one small call, emitted as tokens every section shares.

The single-shot mockup always looked the same because nothing told it what the
product should look like: `bg-gray-100`, blue buttons, the model's defaults. This pass
makes one small decision up front — palette, type pairing, radius, shadow, density,
voice — and turns it into CSS custom properties plus a Tailwind config.

The config does something a prompt cannot: it *remaps* the palettes a model reaches
for by habit. `gray`/`slate`/`zinc` become a neutral scale tinted to the brand, and
`blue`/`indigo` become the brand's primary. So a section that ignores the vocabulary
and writes `bg-blue-600` still comes out on-brand — the design system binds whether
or not a 7B model remembers it exists.

Colour is chosen by the model and *checked* here: a primary that cannot carry white
text at WCAG AA is darkened until it can, so no model choice can ship an unreadable
button.
"""
from __future__ import annotations

import colorsys
import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Optional

from pydantic import BaseModel, Field

#: Type pairings the model chooses between, by id. Curated rather than free-form: a
#: model naming fonts invents ones Google does not serve, and the page falls back to
#: Times. Each pairing is a display face for headings and a text face for everything
#: else, both loaded from Google Fonts with the fallbacks written out.
FONT_PAIRS: dict[str, dict] = {
    "modern": {
        "label": "Crisp geometric sans — product and SaaS",
        "display": "Plus Jakarta Sans",
        "body": "Plus Jakarta Sans",
        "query": "family=Plus+Jakarta+Sans:wght@400;500;600;700;800",
        "fallback": "ui-sans-serif, system-ui, sans-serif",
    },
    "editorial": {
        "label": "Soft serif headlines over a clean sans — content, food, travel, culture",
        "display": "Fraunces",
        "body": "Source Sans 3",
        "query": "family=Fraunces:opsz,wght@9..144,500;9..144,700&family=Source+Sans+3:wght@400;600;700",
        "fallback": "ui-serif, Georgia, serif",
    },
    "technical": {
        "label": "Grotesk headlines, plex body — developer tools and data",
        "display": "Space Grotesk",
        "body": "IBM Plex Sans",
        "query": "family=Space+Grotesk:wght@500;600;700&family=IBM+Plex+Sans:wght@400;500;600",
        "fallback": "ui-sans-serif, system-ui, sans-serif",
    },
    "friendly": {
        "label": "Rounded and warm — consumer, community, education, health",
        "display": "Nunito",
        "body": "Nunito Sans",
        "query": "family=Nunito:wght@600;700;800&family=Nunito+Sans:wght@400;600;700",
        "fallback": "ui-rounded, ui-sans-serif, system-ui, sans-serif",
    },
    "classic": {
        "label": "High-contrast serif display — finance, legal, premium, real estate",
        "display": "Playfair Display",
        "body": "Lato",
        "query": "family=Playfair+Display:wght@600;700&family=Lato:wght@400;700",
        "fallback": "ui-serif, Georgia, serif",
    },
    "geometric": {
        "label": "Clean geometric pair — marketplaces, productivity, startups",
        "display": "Outfit",
        "body": "DM Sans",
        "query": "family=Outfit:wght@500;600;700&family=DM+Sans:wght@400;500;700",
        "fallback": "ui-sans-serif, system-ui, sans-serif",
    },
    "humanist": {
        "label": "Open humanist sans — collaboration, HR, internal tools",
        "display": "Manrope",
        "body": "Manrope",
        "query": "family=Manrope:wght@400;500;600;700;800",
        "fallback": "ui-sans-serif, system-ui, sans-serif",
    },
    "bold": {
        "label": "Condensed, loud headlines — sports, events, retail, gaming",
        "display": "Archivo",
        "body": "Archivo",
        "query": "family=Archivo:wdth,wght@62..125,500;62..125,700;62..125,800",
        "fallback": "ui-sans-serif, system-ui, sans-serif",
    },
}

#: Brand palettes for when the model's call fails. Picked by a hash of the product
#: name, so the fallback is stable for a product and different between products —
#: the thing it replaces made every mockup the same blue.
_FALLBACK_PALETTES: tuple[tuple[str, str, str], ...] = (
    ("#4338ca", "#f59e0b", "cool"),
    ("#0f766e", "#f97316", "warm"),
    ("#be123c", "#0ea5e9", "neutral"),
    ("#7c3aed", "#10b981", "cool"),
    ("#b45309", "#2563eb", "warm"),
    ("#1d4ed8", "#e11d48", "cool"),
    ("#15803d", "#a855f7", "neutral"),
    ("#9d174d", "#14b8a6", "warm"),
)

_RADIUS = {
    "none": ("2px", "3px", "4px", "6px"),
    "sm": ("4px", "6px", "8px", "10px"),
    "md": ("6px", "8px", "12px", "16px"),
    "lg": ("8px", "12px", "16px", "22px"),
    "xl": ("10px", "16px", "22px", "28px"),
}
_SHADOW = {
    "none": ("none", "0 0 0 1px rgb(var(--c-line) / 1)", "0 0 0 1px rgb(var(--c-line) / 1)"),
    "soft": (
        "0 1px 2px rgb(var(--c-ink) / .05)",
        "0 4px 16px -6px rgb(var(--c-ink) / .12)",
        "0 18px 40px -16px rgb(var(--c-ink) / .22)",
    ),
    "crisp": (
        "0 1px 0 rgb(var(--c-ink) / .08)",
        "0 2px 0 rgb(var(--c-ink) / .10), 0 0 0 1px rgb(var(--c-line) / 1)",
        "0 6px 0 -2px rgb(var(--c-ink) / .12), 0 0 0 1px rgb(var(--c-line) / 1)",
    ),
    "lifted": (
        "0 2px 6px rgb(var(--c-ink) / .08)",
        "0 12px 28px -10px rgb(var(--c-ink) / .25)",
        "0 30px 60px -20px rgb(var(--c-ink) / .35)",
    ),
}
#: (root font size, vertical section padding) per density.
_DENSITY = {
    "compact": ("15px", "3.5rem"),
    "comfortable": ("16px", "5rem"),
    "spacious": ("17px", "6.5rem"),
}
_TINTS = ("cool", "warm", "neutral")

#: Tailwind's scale steps, and the lightness each step lands at relative to white.
_STEPS = (50, 100, 200, 300, 400, 500, 600, 700, 800, 900, 950)


class DesignChoice(BaseModel):
    """What the model is asked to decide. Everything else is derived from it."""

    tagline: str = Field(description="one line, under 12 words, in the brand's voice")
    voice: str = Field(description="three adjectives for how the copy should sound")
    primary: str = Field(description="brand colour as #rrggbb — used for primary actions")
    accent: str = Field(description="a contrasting highlight colour as #rrggbb")
    neutral_tint: str = Field(description="cool | warm | neutral — the tint of the greys")
    font_pair: str = Field(description="one font pairing id from the list")
    radius: str = Field(description="none | sm | md | lg | xl")
    shadow: str = Field(description="none | soft | crisp | lifted")
    density: str = Field(description="compact | comfortable | spacious")


@dataclass(frozen=True)
class DesignSystem:
    """The resolved tokens. Stable, contrast-checked, ready to print into a page."""

    primary: str
    accent: str
    tint: str
    font_pair: str
    radius: str
    shadow: str
    density: str
    voice: str
    tagline: str
    #: "model" or "fallback" — reported, because a fallback look is worth knowing about.
    source: str = "model"
    notes: tuple[str, ...] = field(default_factory=tuple)

    # ── derived palette ──────────────────────────────────────────────────────
    @property
    def primary_scale(self) -> dict[int, str]:
        return _scale(self.primary)

    @property
    def accent_scale(self) -> dict[int, str]:
        return _scale(self.accent)

    @property
    def neutral_scale(self) -> dict[int, str]:
        return _neutral_scale(self.primary, self.tint)

    @property
    def fonts(self) -> dict:
        return FONT_PAIRS[self.font_pair]

    def as_dict(self) -> dict:
        return {
            "primary": self.primary,
            "accent": self.accent,
            "tint": self.tint,
            "font_pair": self.font_pair,
            "radius": self.radius,
            "shadow": self.shadow,
            "density": self.density,
            "voice": self.voice,
            "tagline": self.tagline,
            "source": self.source,
            "notes": list(self.notes),
        }

    # ── what a section prompt is told ────────────────────────────────────────
    def vocabulary(self) -> str:
        """The class names a section is asked to style with — short, and complete."""
        return (
            "DESIGN SYSTEM — style with Tailwind utilities plus these tokens:\n"
            "- Colours: bg-bg (page), bg-surface (cards, panels), text-ink (headings, body), "
            "text-muted (secondary text), border-line (hairlines), bg-primary + text-on-primary "
            "(primary actions), text-primary (links, emphasis), bg-primary/10 (soft tints), "
            "bg-accent / text-accent (highlights, badges). Do not use raw palette colours like "
            "gray-500 or blue-600.\n"
            "- Type: font-display on headings, body text inherits the text face. Headline scale: "
            "text-4xl md:text-6xl for a page hero, text-3xl md:text-4xl for section headings.\n"
            "- Components (ready-made classes): btn btn-primary, btn btn-secondary, btn btn-ghost, "
            "card, input, select, textarea, label, badge, chip, table.\n"
            "- Layout: wrap content in <div class=\"container-page\"> (centred, padded). The "
            "section's own vertical padding is set by the platform — do not add py-* to the "
            "outer element.\n"
            f"- Voice: {self.voice}."
        )


# ── colour maths ─────────────────────────────────────────────────────────────
_HEX = re.compile(r"^#?([0-9a-fA-F]{6}|[0-9a-fA-F]{3})$")


def _hex(value: object) -> Optional[str]:
    """`#rgb`/`#rrggbb` (hash optional) as lowercase `#rrggbb`, or None."""
    match = _HEX.match(str(value or "").strip())
    if not match:
        return None
    digits = match.group(1)
    if len(digits) == 3:
        digits = "".join(c * 2 for c in digits)
    return "#" + digits.lower()


def _rgb(hex_value: str) -> tuple[int, int, int]:
    h = hex_value.lstrip("#")
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def _to_hex(r: float, g: float, b: float) -> str:
    clamp = lambda v: max(0, min(255, int(round(v))))  # noqa: E731
    return "#{:02x}{:02x}{:02x}".format(clamp(r), clamp(g), clamp(b))


def _hls(hex_value: str) -> tuple[float, float, float]:
    r, g, b = (c / 255 for c in _rgb(hex_value))
    return colorsys.rgb_to_hls(r, g, b)


def _from_hls(h: float, l: float, s: float) -> str:  # noqa: E741 - colour maths
    r, g, b = colorsys.hls_to_rgb(h, max(0.0, min(1.0, l)), max(0.0, min(1.0, s)))
    return _to_hex(r * 255, g * 255, b * 255)


def _luminance(hex_value: str) -> float:
    def channel(c: int) -> float:
        v = c / 255
        return v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4

    r, g, b = _rgb(hex_value)
    return 0.2126 * channel(r) + 0.7152 * channel(g) + 0.0722 * channel(b)


def contrast(a: str, b: str) -> float:
    """WCAG contrast ratio between two `#rrggbb` colours."""
    la, lb = _luminance(a), _luminance(b)
    hi, lo = max(la, lb), min(la, lb)
    return (hi + 0.05) / (lo + 0.05)


def _readable_on_white(hex_value: str, ratio: float = 4.5) -> tuple[str, bool]:
    """Darken until white text on it passes `ratio`. Returns (colour, was_changed)."""
    h, l, s = _hls(hex_value)  # noqa: E741
    colour = hex_value
    changed = False
    while contrast(colour, "#ffffff") < ratio and l > 0.08:
        l -= 0.03  # noqa: E741
        colour = _from_hls(h, l, s)
        changed = True
    return colour, changed


def _scale(base: str) -> dict[int, str]:
    """A Tailwind-style 50–950 ramp with `base` sitting at 600.

    600 is where models put buttons (`bg-blue-600`), so that is where the brand colour
    lands; everything lighter walks toward white and everything darker toward black,
    holding the hue.
    """
    h, l, s = _hls(base)  # noqa: E741
    lighter = {50: 0.97, 100: 0.93, 200: 0.86, 300: 0.77, 400: 0.66}
    out: dict[int, str] = {}
    for step in _STEPS:
        if step in lighter:
            target = lighter[step]
            out[step] = _from_hls(h, max(target, l), s * (0.55 + 0.45 * (1 - target)))
        elif step == 500:
            out[step] = _from_hls(h, l + (0.66 - l) * 0.45 if l < 0.66 else l, s)
        elif step == 600:
            out[step] = base
        else:
            factor = {700: 0.82, 800: 0.66, 900: 0.52, 950: 0.34}[step]
            out[step] = _from_hls(h, l * factor, min(1.0, s * 1.02))
    return out


def _neutral_scale(primary: str, tint: str) -> dict[int, str]:
    """Greys that belong to the brand: the primary's hue at a whisper of saturation."""
    h, _l, _s = _hls(primary)
    if tint == "warm":
        h, sat = 0.08, 0.10
    elif tint == "neutral":
        sat = 0.02
    else:
        sat = 0.07
    lightness = {
        50: 0.975, 100: 0.955, 200: 0.91, 300: 0.84, 400: 0.66, 500: 0.49,
        600: 0.38, 700: 0.30, 800: 0.19, 900: 0.12, 950: 0.07,
    }
    return {step: _from_hls(h, lightness[step], sat) for step in _STEPS}


def _triplet(hex_value: str) -> str:
    """`#4338ca` -> `67 56 202`, the form `rgb(var(--x) / <alpha>)` needs."""
    return "{} {} {}".format(*_rgb(hex_value))


# ── resolving a choice ───────────────────────────────────────────────────────
def _pick(value: object, allowed, default: str) -> str:
    text = str(value or "").strip().lower()
    return text if text in allowed else default


def _seed(text: str) -> int:
    return int(hashlib.sha256((text or "product").encode("utf-8")).hexdigest()[:8], 16)


def fallback(product: str, tagline: str = "") -> DesignSystem:
    """A stable, product-specific look for when the model's choice is unusable."""
    seed = _seed(product)
    primary, accent, tint = _FALLBACK_PALETTES[seed % len(_FALLBACK_PALETTES)]
    pairs = list(FONT_PAIRS)
    primary, _ = _readable_on_white(primary)
    return DesignSystem(
        primary=primary,
        accent=accent,
        tint=tint,
        font_pair=pairs[(seed // 7) % len(pairs)],
        radius=("md", "lg", "xl", "sm")[(seed // 11) % 4],
        shadow=("soft", "lifted", "crisp")[(seed // 13) % 3],
        density="comfortable",
        voice="clear, confident, friendly",
        tagline=tagline,
        source="fallback",
    )


def from_dict(data: object, product: str = "") -> DesignSystem:
    """Rebuild the tokens a site document recorded, for an edit made to it later."""
    if not isinstance(data, dict):
        return fallback(product)
    base = fallback(product)
    return DesignSystem(
        primary=_hex(data.get("primary")) or base.primary,
        accent=_hex(data.get("accent")) or base.accent,
        tint=_pick(data.get("tint"), _TINTS, base.tint),
        font_pair=data.get("font_pair") if data.get("font_pair") in FONT_PAIRS else base.font_pair,
        radius=_pick(data.get("radius"), _RADIUS, base.radius),
        shadow=_pick(data.get("shadow"), _SHADOW, base.shadow),
        density=_pick(data.get("density"), _DENSITY, base.density),
        voice=str(data.get("voice") or base.voice),
        tagline=str(data.get("tagline") or ""),
        source=str(data.get("source") or "model"),
    )


def resolve(choice: DesignChoice, product: str) -> DesignSystem:
    """Turn the model's choice into tokens, correcting what would render badly."""
    base = fallback(product)
    notes: list[str] = []

    primary = _hex(choice.primary)
    if primary is None:
        notes.append(f"primary '{choice.primary}' is not a colour; used {base.primary}")
        primary = base.primary
    primary, darkened = _readable_on_white(primary)
    if darkened:
        notes.append("primary darkened until white text on it passes WCAG AA")

    accent = _hex(choice.accent) or base.accent
    if accent == primary:
        accent = base.accent

    font_pair = str(choice.font_pair or "").strip().lower()
    if font_pair not in FONT_PAIRS:
        notes.append(f"font pairing '{choice.font_pair}' is not offered; used {base.font_pair}")
        font_pair = base.font_pair

    tagline = " ".join(str(choice.tagline or "").split())[:120]
    voice = " ".join(str(choice.voice or "").split())[:80] or base.voice

    return DesignSystem(
        primary=primary,
        accent=accent,
        tint=_pick(choice.neutral_tint, _TINTS, base.tint),
        font_pair=font_pair,
        radius=_pick(choice.radius, _RADIUS, base.radius),
        shadow=_pick(choice.shadow, _SHADOW, base.shadow),
        density=_pick(choice.density, _DENSITY, base.density),
        voice=voice,
        tagline=tagline,
        source="model",
        notes=tuple(notes),
    )


# ── the prompt ───────────────────────────────────────────────────────────────
SYSTEM = (
    "You are a senior brand and product designer. You choose the visual system for a web "
    "product: one brand colour, one accent, a type pairing, and the shape language. You "
    "reply with ONLY a JSON object matching the requested shape."
)


def instructions() -> str:
    pairs = "\n".join(f'- "{key}": {spec["label"]}' for key, spec in FONT_PAIRS.items())
    return (
        "Choose the design system for this product. Make it specific to the product and its "
        "users — not a generic SaaS blue.\n\n"
        f"Font pairing ids:\n{pairs}\n\n"
        "Rules:\n"
        "- primary: a confident brand colour as #rrggbb that white text can sit on.\n"
        "- accent: a colour that contrasts with primary, as #rrggbb.\n"
        "- neutral_tint: cool | warm | neutral.  radius: none | sm | md | lg | xl.\n"
        "- shadow: none | soft | crisp | lifted.  density: compact | comfortable | spacious.\n"
        "- tagline: under 12 words.  voice: three adjectives."
    )


# ── what goes into the page head ─────────────────────────────────────────────
def head_html(ds: DesignSystem) -> str:
    """Fonts, the Tailwind config and the token/base stylesheet, in that order."""
    fonts = ds.fonts
    radius = _RADIUS[ds.radius]
    shadow = _SHADOW[ds.shadow]
    root_size, section_y = _DENSITY[ds.density]
    neutral = ds.neutral_scale
    primary = ds.primary_scale
    accent = ds.accent_scale

    def css_font(name: str) -> str:
        return f"'{name}', {fonts['fallback']}"

    config = {
        "theme": {
            "extend": {
                "colors": {
                    "primary": {
                        **{str(k): v for k, v in primary.items()},
                        "DEFAULT": "rgb(var(--c-primary) / <alpha-value>)",
                    },
                    "accent": {
                        **{str(k): v for k, v in accent.items()},
                        "DEFAULT": "rgb(var(--c-accent) / <alpha-value>)",
                    },
                    "on-primary": "rgb(var(--c-on-primary) / <alpha-value>)",
                    "bg": "rgb(var(--c-bg) / <alpha-value>)",
                    "surface": "rgb(var(--c-surface) / <alpha-value>)",
                    "ink": "rgb(var(--c-ink) / <alpha-value>)",
                    "muted": "rgb(var(--c-muted) / <alpha-value>)",
                    "line": "rgb(var(--c-line) / <alpha-value>)",
                    # The palettes a model writes by habit, pointed at the brand.
                    **{name: {str(k): v for k, v in neutral.items()}
                       for name in ("gray", "slate", "zinc", "neutral", "stone")},
                    **{name: {str(k): v for k, v in primary.items()}
                       for name in ("blue", "indigo")},
                },
                "fontFamily": {
                    "display": [fonts["display"], *fonts["fallback"].split(", ")],
                    "sans": [fonts["body"], *fonts["fallback"].split(", ")],
                },
                "borderRadius": {
                    "sm": radius[0], "DEFAULT": radius[1], "md": radius[1],
                    "lg": radius[2], "xl": radius[3], "2xl": radius[3], "3xl": radius[3],
                },
                "boxShadow": {
                    "sm": shadow[0], "DEFAULT": shadow[1], "md": shadow[1],
                    "lg": shadow[2], "xl": shadow[2], "2xl": shadow[2],
                },
            }
        }
    }

    tokens = (
        ":root{"
        f"--c-primary:{_triplet(ds.primary)};"
        f"--c-primary-strong:{_triplet(primary[700])};"
        f"--c-accent:{_triplet(ds.accent)};"
        "--c-on-primary:255 255 255;"
        f"--c-bg:{_triplet(neutral[50])};"
        "--c-surface:255 255 255;"
        f"--c-ink:{_triplet(neutral[900])};"
        f"--c-muted:{_triplet(neutral[600])};"
        f"--c-line:{_triplet(neutral[200])};"
        f"--c-soft:{_triplet(primary[50])};"
        f"--r-sm:{radius[0]};--r:{radius[1]};--r-lg:{radius[2]};--r-xl:{radius[3]};"
        f"--sh-sm:{shadow[0]};--sh:{shadow[1]};--sh-lg:{shadow[2]};"
        f"--font-display:{css_font(fonts['display'])};"
        f"--font-body:{css_font(fonts['body'])};"
        f"--section-y:{section_y};"
        f"font-size:{root_size};"
        "}"
    )

    return (
        '<link rel="preconnect" href="https://fonts.googleapis.com">\n'
        '<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>\n'
        f'<link rel="stylesheet" href="https://fonts.googleapis.com/css2?{fonts["query"]}&display=swap">\n'
        '<script src="https://cdn.tailwindcss.com"></script>\n'
        f"<script>tailwind.config = {json.dumps(config, separators=(',', ':'))};</script>\n"
        f'<style id="ds-tokens">{tokens}</style>\n'
        f'<style id="ds-base">{BASE_CSS}</style>'
    )


#: Component classes and runtime styling. Written against the tokens only, so the
#: same sheet serves every product and every fallback template.
BASE_CSS = """
*,*::before,*::after{box-sizing:border-box}
html{-webkit-text-size-adjust:100%;scroll-padding-top:5rem}
body{margin:0;background:rgb(var(--c-bg));color:rgb(var(--c-ink));font-family:var(--font-body);line-height:1.6;-webkit-font-smoothing:antialiased;text-rendering:optimizeLegibility}
h1,h2,h3,h4,.font-display{font-family:var(--font-display);letter-spacing:-.015em;line-height:1.15;text-wrap:balance}
p{text-wrap:pretty}
img{max-width:100%;height:auto;display:block}
.container-page{width:100%;max-width:72rem;margin-inline:auto;padding-inline:1.25rem}
@media (min-width:640px){.container-page{padding-inline:2rem}}
main [data-section]{padding-block:var(--section-y)}
main [data-section][data-kind="hero"]{padding-block:calc(var(--section-y) * 1.25)}
[data-route]{display:none}
[data-route].is-active{display:block}
template{display:none}
.btn{display:inline-flex;align-items:center;justify-content:center;gap:.5rem;min-height:2.75rem;padding:.625rem 1.15rem;border-radius:var(--r);font-weight:600;font-size:.95rem;line-height:1.2;border:1px solid transparent;cursor:pointer;text-decoration:none;transition:background-color .15s ease,border-color .15s ease,color .15s ease,box-shadow .15s ease,transform .15s ease;white-space:nowrap}
.btn:focus-visible,.input:focus-visible,.select:focus-visible,.textarea:focus-visible,.chip:focus-visible,a:focus-visible{outline:2px solid rgb(var(--c-primary));outline-offset:2px}
.btn:active{transform:translateY(1px)}
.btn-primary{background:rgb(var(--c-primary));color:rgb(var(--c-on-primary));box-shadow:var(--sh-sm)}
.btn-primary:hover{background:rgb(var(--c-primary-strong))}
.btn-secondary{background:rgb(var(--c-surface));color:rgb(var(--c-ink));border-color:rgb(var(--c-line))}
.btn-secondary:hover{border-color:rgb(var(--c-primary) / .5);color:rgb(var(--c-primary))}
.btn-ghost{background:transparent;color:rgb(var(--c-primary))}
.btn-ghost:hover{background:rgb(var(--c-primary) / .08)}
.card{background:rgb(var(--c-surface));border:1px solid rgb(var(--c-line));border-radius:var(--r-lg);box-shadow:var(--sh-sm);padding:1.5rem}
.label{display:block;font-size:.85rem;font-weight:600;color:rgb(var(--c-ink));margin-bottom:.35rem}
.input,.select,.textarea{display:block;width:100%;min-height:2.75rem;padding:.6rem .8rem;border:1px solid rgb(var(--c-line));border-radius:var(--r);background:rgb(var(--c-surface));color:rgb(var(--c-ink));font:inherit;font-size:.95rem;transition:border-color .15s ease,box-shadow .15s ease}
.textarea{min-height:6rem;resize:vertical}
.input:focus,.select:focus,.textarea:focus{border-color:rgb(var(--c-primary));box-shadow:0 0 0 3px rgb(var(--c-primary) / .15);outline:none}
.input::placeholder,.textarea::placeholder{color:rgb(var(--c-muted) / .8)}
[aria-invalid="true"]{border-color:#dc2626 !important;box-shadow:0 0 0 3px rgb(220 38 38 / .12) !important}
.form-error{margin:.35rem 0 0;font-size:.82rem;color:#b91c1c}
.form-error[hidden]{display:none}
.badge{display:inline-flex;align-items:center;gap:.3rem;padding:.2rem .6rem;border-radius:999px;font-size:.75rem;font-weight:600;background:rgb(var(--c-primary) / .1);color:rgb(var(--c-primary-strong))}
.chip{display:inline-flex;align-items:center;min-height:2.25rem;padding:.35rem .85rem;border-radius:999px;border:1px solid rgb(var(--c-line));background:rgb(var(--c-surface));color:rgb(var(--c-ink));font-size:.85rem;font-weight:500;cursor:pointer}
.chip[aria-pressed="true"]{background:rgb(var(--c-primary));border-color:rgb(var(--c-primary));color:rgb(var(--c-on-primary))}
.table{width:100%;border-collapse:collapse;font-size:.92rem}
.table th{text-align:left;font-weight:600;color:rgb(var(--c-muted));font-size:.78rem;text-transform:uppercase;letter-spacing:.04em;padding:.75rem 1rem;border-bottom:1px solid rgb(var(--c-line))}
.table td{padding:.85rem 1rem;border-bottom:1px solid rgb(var(--c-line));vertical-align:middle}
.table tbody tr:hover{background:rgb(var(--c-primary) / .04)}
[data-sort-field]{cursor:pointer}
[aria-sort="ascending"]::after{content:" ↑"}
[aria-sort="descending"]::after{content:" ↓"}
a[aria-current="page"]{color:rgb(var(--c-primary))}
[data-empty][hidden]{display:none}
[data-modal]{position:fixed;inset:0;z-index:60;display:none;align-items:center;justify-content:center;padding:1rem;background:rgb(var(--c-ink) / .45);backdrop-filter:blur(2px)}
[data-modal].is-open{display:flex}
[data-modal]>*{width:100%;max-width:32rem;max-height:calc(100vh - 2rem);overflow:auto}
.app-toasts{position:fixed;right:1rem;bottom:1rem;z-index:80;display:flex;flex-direction:column;gap:.5rem;max-width:calc(100vw - 2rem)}
.app-toast{padding:.75rem 1rem;border-radius:var(--r);background:rgb(var(--c-ink));color:#fff;font-size:.9rem;box-shadow:var(--sh-lg);animation:app-toast-in .2s ease-out}
.app-toast.is-leaving{opacity:0;transform:translateY(4px);transition:opacity .3s ease,transform .3s ease}
@keyframes app-toast-in{from{opacity:0;transform:translateY(6px)}to{opacity:1;transform:none}}
details>summary{cursor:pointer;list-style:none}
details>summary::-webkit-details-marker{display:none}
@media (prefers-reduced-motion:reduce){*{animation:none !important;transition:none !important;scroll-behavior:auto !important}}
""".strip().replace("\n", "")
