"""Turn a project's design into a clickable site, and edit it one section at a time.

This used to be one call: a 7B model asked for a whole product in a single document,
from a four-line brief, and it returned 2.8 KB — one page, five sections, a form that
went nowhere. It is now a build of six passes, each small enough to fit the window of
whatever model the user chose, to retry on its own, and (on a cloud model) to run
beside the others:

  1. **design**  — palette, type, radius, shadow, density, voice → CSS tokens
  2. **plan**    — routes, their sections, and the collections behind them
  3. **seed**    — realistic records for every collection
  4. **sections**— one call per section, checked against its binding contract,
                   repaired once, replaced by the platform's template if still broken
  5. **runtime** — the platform's own router/store/forms/filters, inlined (not a call)
  6. **verify**  — structural checks on the assembled site, plus a headless render
                   where Playwright is installed

Nothing here names a model, a window or a token budget. Every pass asks the router
for the profile of the model that will answer it and sizes itself from that.
"""
from __future__ import annotations

import re
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import date
from typing import Optional, Type

from pydantic import BaseModel, ValidationError

from app.core.config import settings
from app.core.constants import RoutingMode
from app.core.logging import get_logger
from app.core.reading import json_object
from app.preview import design as D
from app.preview import document, verify
from app.preview import html as H
from app.preview import plan as P
from app.preview import seed as S
from app.preview import sections as X
from app.preview.brief import SiteBrief, brief_from_idea, site_brief
from app.preview.jobs import NullReporter, Reporter
from app.router.base import ProviderError
from app.router.model_profile import ModelProfile
from app.core import identity
from app.router.router import router
from app.schemas.agent_outputs import response_schema
from app.schemas.llm import ChatMessage, GenerationOptions, LLMResponse

log = get_logger(__name__)

#: The role these calls route by and bill to. The mockup is many model calls per build
#: and is its own line in Settings, so it can be pointed at a smaller or faster model
#: than the code phases without touching them.
ROLE = "preview"

__all__ = [
    "ROLE",
    "BuildResult",
    "EditRejected",
    "build_site",
    "edit_section",
    "site_brief",
    "brief_from_idea",
]


@dataclass
class BuildResult:
    html: str
    report: dict
    responses: list[LLMResponse] = field(default_factory=list)


class EditRejected(ValueError):
    """An edit that would break the section's bindings, after a repair round."""

    def __init__(self, problems: list[str], responses: Optional[list[LLMResponse]] = None) -> None:
        self.problems = problems
        #: The calls it took to find out — billed even though nothing was saved.
        self.responses = list(responses or [])
        super().__init__("; ".join(problems))


class EditTooLarge(ValueError):
    """The part to edit does not fit in what the model can read.

    It used to be cut to fit, silently: the model rewrote the half it saw and the
    other half disappeared from the mockup. Now the edit is refused, so the person
    can select something smaller.
    """

    def __init__(self, size: int, room: int) -> None:
        self.size = size
        self.room = room
        super().__init__(f"{size} characters, and the model can read {room}")


# ── one call ─────────────────────────────────────────────────────────────────
class _Calls:
    """Every model call one build makes, in order — for usage and the report."""

    def __init__(self, mode: RoutingMode, preferred_model: Optional[str]) -> None:
        self.mode = mode
        self.preferred_model = preferred_model
        self.responses: list[LLMResponse] = []
        self._profile: Optional[ModelProfile] = None

    @property
    def profile(self) -> ModelProfile:
        if self._profile is None:
            self._profile = router.profile_for(
                self.mode, self.preferred_model, complexity="high", role=ROLE
            )
        return self._profile

    @property
    def char_budget(self) -> int:
        return self.profile.prompt_char_budget

    @property
    def output_chars(self) -> int:
        return int(self.profile.max_output_tokens * max(settings.approx_chars_per_token, 1.0))

    def complete(self, messages: list[ChatMessage], options: GenerationOptions) -> LLMResponse:
        resp = router.complete(
            messages,
            mode=self.mode,
            preferred_model=self.preferred_model,
            options=options,
            complexity="high",
            role=ROLE,
        )
        self.responses.append(resp)
        return resp

    def json(
        self,
        system: str,
        instructions: str,
        context: str,
        schema: dict,
        model: Optional[Type[BaseModel]] = None,
        temperature: float = 0.4,
    ) -> Optional[object]:
        """A schema-constrained call. Returns the parsed object, or None if unusable.

        The instructions are never cut; the context is cut to whatever the window of
        the chosen model leaves after them.
        """
        room = max(self.char_budget - len(system) - len(instructions) - 60, 0)
        body = context[:room]
        user = (f"# What the team has designed\n{body}\n\n" if body else "") + f"# Task\n{instructions}"
        resp = self.complete(
            [ChatMessage(role="system", content=system), ChatMessage(role="user", content=user)],
            GenerationOptions(json_mode=True, json_schema=schema, temperature=temperature),
        )
        parsed = json_object(resp.text)
        if parsed is None:
            return None
        if model is None:
            return parsed
        try:
            return model.model_validate(parsed)
        except ValidationError as e:
            log.info("Mockup pass returned an unusable shape: %s", str(e)[:200])
            return None


