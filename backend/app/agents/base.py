"""Base agent: prompt assembly, routing, schema-checked parsing, markdown rendering.

Each specialist subclass declares its role (system prompt), the *type* its deliverable
must have, and which upstream phase outputs it depends on. The base class handles
everything else: sizing the prompt to the model actually chosen, calling the router with
the schema attached, checking what came back against that same type, sending one repair
round when it misses, and turning the result into display markdown.

The sizing matters as much as the schema. Every truncation length here used to be a
literal — 6000 characters of prior-phase context, 4000 of reference material — chosen
against no particular model. Combined with a provider that never set a context window,
that put the later phases over the limit, and a runtime truncates from the head: the system
prompt, and with it the required shape, went first. Both halves are now derived from the
window the model reports.
"""
from __future__ import annotations
from typing import Optional

import json
from dataclasses import dataclass, field

from pydantic import BaseModel, ValidationError

from app.agents import handoff
from app.router import inflight
from app.build import contract as build_contract
from app.build import layout as build_layout
from app.build import buildlog
from app.build import runner as build_runner
from app.build.check import BuildCheck, check_phase, phase_tree
from app.core.config import settings
from app.core.constants import CODE_PHASES, PHASE_ORDER, BuildStatus, Phase, RoutingMode, SchemaStatus
from app.core.logging import get_logger
from app.core.reading import has_content, json_object
from app.orchestration.charter import Charter
from app.orchestration.charter import violations as charter_violations
from app.orchestration.claim import Superseded
from app.router.model_profile import ModelProfile
from app.router.base import RequestCancelled
from app.router.router import router
from app.schemas.agent_outputs import GenericOutput, response_schema, shape_text, subset_schema
from app.schemas.llm import ChatMessage, GenerationOptions, LLMResponse, Usage
from app.skills.loader import render as render_skill
from app.skills.selection import Selected

log = get_logger(__name__)

#: The phases whose code is built for real (#75), and the side each one builds. QA's
#: tests run in the same sandbox (#76) — see `QAEngineerAgent._run_tests`.
_BUILT_SIDES = {
    Phase.FRONTEND_ENGINEER.value: build_layout.FRONTEND,
    Phase.BACKEND_ENGINEER.value: build_layout.BACKEND,
}

#: What a person wrote gets whatever it needs, up to this share of the budget each.
#: These are normally a sentence or two, so the cap almost never binds — but `idea`
#: and `feedback` have no maximum length at the API, and an unbounded section is one
#: that overruns the window however carefully the rest is measured.
_PERSON_SHARE = {
    "idea": 0.25,
    "feedback": 0.15,
    "extra": 0.10,
    # Not a person's words, but sized the same way: the scanners' findings (#77) are
    # served whole when they fit and cut to this share when they don't. Empty for
    # every agent but Warden, so nobody else pays for it.
    "scan": 0.12,
    # A change request (#79): the person's words and the plan. Empty on every other run.
    "change": 0.15,
}
#: How whatever remains is divided between the sections the pipeline assembles.
#: `skills` is a claimant here rather than a constant of its own, and that is the
#: whole point. A separate budget added on top of this allocator is how a phase ends
#: up over the window on a small local model — precisely the truncation
#: `model_profile.py` exists to prevent. On a 7B window a skill arriving means RAG
#: or memory gets less, and that is the correct trade, made visibly.
_CONTEXT_SHARE = {
    "depends_on": 0.55,
    "rag": 0.19,
    "skills": 0.18,
    "memory": 0.08,
    # The deliverable a change edits (#79). Present only then, so every other run's
    # shares renormalise to exactly what they were.
    "current": 0.45,
}
#: How the deliverable being changed is introduced, and how the edit is asked for (#79).
_CURRENT_FRAME = "# What you delivered before — change it, don't start over\n{body}\n"
_CHANGE_FRAME = "# The change to make\n{body}\n"
_EDIT_TASK = (
    "You are changing an existing app, not building it again. Return the JSON object "
    "with the change made. In every list of files (`files`, `test_files`, …) return ONLY "
    "the files you add or change, each complete, and put the paths of files you remove in "
    "`deleted`. Every file you don't return is kept exactly as it is; every other field "
    "you leave out keeps its current value."
)
#: The text wrapping each optional section. Written once and used twice — to build
#: the section, and to charge its cost against the budget — because a frame that is
#: estimated on one side and printed on the other is a frame the prompt overruns by
#: however far the estimate was off.
_DEP_FRAME = handoff.DEP_FRAME
_RAG_FRAME = "# Reference material (from the uploaded knowledge base)\n{body}\n"
_MEMORY_FRAME = "# Lessons from past projects (long-term memory)\n{body}\n"
_SKILLS_FRAME = "# How this team does this work — follow these procedures\n{body}\n"
#: What the heading and the join around it cost, charged *inside* the skills budget
#: rather than on top of it. That is what lets the block be dropped entirely when
#: nothing fits: the skeleton that measures the overhead and the prompt that is sent
#: both omit it, so the two still cost the same — and a phase on a 4k window is not
#: handed "follow these procedures" with the knowledge base sitting underneath it as
#: the only thing that looks like an answer.
_SKILLS_FRAME_COST = len(_SKILLS_FRAME.format(body="")) + 1  # + the "\n" join


@dataclass
class AgentContext:
    """Everything an agent needs to do its job for one project/phase."""

    idea: str
    routing_mode: RoutingMode = RoutingMode.LOCAL_ONLY
    preferred_model: Optional[str] = None
    prior_outputs: dict[str, dict] = field(default_factory=dict)  # phase -> structured output
    rag_context: str = ""
    memory_context: str = ""
    #: The skills chosen for this phase, best first. Candidates rather than a
    #: finished block: *which* of them fit is decided against the window of the
    #: model this agent is about to call, which is knowledge the selector does not
    #: have and the graph node that assembled this list does not either.
    skills: tuple[Selected, ...] = ()
    feedback: Optional[str] = None  # human guidance when a phase is re-run after rejection
    extra_context: str = ""  # e.g. an agent-debate decision injected before a phase
    #: What the security scanners already reported (#77), capped — Warden only.
    scan_context: str = ""
    #: The technology decisions frozen after the architecture was settled. Printed
    #: into this agent's system prompt and checked against what it writes.
    charter: Optional[Charter] = None
    #: The fix loop's last round: route this call as the hardest kind of work, so it
    #: lands on the most capable model the router would pick for it, and give it one
    #: more self-repair round. Chosen by the router's own ranking, never by name.
    escalate: bool = False
    #: The model an escalated call is pinned to, resolved once per run by the router.
    pin_model: Optional[str] = None
    #: A change made on the app preview (#78): the attempt says its own site style.
    revising: bool = False
    #: A change request on a finished build (#79): what to change, in the person's words
    #: plus the plan's, and this phase's current deliverable to change it in. Set
    #: together: with both, the agent *edits* — it is shown what it wrote and returns
    #: only what it adds or changes — instead of writing the deliverable again.
    change: str = ""
    base_output: Optional[dict] = None
    #: Paths the change is likely to touch, whose current code is shown in full.
    likely_files: tuple[str, ...] = ()

    @property
    def editing(self) -> bool:
        return bool(self.change) and self.base_output is not None


