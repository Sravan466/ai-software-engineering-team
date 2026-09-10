"""Base agent: prompt assembly, routing, schema-checked parsing, markdown rendering.

Each specialist subclass declares its role (system prompt), the *type* its deliverable
must have, and which upstream phase outputs it depends on. The base class handles
everything else: sizing the prompt to the model actually chosen, calling the router with
the schema attached, checking what came back against that same type, sending one repair
round when it misses, and turning the result into display markdown.

The sizing matters as much as the schema. Every truncation length here used to be a
literal — 6000 characters of prior-phase context, 4000 of reference material — chosen
against no particular model. Combined with a provider that never set a context window,
that put the later phases over the limit, and Ollama truncates from the head: the system
prompt, and with it the required shape, went first. Both halves are now derived from the
window the model reports.
"""
from __future__ import annotations
from typing import Optional

import json
from dataclasses import dataclass, field

from pydantic import BaseModel, ValidationError

from app.core.config import settings
from app.core.constants import RoutingMode, SchemaStatus
from app.core.logging import get_logger
from app.core.reading import json_object
from app.router.model_profile import ModelProfile
from app.router.router import router
from app.schemas.agent_outputs import GenericOutput, response_schema, shape_text
from app.schemas.llm import ChatMessage, GenerationOptions, LLMResponse, Usage

log = get_logger(__name__)

#: What a person wrote gets whatever it needs, up to this share of the budget each.
#: These are normally a sentence or two, so the cap almost never binds — but `idea`
#: and `feedback` have no maximum length at the API, and an unbounded section is one
#: that overruns the window however carefully the rest is measured.
_PERSON_SHARE = {
    "idea": 0.25,
    "feedback": 0.15,
    "extra": 0.10,
}
#: How whatever remains is divided between the sections the pipeline assembles.
_CONTEXT_SHARE = {
    "depends_on": 0.62,
    "rag": 0.26,
    "memory": 0.12,
}
#: The text wrapping each optional section. Written once and used twice — to build
#: the section, and to charge its cost against the budget — because a frame that is
#: estimated on one side and printed on the other is a frame the prompt overruns by
#: however far the estimate was off.
_DEP_FRAME = "# Context — {dep} output\n```json\n{body}\n```\n"
_RAG_FRAME = "# Reference material (from the uploaded knowledge base)\n{body}\n"
_MEMORY_FRAME = "# Lessons from past projects (long-term memory)\n{body}\n"