# ── the passes ───────────────────────────────────────────────────────────────
def _design(calls: _Calls, brief: SiteBrief) -> D.DesignSystem:
    choice = calls.json(
        D.SYSTEM,
        D.instructions(),
        "\n\n".join(p for p in (brief.product_block(), brief.features_block()) if p),
        response_schema(D.DesignChoice),
        D.DesignChoice,
        temperature=0.7,
    )
    if not isinstance(choice, D.DesignChoice):
        return D.fallback(brief.product)
    return D.resolve(choice, brief.product)


def _plan(calls: _Calls, brief: SiteBrief, ds: D.DesignSystem) -> P.SitePlan:
    max_routes = max(settings.preview_max_routes, 2)
    max_sections = max(settings.preview_max_sections_per_route, 2)
    brief.tagline = ds.tagline or brief.tagline
    spec = calls.json(
        P.SYSTEM,
        P.instructions(max_routes, max_sections),
        brief.planning_text(),
        response_schema(P.PlanSpec),
        P.PlanSpec,
        temperature=0.4,
    )
    if isinstance(spec, P.PlanSpec) and spec.routes:
        site = P.normalise(spec, brief, max_routes=max_routes, max_sections=max_sections)
        if len(site.routes) >= 2:
            return site
    return P.normalise(
        P.fallback_spec(brief, max_routes),
        brief,
        max_routes=max_routes,
        max_sections=max_sections,
        source="fallback",
    )


def _seed(calls: _Calls, site: P.SitePlan, brief: SiteBrief, today: date):
    rows = S.row_budget(site, calls.output_chars, max(settings.preview_seed_rows, 3))
    raw = calls.json(
        S.SYSTEM,
        S.instructions(site, rows, today),
        brief.product_block(),
        S.response_schema(site, rows),
        None,
        temperature=0.8,
    )
    return S.seed_rows(site, raw if isinstance(raw, dict) else None, rows, today)


def _section(
    calls: _Calls,
    section: P.SectionPlan,
    site: P.SitePlan,
    brief: SiteBrief,
    ds: D.DesignSystem,
    rows: dict,
) -> X.SectionOutcome:
    """Generate, check, repair once, correct in place, or fall back — for one section."""
    options = GenerationOptions(json_mode=False, temperature=0.5)
    base = X.messages(section, site, brief, ds, rows, calls.char_budget)
    resp = calls.complete(base, options)
    used = 1
    inner, classes = X.unwrap(H.clean_fragment(resp.text), section)
    fatal, fixable = X.check(section, inner, site)
    status = "generated"

    if fatal or fixable:
        retry = calls.complete(
            X.repair_messages(base, resp.text, fatal + fixable, calls.char_budget), options
        )
        used += 1
        inner2, classes2 = X.unwrap(H.clean_fragment(retry.text), section)
        fatal2, fixable2 = X.check(section, inner2, site)
        # Keep whichever attempt is closer to working: a repair that came back worse
        # than what it was repairing is not an improvement.
        if len(fatal2) < len(fatal) or (len(fatal2) == len(fatal) and len(fixable2) <= len(fixable)):
            inner, classes, fatal, fixable = inner2, classes2, fatal2, fixable2
            status = "repaired"

    if fatal:
        return X.SectionOutcome(
            plan=section,
            html=_finish(X.fallback(section, site, brief), ds)[0],
            status="fallback",
            problems=fatal + fixable,
            calls=used,
        )

    inner, fixes = X.repair_in_place(section, inner, site)
    html, drawn = _finish(X.wrap(section, inner, classes), ds)
    if drawn:
        fixes.append(f"drew {drawn} picture(s)")
    return X.SectionOutcome(
        plan=section, html=html, status=status, problems=fixable, fixes=fixes, calls=used
    )


def _finish(fragment: str, ds: D.DesignSystem) -> tuple[str, int]:
    return document.draw_images(fragment, ds)