class RevisionRefused(Exception):
    """A change made on the app preview (#78) that wasn't kept, with the reason a
    person reads: it broke the build, or the crew didn't send the file back."""


@dataclass
class AgentResult:
    output: dict
    content_md: str
    response: LLMResponse
    #: valid | repaired | invalid — whether the output matched its declared shape,
    #: and whether it took a second call to get there.
    schema_status: str = SchemaStatus.VALID.value
    #: What was wrong, when something was. Kept short enough to show a person.
    schema_note: Optional[str] = None
    repair_rounds: int = 0
    #: Every model call this deliverable took, in order. `response` is their sum;
    #: analytics records one event per call, because that is what happened.
    calls: list[LLMResponse] = field(default_factory=list)
    #: Where this deliverable contradicts the stack charter, after every repair round
    #: had its chance. Empty is the normal case and the only shippable one — a phase
    #: that still disagrees with the architecture it was built on stops the run, in
    #: exactly the way a phase that missed its declared shape does.
    stack_violations: list[str] = field(default_factory=list)
    #: Whether this phase's code compiles — ok | failed | unchecked — and, when it does
    #: not, `[{path, line, kind, message}]` for what is still wrong after the repair
    #: round. None for a phase that writes no code.
    build_status: Optional[str] = None
    build_problems: list[dict] = field(default_factory=list)
    #: The skills this deliverable was actually written with, in the order they were
    #: injected. What was *selected* is not the same fact: a skill that did not fit
    #: the window never reached the model, and recording it would make a skill you
    #: cannot confirm was used indistinguishable from one that did nothing.
    skills_used: list[str] = field(default_factory=list)
    #: What this agent was shown of each phase before it — digest, whole output, or
    #: cut — whether the name registry was in its instructions, and how many of its
    #: replies were cut off at the output limit. The "What this agent saw" panel.
    handoff: dict = field(default_factory=dict)
    truncated_replies: int = 0
    #: The real build (#75): what installing, building and starting this phase's code
    #: did — `BuildRun.as_dict()`. None for a phase that isn't built, or whose files
    #: didn't parse, so there was nothing to build.
    build_run: Optional[dict] = None
    #: QA's tests, run for real (#76): `testrun.combine(...)`. Written by the platform,
    #: never by the model. None for every phase but QA.
    test_run: Optional[dict] = None
    #: The security scanners' run (#77): `scan.ScanResult.as_dict()`. Written by the
    #: platform before Warden's model call, never by the model. None for every other
    #: phase.
    scan: Optional[dict] = None
    #: Re-checked from an earlier attempt rather than generated (#76): the fix loop
    #: kept QA's tests while an engineer fixed the code they test. No model call.
    kept: bool = False


@dataclass
class Prompt:
    """A built ask, and which skills survived the budget to be part of it."""

    messages: list[ChatMessage]
    skills_used: list[str] = field(default_factory=list)
    #: One record per dependency: what of it reached the model.
    deps: list[dict] = field(default_factory=list)


@dataclass
class _Deps:
    """Each dependency's digest (already fitted) and what its full output would add.

    Computed once per prompt: the budget pass and the real assembly both read it, and
    serialising several hundred kilobytes of generated source twice to count it is
    the cost `bodies` used to exist to avoid.
    """

    digests: dict[str, str] = field(default_factory=dict)
    wants: dict[str, int] = field(default_factory=dict)


