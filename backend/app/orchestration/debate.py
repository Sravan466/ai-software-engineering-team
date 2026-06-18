"""Agent debate system.

Before the Backend Engineer commits to an implementation, specialist viewpoints
(System Design, Backend, Security) argue a key technical decision (e.g. the database
choice) and the platform records a verdict. The decision is then honoured downstream.

Implemented as a single structured LLM call that role-plays the viewpoints and renders
a verdict — cheap, deterministic to parse, and good enough to demonstrate the mechanism.
"""
from __future__ import annotations
from typing import Optional

import json
import re

from app.core.constants import RoutingMode
from app.core.logging import get_logger
from app.router.router import router
from app.schemas.llm import ChatMessage, GenerationOptions, LLMResponse

log = get_logger(__name__)

_SYSTEM = (
    "You are the moderator of a software engineering design debate. Three specialist "
    "agents — System Design, Backend Engineer, and Security Engineer — argue a key "
    "technical decision. Present each agent's position with a short rationale, then render "
    "a single verdict with justification. Respond with ONLY this JSON object:\n"
    '{ "topic": "string", '
    '"arguments": [{"agent": "string", "position": "string", "rationale": "string"}], '
    '"decision": "string", "rationale": "string" }'
)


def conduct_debate(
    topic: str,
    context: str,
    *,
    mode: RoutingMode,
    preferred_model: Optional[str],
) -> tuple[dict, LLMResponse]:
    user = (
        f"# Decision to debate\n{topic}\n\n"
        f"# Architecture context\n{context[:5000]}\n\n"
        "Run the debate and decide."
    )
    resp = router.complete(
        [ChatMessage(role="system", content=_SYSTEM), ChatMessage(role="user", content=user)],
        mode=mode,
        preferred_model=preferred_model,
        options=GenerationOptions(max_tokens=2048, json_mode=True),
        complexity="medium",
    )
    record = _parse(resp.text, topic)
    return record, resp


def _parse(text: str, topic: str) -> dict:
    text = text.strip()
    fence = re.search(r"```(?:json)?\s*(\{[\s\S]*\})\s*```", text)
    candidate = fence.group(1) if fence else text
    if not candidate.lstrip().startswith("{"):
        brace = re.search(r"\{.*\}", candidate, re.DOTALL)
        if brace:
            candidate = brace.group(0)
    try:
        data = json.loads(candidate)
        data.setdefault("topic", topic)
        data.setdefault("arguments", [])
        data.setdefault("decision", "")
        data.setdefault("rationale", "")
        return data
    except (json.JSONDecodeError, ValueError):
        return {"topic": topic, "arguments": [], "decision": text, "rationale": ""}


def decision_summary(record: dict) -> str:
    """One-paragraph statement of the verdict, for injection into the backend agent."""
    decision = record.get("decision", "").strip()
    rationale = record.get("rationale", "").strip()
    topic = record.get("topic", "").strip()
    out = f"Debate on '{topic}': the team decided — {decision}"
    if rationale:
        out += f" (rationale: {rationale})"
    return out