def _concurrency(calls: _Calls) -> int:
    """How many section calls run at once: one on a local runtime, a few on a cloud one.

    A local runtime serves one request at a time, so parallel calls only queue — and a
    queued call's timeout starts while it waits. A cloud provider serves them together.
    """
    provider = router.provider(calls.profile.provider)
    if provider is None or getattr(provider, "is_local", True):
        return 1
    return max(settings.preview_concurrency, 1)


# ── the build ────────────────────────────────────────────────────────────────
def build_site(
    brief: SiteBrief,
    *,
    mode: RoutingMode = RoutingMode.LOCAL_ONLY,
    preferred_model: Optional[str] = None,
    progress: Optional[Reporter] = None,
    today: Optional[date] = None,
) -> BuildResult:
    """Run all six passes and return the document, its report, and every call made.

    A provider failure ends the build — a mockup assembled entirely from the
    platform's templates because the model was unreachable is not the thing that was
    asked for. A call that returns something unusable does not: that pass falls back
    and the build carries on, and the report says which passes did.
    """
    progress = progress or NullReporter()
    today = today or date.today()
    calls = _Calls(mode, preferred_model)
    try:
        return _build(brief, calls, progress, today)
    except Exception as e:  # noqa: BLE001 - re-raised; only annotated on the way out
        # The calls before the failure happened and cost what they cost, whatever
        # failed; the caller bills them even though there is nothing to save.
        e.responses = list(calls.responses)  # type: ignore[attr-defined]
        raise


def _build(brief: SiteBrief, calls: "_Calls", progress: Reporter, today: date) -> BuildResult:
    started = time.monotonic()

    progress.stage("design")
    ds = _design(calls, brief)

    progress.stage("plan")
    site = _plan(calls, brief, ds)

    progress.stage("seed")
    rows, sources = _seed(calls, site, brief, today)

    todo = site.all_sections()
    progress.stage("sections", total=len(todo))
    outcomes: dict[str, X.SectionOutcome] = {}

    def one(section: P.SectionPlan) -> X.SectionOutcome:
        outcome = _section(calls, section, site, brief, ds, rows)
        progress.step(section.label)
        return outcome

    workers = _concurrency(calls)
    if workers == 1:
        for section in todo:
            outcomes[section.id] = one(section)
    else:
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="mockup-section") as pool:
            # Each worker starts with no account; `carry` hands it the build's.
            futures = {pool.submit(identity.carry(one), section): section for section in todo}
            try:
                for future, section in futures.items():
                    outcomes[section.id] = future.result()
            except ProviderError:
                for future in futures:
                    future.cancel()
                raise

    progress.stage("verify")
    html = document.assemble(site, ds, {sid: o.html for sid, o in outcomes.items()}, rows)
    checks = verify.static_checks(html, site)
    render = verify.render_check(html, [r.path for r in site.routes])

    ordered = [outcomes[s.id] for s in todo]
    counts = {status: sum(1 for o in ordered if o.status == status)
              for status in ("generated", "repaired", "fallback")}
    report = {
        "version": 2,
        "product": site.product,
        "model": calls.profile.model,
        "provider": calls.profile.provider,
        "passes": {
            "design": ds.source,
            "plan": site.source,
            "seed": "model" if all(v == "model" for v in sources.values())
            else ("generated" if all(v == "generated" for v in sources.values()) else "mixed"),
        },
        "design": ds.as_dict(),
        "plan_notes": site.notes,
        "routes": [{"path": r.path, "title": r.title, "sections": len(r.sections)} for r in site.routes],
        "collections": [
            {"name": c.name, "label": c.label, "rows": len(rows.get(c.name, [])), "source": sources.get(c.name)}
            for c in site.collections
        ],
        "sections": [o.report() for o in ordered],
        "counts": counts,
        "checks": checks,
        "render": render,
        "bytes": len(html.encode("utf-8")),
        "calls": len(calls.responses),
        "tokens": sum(r.usage.total_tokens for r in calls.responses),
        "elapsed_ms": int((time.monotonic() - started) * 1000),
    }
    log.info(
        "Mockup built for %s: %d page(s), %d section(s) (%d fallback), %s KB, %d call(s)",
        site.product,
        len(site.routes),
        len(ordered),
        counts["fallback"],
        f"{report['bytes'] / 1024:.1f}",
        report["calls"],
    )
    return BuildResult(html=html, report=report, responses=calls.responses)