class BaseAgent:
    #: phase key == constants.Phase value
    key: str = "base"
    title: str = "Base Agent"
    complexity: str = "medium"  # routing hint: low | medium | high

    #: One-line description of the agent's mandate (also shown in the UI).
    role: str = ""
    #: The type this agent's deliverable must have. It is the single source of the
    #: shape: the prompt sketch, the decoding constraint and the validation all come
    #: from here, so they cannot fall out of step with one another.
    output_model: type[BaseModel] = GenericOutput
    #: Which prior phase outputs to inline as context (by phase key).
    depends_on: tuple[str, ...] = ()

    # ── the declared shape, three ways ──────────────────────────────────────
    @property
    def output_spec(self) -> str:
        """The JSON sketch shown to the model, rendered from `output_model`."""
        return shape_text(self.output_model)

    @classmethod
    def response_schema(cls) -> dict:
        """The JSON Schema providers constrain decoding to."""
        return response_schema(cls.output_model)

    # ── public entrypoint ───────────────────────────────────────────────────
    def run(self, ctx: AgentContext) -> AgentResult:
        self._pin(ctx)
        profile = router.profile_for(

            ctx.routing_mode, ctx.preferred_model, complexity=self._complexity(ctx), role=self.key, pin=ctx.pin_model
        )
        ask = self._build_messages(ctx, profile)
        options = GenerationOptions(json_mode=True, json_schema=self._schema_for(ctx))

        resp = self._complete(ask.messages, ctx, options)
        responses = [resp]
        # A reply cut off at the output limit is kept for its finished fields, and
        # the next round asks only for what is missing — not for the whole object
        # again, under the same output cap that cut it the first time.
        partial: dict = {}
        raw, partial, cut = self._read_reply(resp, partial)
        truncated = 1 if cut else 0
        echo = resp.text
        output, errors = self._check(raw, ctx)
        if cut and self._missing(raw):
            errors = [_TRUNCATED_ERROR] + errors
        best = (output, errors)
        # Which skills the *kept* attempt was written with. A repair round is sized
        # down to make room for the echoed attempt, so it can carry fewer of them —
        # and the provenance has to describe the answer that survived, not the ask
        # that was abandoned.
        best_skills = list(ask.skills_used)

        rounds = 0
        allowed = max(settings.schema_repair_rounds, 0) + (1 if ctx.escalate else 0)
        while errors and rounds < allowed:
            rounds += 1
            log.warning(
                "%s returned output that does not match its shape (%s). Repair round %d.",
                self.title,
                "; ".join(errors[:3]),
                rounds,
            )
            # Rebuilt from the original ask each round, carrying only the *latest*
            # attempt, and with the room for that attempt taken *out* of the context
            # budget rather than added on top of it. Appending an echo to a prompt
            # already sized to fill the window is how the repair call — the one whose
            # whole job is to restate the shape — gets truncated from the head and
            # loses the system prompt that carries it.
            if partial:
                missing = self._missing(partial)
                retry = self._continue_messages(ctx, profile, partial, missing)
                round_options = options.model_copy(
                    update={"json_schema": subset_schema(self.output_model, missing)}
                )
            else:
                retry = self._repair_messages(ctx, profile, echo, errors)
                round_options = options
            resp = self._complete(retry.messages, ctx, round_options)
            responses.append(resp)
            merged = bool(partial)
            raw, partial, cut = self._read_reply(resp, partial)
            truncated += 1 if cut else 0
            # A continuation answered only the missing keys; the next repair, if one
            # is needed, is about the whole object they make together.
            echo = json.dumps(raw) if merged else resp.text
            output, errors = self._check(raw, ctx)
            if cut and self._missing(raw):
                errors = [_TRUNCATED_ERROR] + errors
            # Fewer things wrong wins. Without this the *last* attempt is kept
            # whatever it looks like, so a repair that came back worse than the
            # response it was repairing is what reaches the database and the gates.
            if len(errors) < len(best[1]):
                best = (output, errors)
                best_skills = list(retry.skills_used)
        output, errors = best
        # Two different failures, kept apart all the way to the reviewer. "This did
        # not match its declared shape" and "this contradicts the stack everyone else
        # is building against" are fixed by different people in different ways, and
        # the panels downstream say which happened.
        stack_errors = (
            charter_violations(ctx.charter, self.key, output)
            if settings.enforce_stack_charter
            else []
        )
        # And a third: whether it compiles. Recorded apart from the other two because
        # "this does not parse" is neither a shape problem nor a stack problem, and
        # the Ship review says which of the three a build still has.
        build = self._build_check(ctx, output)
        build_errors = build.messages() if build else []
        shape_errors = [e for e in errors if e not in stack_errors and e not in build_errors]
        # Then, once it parses, for real: installed, built and started in a sandbox.
        # What fails there goes to the fix loop with the parser's problems.
        build_run = self._run_build(ctx, output, build)
        # And QA's tests are run (#76). A suite that can't be collected joins `build`
        # as the tests' own problems; tests that run and fail are the fix loop's.
        test_run = self._run_tests(ctx, output, build)

        if errors:
            # Repair stops paying after a round or two, and a local model pays
            # wall-clock for every attempt. Keep the best try — and say so, because
            # the cost and security gates downstream read these keys, and a gate that
            # cannot find them is a gate that silently stops gating.
            log.error(
                "%s output still has %d problem(s) after %d repair round(s): %s",
                self.title,
                len(errors),
                rounds,
                "; ".join(errors[:3]),
            )

        status = (
            SchemaStatus.INVALID.value
            if shape_errors
            else (SchemaStatus.REPAIRED.value if rounds else SchemaStatus.VALID.value)
        )
        record = {
            "deps": ask.deps,
            "registry": bool(self._registry(ctx)),
            "contract": self._contract(ctx) is not None,
            "truncated_replies": truncated,
        }
        return AgentResult(
            output=output,
            content_md=self.to_markdown(output),
            response=_merge(responses),
            schema_status=status,
            # The model gets the backticked form in the repair prompt, where they
            # delimit a field name. A person reads this in a sentence on screen.
            schema_note="; ".join(e.replace("`", "") for e in shape_errors[:3]) or None,
            repair_rounds=rounds,
            calls=responses,
            stack_violations=stack_errors,
            build_status=build.status if build else None,
            build_problems=build.as_list() if build else [],
            skills_used=best_skills,
            handoff=record,
            truncated_replies=truncated,
            build_run=build_run,
            test_run=test_run,
        )

    def recheck(self, ctx: AgentContext, kept: dict) -> AgentResult:
        """Check an earlier attempt again, against the phases rebuilt before it (#76).

        The fix loop's way of asking "did the engineer's fix turn these tests green?"
        of *the same tests*: QA's suite is kept while the code it tests is rewritten,
        then compiled and run again here — no model call, so nothing about the suite
        changes between the run that failed and the one that judges the fix.
        """
        output, errors = self._validate(dict(kept.get("output") or {}))
        if not errors:
            output, own = self.own_checks(output, ctx)
            errors = errors + own
        stack_errors = (
            charter_violations(ctx.charter, self.key, output) if settings.enforce_stack_charter else []
        )
        build = self._build_check(ctx, output)
        # The same files build the same way: a kept phase whose side hashes as it did
        # when it was built isn't installed and built again.
        build_run = self._run_build(ctx, output, build, reuse=kept.get("build_run"))
        test_run = self._run_tests(ctx, output, build)
        if isinstance(test_run, dict):
            test_run["kept"] = True
        response = LLMResponse(
            text="",
            provider=str(kept.get("provider") or "platform"),
            model=str(kept.get("model") or "kept"),
            usage=Usage(),
            latency_ms=0,
            is_local=kept.get("is_local"),
        )
        return AgentResult(
            output=output,
            content_md=self.to_markdown(output),
            response=response,
            schema_status=SchemaStatus.INVALID.value if errors else SchemaStatus.VALID.value,
            schema_note="; ".join(e.replace("`", "") for e in errors[:3]) or None,
            calls=[],
            stack_violations=stack_errors,
            build_status=build.status if build else None,
            build_problems=build.as_list() if build else [],
            skills_used=list(kept.get("skills_used") or []),
            # `kept_from`: the attempt this code was first written in, whose mockup
            # draw is still of this code.
            handoff={**(kept.get("handoff") or {}), "kept": True, "kept_from": kept.get("kept_from")},
            build_run=build_run,
            test_run=test_run,
            kept=True,
        )

    def revise(self, ctx: AgentContext, spec: dict) -> AgentResult:
        """A change made on the app preview (#78), as a new attempt at this phase.

        The person changed the running app, so the change is to the code: this attempt
        is the current one with that change made, then checked the way any attempt is
        — compiled, and built for real. `spec` carries the current attempt (`output`,
        `build_run`, `build_status`) and one change:

          * `files` — `{path: content}`, typed over on the preview (no model);
          * `theme` — the site style, kept with the attempt for the scaffold to write;
          * `restore` — an earlier attempt's `output` and `build_run` (undo and redo);
          * `ask` — "change this element", for the crew: one write call for one file
            (`_ask`, code phases only).

        A change that breaks what built before is refused, not kept: `RevisionRefused`
        names why, and the attempt on screen stays the current one. The fix loop is for
        the crew's own mistakes, and it rewrites a whole phase to fix one.
        """
        from copy import deepcopy

        base = spec.get("restore") if isinstance(spec.get("restore"), dict) else spec
        output = deepcopy(base.get("output") or {})
        changed: list[str] = []
        files = [dict(f) for f in output.get("files") or [] if isinstance(f, dict)]
        for path, content in (spec.get("files") or {}).items():
            entry = next((f for f in files if f.get("path") == path), None)
            if entry is None:
                raise RevisionRefused(f"{path} isn't one of the files this phase wrote.")
            entry["code"] = content
            entry.pop("content", None)
            changed.append(path)
        output["files"] = files
        if "theme" in spec:
            if spec["theme"]:
                output["app_theme"] = spec["theme"]
            else:
                output.pop("app_theme", None)
        calls: list[LLMResponse] = []
        if isinstance(spec.get("ask"), dict):
            output, calls = self._ask(ctx, output, spec["ask"])
            changed.append(str(spec["ask"].get("path") or ""))

        output, errors = self._validate(output)
        if not errors:
            output, own = self.own_checks(output, ctx)
            errors = errors + own
        stack_errors = (
            charter_violations(ctx.charter, self.key, output) if settings.enforce_stack_charter else []
        )
        build = self._build_check(ctx, output)
        before_ok = spec.get("build_status") != BuildStatus.FAILED.value
        if build is not None and build.status == BuildStatus.FAILED.value and before_ok:
            raise RevisionRefused("That change doesn't compile: " + "; ".join(build.messages()[:2]))
        build_run = self._run_build(ctx, output, build, reuse=base.get("build_run"))
        built_before = (spec.get("build_run") or {}).get("status") != BuildStatus.FAILED.value
        if isinstance(build_run, dict) and build_run.get("status") == BuildStatus.FAILED.value and built_before:
            first = next(iter(build_run.get("problems") or []), None)
            detail = f" — {first.get('path')}: {first.get('message')}" if isinstance(first, dict) else ""
            raise RevisionRefused(f"That change didn't build ({build_run.get('summary')}){detail}"[:400])
        response = _merge(calls) if calls else LLMResponse(
            text="", provider="platform", model="preview edit", usage=Usage(), latency_ms=0, is_local=None
        )
        handoff = {k: v for k, v in (spec.get("handoff") or {}).items() if k not in ("kept", "kept_from", "edit")}
        handoff["edit"] = {
            "kind": spec.get("kind") or ("ask" if calls else "patch"),
            "label": spec.get("label") or "",
            "files": [c for c in changed if c][:6],
        }
        return AgentResult(
            output=output,
            content_md=self.to_markdown(output),
            response=response,
            schema_status=SchemaStatus.INVALID.value if errors else SchemaStatus.VALID.value,
            schema_note="; ".join(e.replace("`", "") for e in errors[:3]) or None,
            calls=calls,
            stack_violations=stack_errors,
            build_status=build.status if build else None,
            build_problems=build.as_list() if build else [],
            skills_used=list(spec.get("skills_used") or []),
            handoff=handoff,
            build_run=build_run,
        )

    def _ask(self, ctx: AgentContext, output: dict, ask: dict) -> tuple[dict, list[LLMResponse]]:
        """Change one file as the person asked (#78). Only code phases write files."""
        raise RevisionRefused(f"{self.title} doesn't write files, so it can't change one.")

    def _run_tests(self, ctx: AgentContext, output: dict, build: Optional[BuildCheck]) -> Optional[dict]:
        """Run the tests this phase wrote (#76). Only QA writes any."""
        return None

    def _pin(self, ctx: AgentContext) -> None:
        """The fix loop's last round: pin this call to the strongest model, once."""
        if ctx.escalate and ctx.pin_model is None:
            try:
                ctx.pin_model = router.strongest_for(
                    ctx.routing_mode, ctx.preferred_model, role=self.key
                )
            except Exception as e:  # noqa: BLE001 - escalation is best effort
                log.warning("%s: no stronger model could be chosen: %s", self.title, e)
            if ctx.pin_model:
                log.info("%s: last fix round runs on %s", self.title, ctx.pin_model)

    def _missing(self, fields: dict) -> list[str]:

        """Declared fields with no answer yet, under their own name or a drift alias."""
        out = []
        for name, info in self.output_model.model_fields.items():
            if not info.is_required():
                continue
            names = {name, *(getattr(info.validation_alias, "choices", None) or ())}
            if not any(n in fields for n in names):
                out.append(name)
        return out

    def _read_reply(self, resp: LLMResponse, partial: dict) -> tuple[dict, dict, bool]:
        """(what to check, finished fields still waiting for the rest, was it cut off).

        A cut reply contributes the fields that arrived whole; a continuation is
        merged over the fields kept from before. Once nothing is missing there is
        nothing to continue, whether or not the closing brace made it.
        """
        cut = _cut_off(resp)
        if cut:
            got = _finished_fields(resp.text)
        else:
            got = json_object(resp.text)
            if got is None:
                got = {} if partial else self._parse(resp.text)
        raw = {**partial, **got} if partial else got
        if cut and not raw:
            # Cut inside the very first value: nothing arrived whole to keep.
            return self._parse(resp.text), {}, cut
        waiting = raw if cut and self._missing(raw) else {}
        return raw, waiting, cut

    def _complexity(self, ctx: AgentContext) -> str:
        return "high" if ctx.escalate else self.complexity

    # ── editing a deliverable (#79) ─────────────────────────────────────────
    def _schema_for(self, ctx: AgentContext) -> dict:
        """The decoding constraint: the shape, plus `deleted` when the agent is editing —
        a constrained decoder can't name a removed file in a key the schema lacks."""
        schema = self.response_schema()
        if not ctx.editing:
            return schema
        props = dict(schema.get("properties") or {})
        props.setdefault("deleted", {"type": "array", "items": {"type": "string"}})
        return {**schema, "properties": props}

    def merge_edit(self, base: dict, raw: object) -> dict:
        """An edit laid over what was there: files by path (`deleted` removes them),
        every other field replaced only when the reply says something for it."""
        return merge_edit(base, raw)

    def _task_for(self, ctx: AgentContext) -> str:
        return self.edit_task_text() if ctx.editing else self.task_text()

    def edit_task_text(self) -> str:
        """The ask when this agent is changing what it delivered (#79)."""
        return f"{_EDIT_TASK}\n\n{self.task_text()}"

    def _check(self, raw: dict, ctx: AgentContext) -> tuple[dict, list[str]]:
        """Everything wrong with one attempt: its shape, and its stack.

        Both go into the same list because both get the same treatment — one repair
        round with the problems named — and because they are genuinely one question
        to the model: *this is not the deliverable that was asked for, here is why.*
        Running the charter check as a separate pass afterwards would mean an agent
        that wrote Mongoose under a Postgres charter is only ever told so by a
        security review three phases later, which is where this started.

        An edit (#79) is checked as the deliverable it makes: what came back, laid over
        what was there.
        """
        if ctx.editing:
            raw = self.merge_edit(ctx.base_output or {}, raw)
        output, errors = self._validate(raw)
        if not errors:
            output, own = self.own_checks(output, ctx)
            errors = errors + own
        if settings.enforce_stack_charter:
            errors = errors + charter_violations(ctx.charter, self.key, output)
        build = self._build_check(ctx, output)
        if build is not None:
            errors = errors + build.messages()
        return output, errors

    def _build_check(self, ctx: AgentContext, output: dict) -> Optional[BuildCheck]:
        """Compile what this phase wrote, in the build it is joining. None if not code.

        Best effort by construction: a checker that crashes must not fail a phase that
        may well be correct, so a failure here is logged and the phase is simply not
        checked — which the status then says, rather than claiming it passed.
        """
        self._carry_theme(ctx, output)
        if not settings.enforce_build_check or self.key not in CODE_PHASES:
            return None
        try:
            return check_phase(ctx.prior_outputs, self.key, output, ctx.charter)
        except Exception as e:  # noqa: BLE001 - the gate must not become the failure
            log.warning("%s: the compile check could not run: %s", self.title, e)
            return BuildCheck(status=BuildStatus.UNCHECKED.value, reason=f"The compile check could not run: {e}")

    def _carry_theme(self, ctx: AgentContext, output: dict) -> None:
        """A frontend the crew (re)writes keeps the site style the person chose on the
        app preview (#78) — before it is checked and built, so the build that passes
        is of the Tailwind config that ships. A change made on the preview itself says
        its own style (an undo may be taking one away), so it is left alone."""
        if self.key != Phase.FRONTEND_ENGINEER.value or ctx.revising or not isinstance(output, dict):
            return
        if "app_theme" in output:
            return
        build = inflight.current() or {}
        if not build.get("id"):
            return
        from app.db.base import SessionLocal
        from app.db.models import Project
        from app.preview import app_state

        try:
            with SessionLocal() as db:
                project = db.get(Project, build["id"])
                theme = app_state.theme(project) if project is not None else None
        except Exception:  # noqa: BLE001 - a style is never worth failing a phase over
            log.exception("Couldn't read the chosen site style")
            return
        if theme:
            output["app_theme"] = theme

    def _run_build(
        self, ctx: AgentContext, output: dict, build: Optional[BuildCheck], reuse: Optional[dict] = None
    ) -> Optional[dict]:
        """Install, build and start what this phase wrote, once it parses (#75).

        Only after the parser passes: a file that doesn't parse can't build, and the
        parser names it faster and more precisely. What the real build finds is
        merged into `build` — a failed build fails the phase exactly as a parse error
        does, and goes to the fix loop the same way. A runner that can't run (no
        Docker, no builder service) leaves `build` as the parser left it.
        """
        side = _BUILT_SIDES.get(self.key)
        if (
            side is None
            or build is None
            or not settings.enforce_build_check
            or not settings.build_run_enabled
            or build.status == BuildStatus.FAILED.value
        ):
            return None
        try:
            files, _ = phase_tree(ctx.prior_outputs, self.key, output, ctx.charter)
            if (
                isinstance(reuse, dict)
                and reuse.get("fingerprint")
                # Only a verdict: a build that never finished (registry down, out of
                # time, stopped) says nothing, and is run again.
                and reuse.get("status") in (BuildStatus.OK.value, BuildStatus.FAILED.value)
                and reuse["fingerprint"] == build_runner.fingerprint(build_runner.side_files(files, side))
            ):
                run = build_runner.BuildRun.from_dict(reuse)
            else:
                run = build_runner.run_build(files, side)
        except (RequestCancelled, Superseded):
            raise
        except Exception as e:  # noqa: BLE001 - the runner must not become the failure
            log.warning("%s: the build couldn't run: %s", self.title, e)
            return build_runner.BuildRun.unchecked(side, f"The build couldn't run: {e}").as_dict()
        if run.status == BuildStatus.FAILED.value:
            # The compile gate's caps on the two together: five a file, twenty-four in all.
            build.problems = buildlog.capped(build.problems + run.problems)
            build.status = BuildStatus.FAILED.value
        elif (
            run.status == BuildStatus.OK.value
            and build.status == BuildStatus.UNCHECKED.value
            and all(p.startswith(f"{side}/") for p in build.unchecked)
        ):
            # No parser could read these files here, but they installed and built:
            # that is a stronger check than the one that couldn't run.
            build.status = BuildStatus.OK.value
            build.unchecked = []
            build.reason = None
        return run.as_dict()

    def _complete(
        self, messages: list[ChatMessage], ctx: AgentContext, options: GenerationOptions
    ) -> LLMResponse:
        # Named for the computer answering it: "Backend Engineer for build 'Todo app'".
        with inflight.agent(self.title):
            return router.complete(
                messages,
                mode=ctx.routing_mode,
                preferred_model=ctx.preferred_model,
                options=options,
                complexity=self._complexity(ctx),
                role=self.key,
                pin=ctx.pin_model,
            )

    # ── prompt construction ─────────────────────────────────────────────────
    def _registry(self, ctx: AgentContext) -> handoff.Registry:
        """The names System Design fixed, for every phase after it. Empty before."""
        order = [p.value for p in PHASE_ORDER]
        design = order.index(Phase.SYSTEM_DESIGN.value)
        if self.key not in order or order.index(self.key) <= design:
            return handoff.Registry()
        return handoff.registry(ctx.prior_outputs)

    def _contract(self, ctx: AgentContext) -> Optional[str]:
        if not settings.enforce_build_check:
            return None
        return build_contract.prompt_block(self.key, ctx.charter)

    def system_prompt(
        self, charter: Optional[Charter] = None, registry: Optional[handoff.Registry] = None
    ) -> str:
        """The agent's standing instructions, including the stack it is held to.

        The charter belongs *here* rather than in the user turn, and verbatim rather
        than summarised. Everything in the user turn is sized against a budget and
        can be cut to fit; the one instruction that must survive a small window is
        the one saying which database the rest of the team is building against.
        """
        return (
            self._standing(charter, registry, _HANDOFF_NOTE if self.depends_on else "")
            + "Respond with ONLY a single valid JSON object — no prose, no markdown fences — "
            f"matching this shape:\n{self.output_spec}"
        )

    def _standing(
        self, charter: Optional[Charter], registry: Optional[handoff.Registry], note: str
    ) -> str:
        """Everything in the system prompt above the answer format: who this agent is,
        how hand-offs read (`note`), the charter, the registry, the platform contract."""
        charter_block = charter.prompt_block() if charter else ""
        # Where files go, which files are the platform's, and which packages exist —
        # standing instructions for the phases that write code, for the same reason
        # the charter is: they cannot be trimmed away to fit a small window.
        platform_block = (
            build_contract.prompt_block(self.key, charter) if settings.enforce_build_check else None
        )
        # The names the rest of the team is building against. Here for the charter's
        # reason: a vocabulary trimmed away to fit a small window is one the phase
        # cannot be held to, and the compile gate holds it to it.
        registry_block = registry.prompt_block() if registry else ""
        brief = self.standing_brief(charter)
        return (
            f"You are the {self.title} on an AI software engineering team. {self.role}\n\n"
            + note
            + (f"{charter_block}\n\n" if charter_block else "")
            + (f"{registry_block}\n\n" if registry_block else "")
            + (f"{platform_block}\n\n" if platform_block else "")
            + (f"{brief}\n\n" if brief else "")
        )


    def _build_messages(
        self,
        ctx: AgentContext,
        profile: Optional[ModelProfile] = None,
        reserve: int = 0,
    ) -> Prompt:
        if profile is None:
            profile = router.profile_for(
                ctx.routing_mode, ctx.preferred_model, complexity=self._complexity(ctx), role=self.key, pin=ctx.pin_model
            )
        bodies = self._prepare_deps(ctx, profile)
        budget = self._section_budgets(ctx, profile, reserve, bodies)
        used: list[str] = []
        seen: list[dict] = []
        return Prompt(
            messages=[
                ChatMessage(role="system", content=self.system_prompt(ctx.charter, self._registry(ctx))),
                ChatMessage(role="user", content=self._user_turn(ctx, budget, bodies, used, seen)),
            ],
            skills_used=used,
            deps=seen,
        )

    def _prepare_deps(self, ctx: AgentContext, profile: Optional[ModelProfile]) -> _Deps:
        """Every dependency's digest, fitted to its share of the window, and its want.

        Fitted against the window rather than printed whatever its size: the digests
        are always printed and so are part of the measured overhead, and on a 4K
        window three uncapped ones would be the instructions' room gone.
        """
        deps = [d for d in self.depends_on if d in ctx.prior_outputs]
        budget = profile.prompt_char_budget if profile is not None else 0
        limit = max(int(budget * settings.handoff_digest_share) // max(len(deps), 1), 300)
        out = _Deps()
        for dep in deps:
            output = ctx.prior_outputs[dep]
            out.digests[dep] = handoff.fit(handoff.digest(dep, output), limit)
            out.wants[dep] = handoff.full_cost(output) if isinstance(output, dict) else 0
        return out

    def _user_turn(
        self,
        ctx: AgentContext,
        budget: dict[str, int],
        bodies: Optional[_Deps] = None,
        skills_used: Optional[list[str]] = None,
        deps_seen: Optional[list[dict]] = None,
    ) -> str:
        """Everything the agent is given, with every section held to its budget.

        `bodies` is the serialised prior-phase context, passed in so that measuring
        this prompt does not mean `json.dumps`-ing several hundred kilobytes of
        generated source a second time purely to count its characters.

        `skills_used` is filled in with the skills that fit. It is an out-parameter
        because this method runs twice per prompt — once at zero to measure the
        overhead, once for real — and only the second run is the truth about what
        the model was given.
        """
        if bodies is None:
            bodies = _Deps()
        parts: list[str] = [
            f"# Product idea\n{_clip(ctx.idea, budget['idea'])}\n"
        ]

        # Every dependency is one JSON object that parses: its digest, always, and as
        # much of its output as its share holds, cut between fields and never inside
        # one. A small dependency's unused share goes to the larger ones.
        deps = [d for d in self.depends_on if d in ctx.prior_outputs]
        rooms = handoff.share({d: bodies.wants.get(d, 0) for d in deps}, budget["depends_on"])
        for dep in deps:
            shown = handoff.render(
                dep, ctx.prior_outputs[dep], rooms.get(dep, 0), bodies.digests.get(dep)
            )
            parts.append(shown.text)
            if deps_seen is not None:
                deps_seen.append(shown.record())

        # Before the reference material, because a procedure is how to do the work
        # and reference material is what the work is about — and because a small
        # model weights what it reads first most heavily.
        if ctx.skills:
            body, used = _pack_skills(
                ctx.skills, budget.get("skills", 0) - _SKILLS_FRAME_COST
            )
            # Nothing fitted, so nothing is printed. A heading with no procedure
            # under it is worse than silence on a small model: the next section
            # starts immediately, and "follow these procedures" ends up pointing at
            # the knowledge base. (`_section_budgets` says so in the log; it is the
            # one that knows how much room there was.)
            if body:
                parts.append(_SKILLS_FRAME.format(body=body))
            if skills_used is not None:
                skills_used[:] = used

        if ctx.rag_context:
            parts.append(_RAG_FRAME.format(body=_clip(ctx.rag_context, budget["rag"])))
        if ctx.memory_context:
            parts.append(_MEMORY_FRAME.format(body=_clip(ctx.memory_context, budget["memory"])))
        if ctx.scan_context:
            parts.append(f"{_clip(ctx.scan_context, budget['scan'])}\n")
        if ctx.extra_context:
            parts.append(
                f"# Team decision to honour\n{_clip(ctx.extra_context, budget['extra'])}\n"
            )
        if ctx.editing:
            # Last before the ask, where a small model weights it most: what is there
            # now, then what to change about it (#79).
            parts.append(
                _CURRENT_FRAME.format(
                    body=current_text(ctx.base_output or {}, budget.get("current", 0), ctx.likely_files)
                )
            )
            parts.append(_CHANGE_FRAME.format(body=_clip(ctx.change, budget.get("change", 0))))
        if ctx.feedback:
            parts.append(
                "# Reviewer feedback on your previous attempt — address it directly\n"
                f"{_clip(ctx.feedback, budget['feedback'])}\n"
            )

        parts.append(self._task_for(ctx))
        return "\n".join(parts)

    def _section_budgets(
        self,
        ctx: AgentContext,
        profile: Optional[ModelProfile],
        reserve: int = 0,
        bodies: Optional[_Deps] = None,
    ) -> dict[str, int]:
        """How many characters each section may spend.

        The overhead is *measured*, not estimated: the same assembly runs once with
        every section at zero, which yields the exact cost of the headings, the code
        fences, the truncation markers and the joins around them. Estimating that is
        how a prompt sized to fill the window ends up thirty characters past it, and
        past it is where a runtime truncates from the head — taking the system prompt,
        and the shape it carries, first.

        What a person wrote is served first and in full, capped at a share each: the
        idea and the reviewer's note have no maximum length at the API, so leaving
        them uncut leaves the window unbounded however carefully the rest is sized.
        Whatever survives that is shared between the assembled sections, renormalised
        over the ones that have content — a quarter of the window held back for a
        knowledge base nobody uploaded is a quarter spent on nothing.
        """
        if profile is None:
            profile = router.profile_for(
                ctx.routing_mode, ctx.preferred_model, complexity=self._complexity(ctx), role=self.key, pin=ctx.pin_model
            )

        empty = dict.fromkeys(list(_PERSON_SHARE) + list(_CONTEXT_SHARE), 0)
        # The charter is part of the overhead, not part of the context: it is printed
        # in full or the phase is not really bound by it, so it is counted here at its
        # real cost rather than being sized like a section that can be trimmed.
        if bodies is None:
            bodies = self._prepare_deps(ctx, profile)
        overhead = len(self.system_prompt(ctx.charter, self._registry(ctx))) + len(
            self._user_turn(ctx, empty, bodies)
        )
        if overhead > profile.prompt_char_budget:
            # Nothing can be trimmed to fix this: the overhead *is* the instructions
            # and the shape they carry. Said out loud, because the alternative is the
            # original bug — a prompt silently cut from the head, and an agent
            # generating against a shape it was never shown.
            log.error(
                "%s cannot fit its own instructions in %s's %s-token window "
                "(needs ~%s characters, has %s). Raise the window or use a model with "
                "a larger one; this phase will be truncated.",
                self.title,
                profile.model,
                f"{profile.context_window:,}",
                f"{overhead:,}",
                f"{profile.prompt_char_budget:,}",
            )
        free = max(profile.prompt_char_budget - overhead - max(reserve, 0), 0)

        # What a person wrote is served first: what it needs, never more than its
        # share *of what is actually free*. Taking the share off the whole budget
        # instead double-counts the overhead, and on a small window that alone puts
        # the prompt back over the top.
        written = {
            "idea": ctx.idea, "feedback": ctx.feedback or "", "extra": ctx.extra_context, "scan": ctx.scan_context,
            "change": ctx.change if ctx.editing else "",
        }
        budget = {
            name: min(len(written[name]), int(free * share))
            for name, share in _PERSON_SHARE.items()
        }
        free = max(free - sum(budget.values()), 0)

        present = {
            "depends_on": any(d in ctx.prior_outputs for d in self.depends_on),
            "rag": bool(ctx.rag_context),
            "skills": bool(ctx.skills),
            "memory": bool(ctx.memory_context),
            "current": ctx.editing,
        }
        share_total = sum(_CONTEXT_SHARE[n] for n, has in present.items() if has)

        # Skills are sized first, and charged at what they *actually* cost.
        #
        # Every other section here spends whatever it is given: a budget of 4,000
        # characters of reference material means 4,000 characters of reference
        # material. Skills cannot — a procedure is injected whole or not at all, so
        # a share that cannot hold the smallest candidate buys nothing. Reserving it
        # anyway is how a build with skills switched on ends up *strictly worse* than
        # the same build with them off: no procedures, and a fifth less room for the
        # knowledge base than the run that never asked for any.
        #
        # So: offer them their share, see what fits, keep only that, and hand the
        # rest back to the sections that can spend it.
        budget["skills"] = 0
        if present["skills"] and share_total:
            offered = int(free * (_CONTEXT_SHARE["skills"] / share_total))
            body, _ = _pack_skills(ctx.skills, offered - _SKILLS_FRAME_COST)
            if body:
                budget["skills"] = _SKILLS_FRAME_COST + len(body)
            else:
                smallest = min(len(render_skill(c.skill)) for c in ctx.skills)
                log.info(
                    "%s: %d skill(s) matched, but the smallest needs %s characters "
                    "and this model's share is %s. This phase gets none, and the room "
                    "goes to the context that can use it.",
                    self.title,
                    len(ctx.skills),
                    f"{smallest:,}",
                    f"{max(offered - _SKILLS_FRAME_COST, 0):,}",
                )
            free -= budget["skills"]
            share_total -= _CONTEXT_SHARE["skills"]

        for name, share in _CONTEXT_SHARE.items():
            if name == "skills":
                continue
            budget[name] = int(free * (share / share_total)) if present[name] and share_total else 0
        return budget

    def standing_brief(self, charter: Optional[Charter] = None) -> str:
        """Facts this agent must work from that cannot be trimmed to fit — a pricing
        table, whether GitHub is connected. In the system prompt, beside the charter."""
        return ""

    def own_checks(self, output: dict, ctx: AgentContext) -> tuple[dict, list[str]]:
        """What this agent's output must also satisfy, beyond its declared shape.
        Returns the output to keep (possibly narrowed) and what is wrong with it."""
        return output, []

    def task_instruction(self) -> str:
        """The concrete ask for this phase. Override per agent."""
        return "Produce your deliverable as the JSON object described above."

    def task_text(self) -> str:
        """The ask actually sent: an eval's prompt variant when one is set, else the
        agent's own. The harness compares two task texts on the same ideas this way."""
        return TASK_OVERRIDES.get(self.key) or self.task_instruction()

    def _repair_messages(
        self,
        ctx: AgentContext,
        profile: ModelProfile,
        attempt: str,
        errors: list[str],
    ) -> Prompt:
        """The same ask, plus what was wrong with the answer — inside the same window.

        The rejected attempt is echoed back so the model corrects rather than starts
        over, because the thing being repaired is sometimes a wall of generated code.
        That echo is reserved *before* the context sections are sized, so the whole
        exchange still fits: a repair prompt that overflows gets cut from the head,
        and the head is the system prompt naming the shape being repaired.
        """
        problems = "\n".join(f"- {e}" for e in errors[:12])
        instruction = (
            "That response is not the deliverable that was asked for. Fix exactly "
            f"these problems:\n{problems}\n\n"
            "Return the COMPLETE JSON object again — every key from the shape, "
            "not a patch and not an apology. Keep everything that was already correct."
            + (
                " File lists still hold only the files you add or change."
                if ctx.editing
                else ""
            )
        )
        echo_budget = max(profile.prompt_char_budget // 4, 1000)
        echo = _clip(attempt, echo_budget)
        ask = self._build_messages(ctx, profile, reserve=len(echo) + len(instruction))
        return Prompt(
            messages=[
                *ask.messages,
                ChatMessage(role="assistant", content=echo),
                ChatMessage(role="user", content=instruction),
            ],
            skills_used=ask.skills_used,
        )

    def _continue_messages(
        self,
        ctx: AgentContext,
        profile: ModelProfile,
        partial: dict,
        missing: list[str],
    ) -> Prompt:
        """The reply was cut off at the output limit: ask for the missing keys only.

        Echoing the cut attempt back and asking for "the COMPLETE object again" asks
        for the same reply under the same output cap that cut it. The finished fields
        are kept, named, and not sent again.
        """
        instruction = (
            "Your reply was cut off at this model's output limit. These keys arrived "
            f"complete and are kept: {', '.join(partial) or 'none'}.\n"
            f"Return a JSON object with ONLY the remaining keys: {', '.join(missing)}. "
            "Keep it shorter than before — fewer, smaller items — so it fits."
        )
        ask = self._build_messages(ctx, profile, reserve=len(instruction))
        return Prompt(
            messages=[*ask.messages, ChatMessage(role="user", content=instruction)],
            skills_used=ask.skills_used,
            deps=ask.deps,
        )

    # ── output handling ───────────────────────────────────────────────────────
    def _validate(self, raw: dict) -> tuple[dict, list[str]]:
        """Check a parsed response against the declared shape.

        Returns the output to persist and what (if anything) is wrong with it. On
        success the output comes back out through the model, so the canonical key
        names are what gets stored — a response that said `overallRiskAssessment` is
        saved as `risk_assessment`, and the gate that reads it finds it.
        """
        try:
            validated = self.output_model.model_validate(raw)
        except ValidationError as e:
            return raw, _error_lines(e)
        return validated.model_dump(mode="json"), []

    @staticmethod
    def _parse(text: str) -> dict:
        """The JSON object in a reply, or the raw text wrapped as a summary."""
        parsed = json_object(text)
        if parsed is None:
            log.warning("Agent returned non-JSON output; wrapping raw text as summary.")
            return {"summary": text.strip()}
        return parsed

    def to_markdown(self, output: dict) -> str:
        """Generic renderer; agents may override for nicer formatting."""
        lines: list[str] = [f"## {self.title}\n"]
        lines.append(_render_value(output))
        return "\n".join(lines)


#: How every dependency frame reads, said once in the system prompt.
_HANDOFF_NOTE = (
    "Earlier phases hand over {digest, output}; output may be cut (_cut). Trust the digest.\n\n"
)

#: Set by the eval harness's `--prompt-variant`: phase key -> task text to send
#: instead of the agent's own. Empty in a running server.
TASK_OVERRIDES: dict[str, str] = {}

#: What a reply cut off at the output limit is recorded as, ahead of any shape error.
_TRUNCATED_ERROR = "the reply was cut off at the model's output limit (truncated)"


def _cut_off(resp: LLMResponse) -> bool:
    return (getattr(resp, "finish_reason", None) or "") == "length"


def _finished_fields(text: str) -> dict:
    """The top-level fields of a JSON object that arrived whole before the reply was cut.

    Reads key by key with the standard decoder and stops at the first value that does
    not parse — the one the output limit cut through. Everything before it is exactly
    what the model wrote.
    """
    text = (text or "").strip()
    start = text.find("{")
    if start < 0:
        return {}
    decoder = json.JSONDecoder()
    out: dict = {}
    i = start + 1
    n = len(text)
    while i < n:
        while i < n and text[i] in " \t\r\n,":
            i += 1
        if i >= n or text[i] == "}":
            break
        try:
            key, i = decoder.raw_decode(text, i)
        except ValueError:
            break
        if not isinstance(key, str):
            break
        while i < n and text[i] in " \t\r\n":
            i += 1
        if i >= n or text[i] != ":":
            break
        i += 1
        while i < n and text[i] in " \t\r\n":
            i += 1
        try:
            value, i = decoder.raw_decode(text, i)
        except ValueError:
            break
        out[key] = value
    return out


#: A cut the reader can see. A silent one reads as a model that simply stopped.
#: Deliberately carries no number: the skeleton measured to size the budget has to
#: cost exactly what the finished prompt costs, and "55,344" is six characters longer
#: than "0" — which is the whole overshoot, five sections over.
_TRUNCATED = "… [cut here to fit this model's context window]\n"


def _pack_skills(skills: tuple[Selected, ...], limit: int) -> tuple[str, list[str]]:
    """As many whole skills as fit, in the order they were selected.

    Whole ones only. A procedure cut off mid-step is a procedure with its last
    instruction missing, and nothing downstream — not the agent, not the reviewer —
    can tell that from a procedure that simply ends there. Everything else in this
    prompt is truncatable prose or data; a skill is not.

    A skill that does not fit is skipped rather than ending the pack, so a short
    lower-ranked procedure still reaches the model when a long higher-ranked one
    could not. The order of what *is* taken never changes, so the same build on the
    same model gets the same block twice running.
    """
    blocks: list[str] = []
    used: list[str] = []
    spent = 0
    for chosen in skills:
        block = render_skill(chosen.skill)
        cost = len(block) + (2 if blocks else 0)  # the "\n\n" this join will add
        if spent + cost > limit:
            continue
        blocks.append(block)
        used.append(chosen.skill.name)
        spent += cost
    return "\n\n".join(blocks), used


def _file_lists(output: dict) -> list[str]:
    """The keys of `output` that hold lists of files (`files`, `test_files`, …)."""
    keys = []
    for key, value in (output or {}).items():
        if isinstance(value, list) and value and all(isinstance(v, dict) and "path" in v for v in value):
            keys.append(key)
    return keys


def merge_edit(base: dict, raw: object) -> dict:
    """What an edit (#79) makes of `base`: each list of files merged by path — a file
    that came back replaces the one at its path, a new path is added, a path in `deleted`
    goes — and every other field replaced only when the reply has something for it.
    Files the reply doesn't mention are kept byte for byte."""
    from copy import deepcopy

    reply = raw if isinstance(raw, dict) else {}
    merged = deepcopy(base or {})
    gone = {str(p).strip().lstrip("/") for p in reply.get("deleted") or [] if isinstance(p, str)}
    lists = set(_file_lists(merged)) | set(_file_lists(reply))
    for key, value in reply.items():
        if key == "deleted" or key in lists:
            continue
        if has_content(value):
            merged[key] = value
    for key in lists:
        current = [dict(f) for f in merged.get(key) or [] if isinstance(f, dict)]
        index = {str(f.get("path") or "").strip().lstrip("/"): i for i, f in enumerate(current)}
        for item in reply.get(key) or []:
            if not isinstance(item, dict) or not isinstance(item.get("path"), str):
                continue
            path = item["path"].strip().lstrip("/")
            if path in index:
                old = current[index[path]]
                if "code" in item or "content" in item:
                    # The new text wins whichever key it came under: readers take
                    # `code` before `content`, so a stale `code` would shadow it.
                    old = {k: v for k, v in old.items() if k not in ("code", "content")}
                current[index[path]] = {**old, **item}
            else:
                index[path] = len(current)
                current.append(dict(item))
        merged[key] = [f for f in current if str(f.get("path") or "").strip().lstrip("/") not in gone]
    return merged


def current_text(base: dict, limit: int, likely: tuple = ()) -> str:
    """The deliverable a change edits, as the prompt shows it (#79), cut to `limit`.

    The file list first — every path with what it is for, which is the map of the app —
    then the whole code of the files the change is likely to touch, then the rest of the
    deliverable's fields. A file not shown whole is still on the list, by path."""
    lists = _file_lists(base)
    parts: list[str] = []
    files = [f for key in lists for f in base.get(key) or [] if isinstance(f, dict)]
    if files:
        lines = ["Files (kept exactly as they are unless you return them):"]
        for f in files:
            purpose = str(f.get("purpose") or f.get("targets") or "").strip()
            lines.append(f"- `{f.get('path')}`" + (f" — {purpose[:120]}" if purpose else ""))
        parts.append("\n".join(lines))
        wanted = [str(p).strip().lstrip("/") for p in likely if str(p).strip()]
        for f in files:
            path = str(f.get("path") or "").strip().lstrip("/")
            if not any(path == w or path.endswith("/" + w) or w.endswith("/" + path) for w in wanted):
                continue
            code = f.get("code") if isinstance(f.get("code"), str) else str(f.get("content") or "")
            parts.append(f"### {path}\n```\n{code}{'' if code.endswith(chr(10)) else chr(10)}```")
    rest = {k: v for k, v in (base or {}).items() if k not in lists}
    if rest:
        parts.append("```json\n" + json.dumps(rest, ensure_ascii=False, default=str) + "\n```")
    return _clip("\n\n".join(parts), limit)


def _clip(text: str, limit: int) -> str:
    """Cut to `limit` characters, saying so.

    A limit of zero means there is no room left, so nothing of the text survives —
    returning all of it, which the inverted guard here used to do, overflows exactly
    the window this budget exists to respect. Empty text is returned untouched: a
    "this was cut" marker under an empty idea tells the model its brief was
    truncated when there was never anything there.
    """
    if not text:
        return text
    if limit > 0 and len(text) <= limit:
        return text
    # One shape for every cut, zero-length included: the skeleton this budget was
    # measured from and the prompt finally sent have to cost the same, and a "\n"
    # present in one and absent in the other is a one-character overrun.
    return text[: max(limit, 0)] + "\n" + _TRUNCATED


def _error_lines(error: ValidationError) -> list[str]:
    """Validation errors as lines a model (and a person) can act on."""
    lines: list[str] = []
    for item in error.errors():
        where = ".".join(str(p) for p in item.get("loc", ())) or "(root)"
        lines.append(f"`{where}` — {item.get('msg', 'is invalid')}")
    return lines


def _merge(responses: list[LLMResponse]) -> LLMResponse:
    """One response standing for the whole exchange, repair rounds included.

    The text is the attempt that was kept; the tokens and the wall-clock are every
    call it took to get there, so a repaired phase reports what it actually cost.
    """
    last = responses[-1]
    if len(responses) == 1:
        return last
    return last.model_copy(
        update={
            "usage": Usage(
                prompt_tokens=sum(r.usage.prompt_tokens for r in responses),
                completion_tokens=sum(r.usage.completion_tokens for r in responses),
                total_tokens=sum(r.usage.total_tokens for r in responses),
            ),
            "latency_ms": sum(r.latency_ms for r in responses),
            "fallback_used": any(r.fallback_used for r in responses),
            "attempts": [a for r in responses for a in r.attempts],
        }
    )


def _render_value(value, depth: int = 0) -> str:
    """Render a parsed JSON value as readable markdown."""
    pad = "  " * depth
    if isinstance(value, dict):
        out = []
        for k, v in value.items():
            heading = str(k).replace("_", " ").title()
            if isinstance(v, (dict, list)):
                out.append(f"{pad}**{heading}:**")
                out.append(_render_value(v, depth + 1))
            else:
                out.append(f"{pad}- **{heading}:** {v}")
        return "\n".join(out)
    if isinstance(value, list):
        out = []
        for item in value:
            if isinstance(item, (dict, list)):
                out.append(_render_value(item, depth + 1))
                out.append("")
            else:
                out.append(f"{pad}- {item}")
        return "\n".join(out)
    return f"{pad}{value}"