@dataclass
class AgentContext:
    """Everything an agent needs to do its job for one project/phase."""

    idea: str
    routing_mode: RoutingMode = RoutingMode.LOCAL_ONLY
    preferred_model: Optional[str] = None
    prior_outputs: dict[str, dict] = field(default_factory=dict)  # phase -> structured output
    rag_context: str = ""
    memory_context: str = ""
    feedback: Optional[str] = None  # human guidance when a phase is re-run after rejection
    extra_context: str = ""  # e.g. an agent-debate decision injected before a phase


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
        profile = router.profile_for(
            ctx.routing_mode, ctx.preferred_model, complexity=self.complexity
        )
        ask = self._build_messages(ctx, profile)
        options = GenerationOptions(json_mode=True, json_schema=self.response_schema())

        resp = self._complete(ask, ctx, options)
        responses = [resp]
        output, errors = self._validate(self._parse(resp.text))
        best = (output, errors)

        rounds = 0
        while errors and rounds < max(settings.schema_repair_rounds, 0):
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
            resp = self._complete(
                self._repair_messages(ctx, profile, resp.text, errors), ctx, options
            )
            responses.append(resp)
            output, errors = self._validate(self._parse(resp.text))
            # Fewer things wrong wins. Without this the *last* attempt is kept
            # whatever it looks like, so a repair that came back worse than the
            # response it was repairing is what reaches the database and the gates.
            if len(errors) < len(best[1]):
                best = (output, errors)

        output, errors = best
        if errors:
            # Repair stops paying after a round or two, and a local model pays
            # wall-clock for every attempt. Keep the best try — and say so, because
            # the cost and security gates downstream read these keys, and a gate that
            # cannot find them is a gate that silently stops gating.
            log.error(
                "%s output still does not match its shape after %d repair round(s): %s",
                self.title,
                rounds,
                "; ".join(errors[:3]),
            )

        status = (
            SchemaStatus.INVALID.value
            if errors
            else (SchemaStatus.REPAIRED.value if rounds else SchemaStatus.VALID.value)
        )
        return AgentResult(
            output=output,
            content_md=self.to_markdown(output),
            response=_merge(responses),
            schema_status=status,
            # The model gets the backticked form in the repair prompt, where they
            # delimit a field name. A person reads this in a sentence on screen.
            schema_note="; ".join(e.replace("`", "") for e in errors[:3]) or None,
            repair_rounds=rounds,
            calls=responses,
        )

    def _complete(
        self, messages: list[ChatMessage], ctx: AgentContext, options: GenerationOptions
    ) -> LLMResponse:
        return router.complete(
            messages,
            mode=ctx.routing_mode,
            preferred_model=ctx.preferred_model,
            options=options,
            complexity=self.complexity,
        )

    # ── prompt construction ─────────────────────────────────────────────────
    def system_prompt(self) -> str:
        return (
            f"You are the {self.title} on an AI software engineering team. {self.role}\n\n"
            "You collaborate with other specialist agents; your output is consumed by the "
            "next agent in the pipeline, so be precise, concrete, and complete.\n\n"
            "Respond with ONLY a single valid JSON object — no prose, no markdown fences — "
            f"matching this shape:\n{self.output_spec}"
        )

    def _build_messages(
        self,
        ctx: AgentContext,
        profile: Optional[ModelProfile] = None,
        reserve: int = 0,
    ) -> list[ChatMessage]:
        if profile is None:
            profile = router.profile_for(
                ctx.routing_mode, ctx.preferred_model, complexity=self.complexity
            )
        # Serialised once: the budget pass and the real assembly both read these,
        # and they can be hundreds of kilobytes of generated source apiece.
        bodies = {
            dep: json.dumps(ctx.prior_outputs[dep], indent=2)
            for dep in self.depends_on
            if dep in ctx.prior_outputs
        }
        budget = self._section_budgets(ctx, profile, reserve, bodies)
        return [
            ChatMessage(role="system", content=self.system_prompt()),
            ChatMessage(role="user", content=self._user_turn(ctx, budget, bodies)),
        ]

    def _user_turn(
        self,
        ctx: AgentContext,
        budget: dict[str, int],
        bodies: Optional[dict[str, str]] = None,
    ) -> str:
        """Everything the agent is given, with every section held to its budget.

        `bodies` is the serialised prior-phase context, passed in so that measuring
        this prompt does not mean `json.dumps`-ing several hundred kilobytes of
        generated source a second time purely to count its characters.
        """
        if bodies is None:
            bodies = {}
        parts: list[str] = [
            f"# Product idea\n{_clip(ctx.idea, budget['idea'])}\n"
        ]

        deps = [d for d in self.depends_on if d in ctx.prior_outputs]
        per_dep = budget["depends_on"] // max(len(deps), 1)
        for dep in deps:
            body = bodies.get(dep, "")
            # The "this was cut" note goes outside the fence: inside it, the block
            # the next agent is reading as JSON would no longer parse as any.
            clipped = body[:per_dep] if len(body) > per_dep else body
            note = "" if len(clipped) == len(body) else _TRUNCATED
            parts.append(_DEP_FRAME.format(dep=dep, body=clipped) + note)

        if ctx.rag_context:
            parts.append(_RAG_FRAME.format(body=_clip(ctx.rag_context, budget["rag"])))
        if ctx.memory_context:
            parts.append(_MEMORY_FRAME.format(body=_clip(ctx.memory_context, budget["memory"])))
        if ctx.extra_context:
            parts.append(
                f"# Team decision to honour\n{_clip(ctx.extra_context, budget['extra'])}\n"
            )
        if ctx.feedback:
            parts.append(
                "# Reviewer feedback on your previous attempt — address it directly\n"
                f"{_clip(ctx.feedback, budget['feedback'])}\n"
            )

        parts.append(self.task_instruction())
        return "\n".join(parts)

    def _section_budgets(
        self,
        ctx: AgentContext,
        profile: Optional[ModelProfile],
        reserve: int = 0,
        bodies: Optional[dict[str, str]] = None,
    ) -> dict[str, int]:
        """How many characters each section may spend.

        The overhead is *measured*, not estimated: the same assembly runs once with
        every section at zero, which yields the exact cost of the headings, the code
        fences, the truncation markers and the joins around them. Estimating that is
        how a prompt sized to fill the window ends up thirty characters past it, and
        past it is where Ollama truncates from the head — taking the system prompt,
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
                ctx.routing_mode, ctx.preferred_model, complexity=self.complexity
            )

        empty = dict.fromkeys(list(_PERSON_SHARE) + list(_CONTEXT_SHARE), 0)
        overhead = len(self.system_prompt()) + len(self._user_turn(ctx, empty, bodies))
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
        written = {"idea": ctx.idea, "feedback": ctx.feedback or "", "extra": ctx.extra_context}
        budget = {
            name: min(len(written[name]), int(free * share))
            for name, share in _PERSON_SHARE.items()
        }
        free = max(free - sum(budget.values()), 0)

        present = {
            "depends_on": any(d in ctx.prior_outputs for d in self.depends_on),
            "rag": bool(ctx.rag_context),
            "memory": bool(ctx.memory_context),
        }
        share_total = sum(_CONTEXT_SHARE[n] for n, has in present.items() if has)
        for name, share in _CONTEXT_SHARE.items():
            budget[name] = int(free * (share / share_total)) if present[name] and share_total else 0
        return budget

    def task_instruction(self) -> str:
        """The concrete ask for this phase. Override per agent."""
        return "Produce your deliverable as the JSON object described above."

    def _repair_messages(
        self,
        ctx: AgentContext,
        profile: ModelProfile,
        attempt: str,
        errors: list[str],
    ) -> list[ChatMessage]:
        """The same ask, plus what was wrong with the answer — inside the same window.

        The rejected attempt is echoed back so the model corrects rather than starts
        over, because the thing being repaired is sometimes a wall of generated code.
        That echo is reserved *before* the context sections are sized, so the whole
        exchange still fits: a repair prompt that overflows gets cut from the head,
        and the head is the system prompt naming the shape being repaired.
        """
        problems = "\n".join(f"- {e}" for e in errors[:12])
        instruction = (
            "That response does not match the required shape. Fix exactly these "
            f"problems:\n{problems}\n\n"
            "Return the COMPLETE JSON object again — every key from the shape, "
            "not a patch and not an apology. Keep everything that was already correct."
        )
        echo_budget = max(profile.prompt_char_budget // 4, 1000)
        echo = _clip(attempt, echo_budget)
        return [
            *self._build_messages(ctx, profile, reserve=len(echo) + len(instruction)),
            ChatMessage(role="assistant", content=echo),
            ChatMessage(role="user", content=instruction),
        ]

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


#: A cut the reader can see. A silent one reads as a model that simply stopped.
#: Deliberately carries no number: the skeleton measured to size the budget has to
#: cost exactly what the finished prompt costs, and "55,344" is six characters longer
#: than "0" — which is the whole overshoot, five sections over.
_TRUNCATED = "… [cut here to fit this model's context window]\n"


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