# ── editing one section ──────────────────────────────────────────────────────
_LEGACY_EDIT_SYSTEM = (
    "You are a precise front-end editor. You receive ONE HTML fragment — a single element that "
    "uses Tailwind utility classes — and a change request.\n\n"
    "Return ONLY the modified fragment:\n"
    "- Same outer element; keep its data-section and data-label attributes exactly as given.\n"
    "- Style with Tailwind utility classes. No <html>/<head>/<body> wrapper, no markdown "
    "fences, no commentary, no <script>.\n"
    "- Apply the requested change faithfully and leave everything else intact."
)

_EDIT_SYSTEM = (
    "You are a precise front-end editor working on one section of a clickable prototype. You "
    "receive the section's HTML and a change request, and return the whole section with the "
    "change applied.\n\n"
    "Rules:\n"
    "- Return ONLY the section element — no <html>/<head>/<body>, no <script> or <style>, no "
    "markdown fences, no commentary.\n"
    "- Keep every data-* attribute the section relies on: the platform's runtime drives the "
    "section through them (lists, filters, sorting, forms, figures, navigation). The contract "
    "below says which ones matter.\n"
    "- Style with Tailwind and the design-system classes. Apply the change faithfully and leave "
    "everything else intact."
)


def edit_section(
    fragment: str,
    instruction: str,
    *,
    mode: RoutingMode = RoutingMode.LOCAL_ONLY,
    preferred_model: Optional[str] = None,
    site: Optional[dict] = None,
    section_id: Optional[str] = None,
) -> tuple[str, list[LLMResponse]]:
    """Rewrite one section per the instruction. Returns (fragment, every call made).

    For a site document (`site` is its recorded data) the edited section is checked
    against the same contract it was generated under: an edit that drops the list's
    template or renames the form's fields would leave a section that looks right and
    no longer works. Such an edit gets one repair round, then is refused with the
    reasons rather than saved.
    """
    calls = _Calls(mode, preferred_model)
    section = P.section_from_data(site, section_id) if site and section_id else None
    if site is None or section is None:
        return _edit_legacy(calls, fragment, instruction), calls.responses

    plan = P.site_from_data(site)
    ds = D.from_dict(site.get("design"), plan.product)
    fixed = (
        f"{ds.vocabulary()}\n\n"
        "# Site map\n" + "\n".join(f"- #{r.path} — {r.title}" for r in plan.routes) + "\n\n"
        f"# Contract this section must keep\n{X.contract(section, plan)}\n\n"
        f"# Change request\n{instruction}\n\n"
        "Return the whole section element with the change applied."
    )
    room = max(calls.char_budget - len(_EDIT_SYSTEM) - len(fixed) - 40, 0)
    if len(fragment) > room:
        raise EditTooLarge(len(fragment), room)
    base = [
        ChatMessage(role="system", content=_EDIT_SYSTEM),
        ChatMessage(role="user", content=f"# Current section\n{fragment}\n\n{fixed}"),
    ]
    options = GenerationOptions(json_mode=False, temperature=0.3)

    resp = calls.complete(base, options)
    inner, classes = X.unwrap(H.clean_fragment(resp.text), section)
    fatal, fixable = X.check(section, inner, plan)
    if fatal or fixable:
        retry = calls.complete(
            X.repair_messages(base, resp.text, fatal + fixable, calls.char_budget), options
        )
        inner2, classes2 = X.unwrap(H.clean_fragment(retry.text), section)
        fatal2, _fixable2 = X.check(section, inner2, plan)
        if len(fatal2) <= len(fatal):
            inner, classes, fatal = inner2, classes2, fatal2
    if fatal:
        raise EditRejected(fatal, calls.responses)

    inner, _fixes = X.repair_in_place(section, inner, plan)
    # The wrapper's classes survive an edit that did not mention them — "make the
    # heading bigger" is not a request to lose the section's background.
    if not classes:
        current = H.outer_element(fragment)
        classes = (H.attr(current[1], "class") or "") if current else ""
        platform = X.WRAPPER_CLASS.get(section.kind, "")
        classes = " ".join(c for c in classes.split() if c not in platform.split())
    html, _drawn = _finish(X.wrap(section, inner, classes), ds)
    return html, calls.responses


