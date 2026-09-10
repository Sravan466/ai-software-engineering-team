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
import re
from dataclasses import dataclass, field

from pydantic import BaseModel, ValidationError

from app.core.config import settings
from app.core.constants import RoutingMode, SchemaStatus
from app.core.logging import get_logger
from app.router.model_profile import ModelProfile
from app.router.router import router
from app.schemas.agent_outputs import GenericOutput, response_schema, shape_text
from app.schemas.llm import ChatMessage, GenerationOptions, LLMResponse, Usage

log = get_logger(__name__)

#: How the prompt's character budget is divided between the optional sections. The
#: idea, the task instruction and any human feedback are never cut — they are short,
#: and they are the parts a person wrote. What is left is shared out below.
_CONTEXT_SHARE = {
    "depends_on": 0.62,
    "rag": 0.26,
    "memory": 0.12,
}
#: No section is worth including as a stump.
_MIN_SECTION_CHARS = 400


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
            # attempt. Appending each failure to the last would grow the prompt with
            # every round — which is the shape of the bug being fixed here.
            resp = self._complete(
                self._repair_messages(ask, resp.text, errors, profile), ctx, options
            )
            responses.append(resp)
            output, errors = self._validate(self._parse(resp.text))

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
        self, ctx: AgentContext, profile: Optional[ModelProfile] = None
    ) -> list[ChatMessage]:
        budget = self._section_budgets(ctx, profile)
        parts: list[str] = [f"# Product idea\n{ctx.idea}\n"]

        deps = [d for d in self.depends_on if d in ctx.prior_outputs]
        per_dep = budget["depends_on"] // max(len(deps), 1)
        for dep in deps:
            body = json.dumps(ctx.prior_outputs[dep], indent=2)
            # The "this was cut" note goes outside the fence: inside it, the block
            # the next agent is reading as JSON would no longer parse as any.
            clipped = body[:per_dep] if len(body) > per_dep else body
            note = "" if clipped is body else _TRUNCATED.format(limit=per_dep)
            parts.append(f"# Context — {dep} output\n```json\n{clipped}\n```\n{note}")

        if ctx.rag_context:
            parts.append(
                "# Reference material (from the uploaded knowledge base)\n"
                f"{_clip(ctx.rag_context, budget['rag'])}\n"
            )
        if ctx.memory_context:
            parts.append(
                "# Lessons from past projects (long-term memory)\n"
                f"{_clip(ctx.memory_context, budget['memory'])}\n"
            )
        if ctx.extra_context:
            parts.append(f"# Team decision to honour\n{ctx.extra_context}\n")
        if ctx.feedback:
            parts.append(
                "# Reviewer feedback on your previous attempt — address it directly\n"
                f"{ctx.feedback}\n"
            )

        parts.append(self.task_instruction())
        return [
            ChatMessage(role="system", content=self.system_prompt()),
            ChatMessage(role="user", content="\n".join(parts)),
        ]

    def _section_budgets(
        self, ctx: AgentContext, profile: Optional[ModelProfile]
    ) -> dict[str, int]:
        """How many characters each optional section may spend.

        Derived from the window the model reported, minus what is already committed:
        the system prompt, the idea, the instruction, and anything a person wrote. A
        section's share of what remains is the same proportion whatever the model, so
        a 32k window inlines the whole of the upstream phase and an 8k one inlines the
        part that fits — rather than both cutting at the same invented number.
        """
        if profile is None:
            profile = router.profile_for(
                ctx.routing_mode, ctx.preferred_model, complexity=self.complexity
            )

        committed = len(self.system_prompt()) + len(self.task_instruction()) + len(ctx.idea)
        committed += len(ctx.extra_context) + len(ctx.feedback or "")
        free = max(profile.prompt_char_budget - committed, 0)
        return {
            name: max(int(free * share), _MIN_SECTION_CHARS)
            for name, share in _CONTEXT_SHARE.items()
        }

    def task_instruction(self) -> str:
        """The concrete ask for this phase. Override per agent."""
        return "Produce your deliverable as the JSON object described above."

    def _repair_messages(
        self,
        messages: list[ChatMessage],
        attempt: str,
        errors: list[str],
        profile: ModelProfile,
    ) -> list[ChatMessage]:
        """The same ask, plus what was wrong with the answer.

        The rejected attempt is echoed back so the model corrects rather than starts
        over — clipped to a fraction of the window, because the thing being repaired
        is sometimes a wall of generated code.
        """
        problems = "\n".join(f"- {e}" for e in errors[:12])
        return [
            *messages,
            ChatMessage(
                role="assistant",
                content=_clip(attempt, max(profile.prompt_char_budget // 3, 1000)),
            ),
            ChatMessage(
                role="user",
                content=(
                    "That response does not match the required shape. Fix exactly these "
                    f"problems:\n{problems}\n\n"
                    "Return the COMPLETE JSON object again — every key from the shape, "
                    "not a patch and not an apology. Keep everything that was already "
                    "correct."
                ),
            ),
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
        """Best-effort JSON extraction; fall back to wrapping raw text."""
        text = text.strip()
        # Strip ```json fences if present (greedy so nested braces aren't truncated).
        fence = re.search(r"```(?:json)?\s*(\{[\s\S]*\})\s*```", text)
        candidate = fence.group(1) if fence else text
        # If still not pure JSON, grab the outermost {...}.
        if not candidate.lstrip().startswith("{"):
            brace = re.search(r"\{.*\}", candidate, re.DOTALL)
            if brace:
                candidate = brace.group(0)
        try:
            parsed = json.loads(candidate)
        except (json.JSONDecodeError, ValueError):
            log.warning("Agent returned non-JSON output; wrapping raw text as summary.")
            return {"summary": text}
        return parsed if isinstance(parsed, dict) else {"summary": text}

    def to_markdown(self, output: dict) -> str:
        """Generic renderer; agents may override for nicer formatting."""
        lines: list[str] = [f"## {self.title}\n"]
        lines.append(_render_value(output))
        return "\n".join(lines)


#: A cut the reader can see. A silent one reads as a model that simply stopped.
_TRUNCATED = "… [cut at {limit:,} characters to fit this model's context window]\n"


def _clip(text: str, limit: int) -> str:
    """Cut to `limit` characters, saying so."""
    if limit <= 0 or len(text) <= limit:
        return text
    return text[:limit] + "\n" + _TRUNCATED.format(limit=limit)


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
