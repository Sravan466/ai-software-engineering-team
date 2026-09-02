"""Turn a project's design into a renderable HTML mockup, and edit it one section at a time.

Unlike the pipeline agents (which emit structured JSON), the preview is a single static HTML
document we can drop straight into a sandboxed iframe — Tailwind via CDN, no build step, no
imports — so a local 7B model can produce something that actually *renders*. Edits operate on
one ``data-section`` fragment at a time, which keeps each request small and reliable.
"""
from __future__ import annotations
from typing import List, Optional, Tuple

import json
import re

from app.core.constants import RoutingMode
from app.router.router import router
from app.schemas.llm import ChatMessage, GenerationOptions, LLMResponse

_TAILWIND = "https://cdn.tailwindcss.com"

_GENERATE_SYSTEM = (
    "You are a senior product/UI designer. Output a SINGLE, self-contained, static HTML "
    "document that visually previews the described web product.\n\n"
    "Hard rules:\n"
    f'- Load Tailwind in <head> via <script src="{_TAILWIND}"></script> and style EVERYTHING '
    "with Tailwind utility classes. No external CSS/JS frameworks.\n"
    "- Output ONE complete document only: <!doctype html><html>…</html>. No prose, no markdown "
    "fences, no explanations.\n"
    '- Wrap every major visible block in <section data-section="kebab-id" data-label="Human '
    'Label"> … </section>. Ids are unique and kebab-case (e.g. hero, features, listings, '
    "testimonials, cta, footer).\n"
    "- For imagery use Tailwind gradient/solid color blocks or https://placehold.co/<w>x<h> "
    "placeholders — never local or broken image paths.\n"
    "- Write believable, specific copy for the product. Make it look polished and responsive."
)

_EDIT_SYSTEM = (
    "You are a precise front-end editor. You receive ONE HTML fragment — a single element that "
    "uses Tailwind utility classes — and a change request.\n\n"
    "Return ONLY the modified fragment:\n"
    "- Same outer element; keep its data-section and data-label attributes exactly as given.\n"
    "- Style with Tailwind utility classes. No <html>/<head>/<body> wrapper, no markdown "
    "fences, no commentary.\n"
    "- Apply the requested change faithfully and leave everything else intact."
)


def _clean_document(text: str) -> str:
    """Trim a model response down to just the HTML document."""
    t = (text or "").strip()
    fence = re.search(r"```(?:html)?\s*([\s\S]*?)```", t)
    if fence:
        t = fence.group(1).strip()
    low = t.lower()
    start = low.find("<!doctype")
    if start == -1:
        start = low.find("<html")
    if start > 0:
        t = t[start:]
    end = t.lower().rfind("</html>")
    if end != -1:
        t = t[: end + len("</html>")]
    return t.strip()


def _clean_fragment(text: str) -> str:
    """Trim a model response down to just the HTML fragment."""
    t = (text or "").strip()
    fence = re.search(r"```(?:html)?\s*([\s\S]*?)```", t)
    if fence:
        t = fence.group(1).strip()
    first, last = t.find("<"), t.rfind(">")
    if first != -1 and last > first:
        t = t[first: last + 1]
    return t.strip()


def _short(value: object, limit: int = 600) -> str:
    """Compact, readable one-liner for a JSON-ish value."""
    try:
        text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    except (TypeError, ValueError):
        text = str(value)
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def build_context(project) -> str:
    """Distil the team's outputs into a short brief for the preview generator.

    Lives here rather than in the route because the Frontend phase now produces the
    mockup as it finishes — the brief has two callers, and only one of them is an
    HTTP request.
    """
    by_phase = {ph.phase: ph.output for ph in project.phases if isinstance(ph.output, dict)}
    parts: List[str] = []

    pm = by_phase.get("product_manager", {})
    if pm.get("product_name"):
        parts.append(f"Product name: {pm['product_name']}")
    feats = pm.get("features") or pm.get("mvp_features") or pm.get("user_stories")
    if feats:
        parts.append(f"Key features: {_short(feats)}")

    fe = by_phase.get("frontend_engineer", {})
    if fe.get("framework"):
        parts.append(f"Frontend framework: {fe['framework']}")
    if fe.get("pages"):
        parts.append(f"Pages: {_short(fe['pages'])}")
    if fe.get("components"):
        parts.append(f"Components: {_short(fe['components'])}")

    ds = by_phase.get("system_design", {})
    if ds.get("tech_stack"):
        parts.append(f"Tech stack: {_short(ds['tech_stack'])}")

    return "\n".join(parts) or "(No detailed design yet — infer a sensible layout from the idea.)"


def generate_preview(
    idea: str,
    name: Optional[str],
    context: str,
    *,
    mode: RoutingMode = RoutingMode.LOCAL_ONLY,
    preferred_model: Optional[str] = None,
) -> Tuple[str, LLMResponse]:
    """Produce a full self-contained HTML preview document. Returns (html, llm_response)."""
    user = (
        f"# Product\n{name or idea}\n\n"
        f"Idea: {idea}\n\n"
        f"# What the team has designed so far\n{context}\n\n"
        "Produce the static HTML preview now."
    )
    resp = router.complete(
        [
            ChatMessage(role="system", content=_GENERATE_SYSTEM),
            ChatMessage(role="user", content=user),
        ],
        mode=mode,
        preferred_model=preferred_model,
        options=GenerationOptions(max_tokens=4096, json_mode=False, temperature=0.4),
        complexity="high",
    )
    return _clean_document(resp.text), resp


def edit_section(
    fragment: str,
    instruction: str,
    *,
    mode: RoutingMode = RoutingMode.LOCAL_ONLY,
    preferred_model: Optional[str] = None,
) -> Tuple[str, LLMResponse]:
    """Rewrite a single section fragment per the instruction. Returns (fragment, response)."""
    user = (
        f"# Change request\n{instruction}\n\n"
        f"# Current fragment\n{fragment}\n\n"
        "Return the modified fragment only."
    )
    resp = router.complete(
        [
            ChatMessage(role="system", content=_EDIT_SYSTEM),
            ChatMessage(role="user", content=user),
        ],
        mode=mode,
        preferred_model=preferred_model,
        options=GenerationOptions(max_tokens=2048, json_mode=False, temperature=0.3),
        complexity="medium",
    )
    return _clean_fragment(resp.text), resp