def _edit_legacy(calls: _Calls, fragment: str, instruction: str) -> str:
    """The single-document mockups drawn before sites existed, edited as before."""

    def assemble(html: str) -> str:
        return (
            f"# Change request\n{instruction}\n\n"
            f"# Current fragment\n{html}\n\n"
            "Return the modified fragment only."
        )

    overhead = len(_LEGACY_EDIT_SYSTEM) + len(assemble(""))
    room = max(calls.char_budget - overhead, 0)
    if len(fragment) > room:
        raise EditTooLarge(len(fragment), room)
    user = assemble(fragment)
    resp = calls.complete(
        [
            ChatMessage(role="system", content=_LEGACY_EDIT_SYSTEM),
            ChatMessage(role="user", content=user),
        ],
        GenerationOptions(json_mode=False, temperature=0.3),
    )
    return H.clean_fragment(resp.text)


# ── editing one element ──────────────────────────────────────────────────────
_ELEMENT_SYSTEM = (
    "You are a precise front-end editor. You receive ONE HTML element from a clickable "
    "prototype styled with Tailwind utility classes, the elements it sits inside, and a change "
    "request. Return ONLY that element with the change applied.\n\n"
    "Rules:\n"
    "- Same outer tag. Keep every data-* attribute exactly as given: the prototype's runtime "
    "drives the page through them.\n"
    "- No <html>/<head>/<body>, no <script> or <style>, no markdown fences, no commentary.\n"
    "- Apply the change faithfully and leave everything else intact."
)
#: Attributes the runtime reads. An edited element must keep each one it had.
_BINDING = re.compile(
    r"\s(data-(?:section|route|list|field|stat|count|empty|form|filter|sort|toggle|action|"
    r"modal|open|close|to|limit|error-for|success|redirect))\s*=\s*([\"'])(.*?)\2"
)


def _bindings(markup: str) -> set:
    return {(m.group(1), m.group(3)) for m in _BINDING.finditer(markup or "")}


def edit_element(
    fragment: str,
    instruction: str,
    *,
    context: list[str],
    mode: RoutingMode = RoutingMode.LOCAL_ONLY,
    preferred_model: Optional[str] = None,
    site: Optional[dict] = None,
) -> tuple[str, list[LLMResponse]]:
    """Rewrite one element — a heading, a card, a list's row template — and nothing else.

    The model sees the element and the start tags around it, so "make this bolder"
    knows it is a link in the navbar. What it returns must keep every binding the
    element had; one repair round, then the edit is refused with what went missing.
    """
    calls = _Calls(mode, preferred_model)
    ds = None
    if site is not None:
        plan = P.site_from_data(site)
        ds = D.from_dict(site.get("design"), plan.product)
    around = "\n".join(context) or "(the page body)"
    fixed = (
        (f"{ds.vocabulary()}\n\n" if ds else "")
        + f"# Inside\n{around}\n\n# Change request\n{instruction}\n\n"
        "Return the whole element with the change applied."
    )
    room = max(calls.char_budget - len(_ELEMENT_SYSTEM) - len(fixed) - 40, 0)
    if len(fragment) > room:
        raise EditTooLarge(len(fragment), room)
    base = [
        ChatMessage(role="system", content=_ELEMENT_SYSTEM),
        ChatMessage(role="user", content=f"# Current element\n{fragment}\n\n{fixed}"),
    ]
    options = GenerationOptions(json_mode=False, temperature=0.3)
    wanted = _bindings(fragment)
    original = H.outer_element(fragment)

    def attempt(messages: list[ChatMessage]) -> tuple[str, list[str], str]:
        resp = calls.complete(messages, options)
        cleaned = H.clean_fragment(resp.text)
        problems = []
        got = H.outer_element(cleaned)
        if got is None:
            problems.append("it didn't come back as one element")
        elif original and got[0] != original[0]:
            problems.append(f"it came back as a <{got[0]}> instead of a <{original[0]}>")
        missing = sorted(f'{n}="{v}"' for n, v in wanted - _bindings(cleaned))
        if missing:
            problems.append("it dropped " + ", ".join(missing[:4]))
        if cleaned and not H.tags_balance(cleaned):
            problems.append("its tags don't close")
        return cleaned, problems, resp.text

    cleaned, problems, raw = attempt(base)
    if problems:
        retry = base + [
            ChatMessage(role="assistant", content=raw[:4000]),
            ChatMessage(
                role="user",
                content="That can't be used: " + "; ".join(problems)
                + ". Return the corrected element only.",
            ),
        ]
        cleaned2, problems2, _raw2 = attempt(retry)
        if len(problems2) <= len(problems):
            cleaned, problems = cleaned2, problems2
    if problems:
        raise EditRejected(problems, calls.responses)
    if ds is not None:
        cleaned, _drawn = document.draw_images(cleaned, ds)
    return cleaned, calls.responses
