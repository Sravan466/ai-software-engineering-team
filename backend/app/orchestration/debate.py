"""Agent debate system.

Before the Backend Engineer commits to an implementation, specialist viewpoints
(System Design, Backend, Security) argue a key technical decision (e.g. the database
choice) and the platform records a verdict. The decision is then honoured downstream.

Implemented as a single structured LLM call that role-plays the viewpoints and renders
a verdict — cheap, deterministic to parse, and good enough to demonstrate the mechanism.
"""
from __future__ import annotations
from typing import Optional

from app.core.constants import RoutingMode
from app.core.reading import json_object
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
    # How much architecture to quote comes from the model that will answer — and the
    # overhead is measured the same way the agents measure theirs, by assembling the
    # thing with nothing in it. Subtracting a hand-counted `len(_SYSTEM) + len(topic)`
    # misses the headings and the closing line wrapped around them, which is how a
    # budget computed here overruns the window it was computed from.
    profile = router.profile_for(mode, preferred_model, complexity="medium")

    def assemble(quoted: str) -> str:
        return (
            f"# Decision to debate\n{topic}\n\n"
            f"# Architecture context\n{quoted}\n\n"
            "Run the debate and decide."
        )

    overhead = len(_SYSTEM) + len(assemble(""))
    user = assemble(context[: max(profile.prompt_char_budget - overhead, 0)])
    resp = router.complete(
        [ChatMessage(role="system", content=_SYSTEM), ChatMessage(role="user", content=user)],
        mode=mode,
        preferred_model=preferred_model,
        options=GenerationOptions(json_mode=True),
        complexity="medium",
    )
    record = _parse(resp.text, topic)
    return record, resp


def _parse(text: str, topic: str) -> dict:
    """The verdict, or a record that carries the raw reply instead of dying on it.

    The extractor is shared with the agents rather than copied — this function used
    to hold its own near-identical copy, which meant a reply parsing to something
    other than an object (`123`, `"sorry"`) reached `setdefault` and raised an
    `AttributeError` that killed the Backend phase. One copy had been fixed; this
    one had not, which is the argument for there being one.
    """
    data = json_object(text)
    if data is None:
        return {"topic": topic, "arguments": [], "decision": text, "rationale": ""}
    data.setdefault("topic", topic)
    data.setdefault("arguments", [])
    data.setdefault("decision", "")
    data.setdefault("rationale", "")
    return data


def decision_summary(record: dict) -> str:
    """One-paragraph statement of the verdict, for injection into the backend agent."""
    decision = record.get("decision", "").strip()
    rationale = record.get("rationale", "").strip()
    topic = record.get("topic", "").strip()
    out = f"Debate on '{topic}': the team decided — {decision}"
    if rationale:
        out += f" (rationale: {rationale})"
    return out
