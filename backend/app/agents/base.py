"""Base agent: prompt assembly, routing, lenient JSON parsing, markdown rendering.

Each specialist subclass declares its role (system prompt), the JSON shape it should
return, and which upstream phase outputs it depends on. The base class handles everything
else: building the message list (with RAG + memory context), calling the router, parsing
the model's response, and turning it into display markdown.
"""
from __future__ import annotations
from typing import Optional

import json
import re
from dataclasses import dataclass, field

from app.core.constants import RoutingMode
from app.core.logging import get_logger
from app.router.router import router
from app.schemas.llm import ChatMessage, GenerationOptions, LLMResponse

log = get_logger(__name__)


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


class BaseAgent:
    #: phase key == constants.Phase value
    key: str = "base"
    title: str = "Base Agent"
    complexity: str = "medium"  # routing hint: low | medium | high

    #: One-line description of the agent's mandate (also shown in the UI).
    role: str = ""
    #: Human description of the JSON keys the agent should produce.
    output_spec: str = '{ "summary": "string" }'
    #: Which prior phase outputs to inline as context (by phase key).
    depends_on: tuple[str, ...] = ()

    # ── public entrypoint ───────────────────────────────────────────────────
    def run(self, ctx: AgentContext) -> AgentResult:
        messages = self._build_messages(ctx)
        resp = router.complete(
            messages,
            mode=ctx.routing_mode,
            preferred_model=ctx.preferred_model,
            options=GenerationOptions(max_tokens=4096, json_mode=True),
            complexity=self.complexity,
        )
        output = self._parse(resp.text)
        content_md = self.to_markdown(output)
        return AgentResult(output=output, content_md=content_md, response=resp)

    # ── prompt construction ─────────────────────────────────────────────────
    def system_prompt(self) -> str:
        return (
            f"You are the {self.title} on an AI software engineering team. {self.role}\n\n"
            "You collaborate with other specialist agents; your output is consumed by the "
            "next agent in the pipeline, so be precise, concrete, and complete.\n\n"
            "Respond with ONLY a single valid JSON object — no prose, no markdown fences — "
            f"matching this shape:\n{self.output_spec}"
        )

    def _build_messages(self, ctx: AgentContext) -> list[ChatMessage]:
        parts: list[str] = [f"# Product idea\n{ctx.idea}\n"]

        for dep in self.depends_on:
            if dep in ctx.prior_outputs:
                parts.append(
                    f"# Context — {dep} output\n```json\n"
                    f"{json.dumps(ctx.prior_outputs[dep], indent=2)[:6000]}\n```\n"
                )

        if ctx.rag_context:
            parts.append(
                "# Reference material (from the uploaded knowledge base)\n"
                f"{ctx.rag_context[:4000]}\n"
            )
        if ctx.memory_context:
            parts.append(
                "# Lessons from past projects (long-term memory)\n"
                f"{ctx.memory_context[:2000]}\n"
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

    def task_instruction(self) -> str:
        """The concrete ask for this phase. Override per agent."""
        return "Produce your deliverable as the JSON object described above."

    # ── output handling ───────────────────────────────────────────────────────
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
            return json.loads(candidate)
        except (json.JSONDecodeError, ValueError):
            log.warning("Agent returned non-JSON output; wrapping raw text as summary.")
            return {"summary": text}

    def to_markdown(self, output: dict) -> str:
        """Generic renderer; agents may override for nicer formatting."""
        lines: list[str] = [f"## {self.title}\n"]
        lines.append(_render_value(output))
        return "\n".join(lines)


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
