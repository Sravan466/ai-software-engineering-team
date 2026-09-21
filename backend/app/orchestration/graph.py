"""The LangGraph StateGraph wiring the eight agents into a sequential pipeline.

Compiled with a SQLite checkpointer and `interrupt_after` on every phase node, so each
`invoke`/resume executes exactly one phase and then pauses — the mechanism behind the
human-approval gates.

Two things happen here that make the eight agents one team rather than eight authors:

  **The debate runs before System Design**, not before Backend. A verdict delivered
  after the architecture had already picked a database was a verdict about nothing,
  and the Backend Engineer — the phase it was injected into — ignored it, because it
  arrived as prose with no obligation attached.

  **The charter is frozen once System Design returns**, and from then on it is part of
  every downstream agent's standing instructions and checked against everything they
  write. That is the difference between eight plausible phases and one build.
"""
from __future__ import annotations

import os
import sqlite3
from typing import Optional

from langgraph.graph import END, START, StateGraph

from app.agents import get_agent
from app.agents.base import AgentContext
from app.core.constants import PHASE_ORDER, RoutingMode, Phase, PHASE_LABELS
from app.core.config import settings
from app.core.logging import get_logger
from app.memory.store import memory_store
from app.orchestration import debate as debate_step
from app.orchestration.charter import binding_on, freeze
from app.orchestration.debate import conduct_debate, decision_summary
from app.orchestration.state import PipelineState
from app.rag.knowledge_base import knowledge_base
from app.skills import selection as skills

log = get_logger(__name__)


def _serialize_result(phase_key: str, title: str, result) -> dict:
    r = result.response
    return {
        "phase": phase_key,
        "agent": title,
        "output": result.output,
        "content_md": result.content_md,
        "model_used": r.model,
        "provider_used": r.provider,
        "usage": r.usage.model_dump(),
        "latency_ms": r.latency_ms,
        "fallback_used": r.fallback_used,
        # Whether the agent actually produced the shape it was asked for. Two gates
        # downstream read specific keys off `output`; this is how they know whether
        # a missing one means "nothing to report" or "nobody could read the report".
        "schema_status": result.schema_status,
        "schema_note": result.schema_note,
        # Where this deliverable contradicts the stack the architecture froze. A
        # separate fact from the schema status on purpose: "nobody could read this
        # report" and "this report is written against a different database than the
        # rest of the build" are different problems with different fixes, and the
        # reviewer is shown which of the two happened.
        "stack_violations": list(result.stack_violations),
        # Whether the phase's code compiles, and what is still wrong when it does
        # not. A third fact beside the two above, with its own fix: the files named.
        "build_status": result.build_status,
        "build_problems": list(result.build_problems),
        # Which procedures this deliverable was actually written with. Recorded
        # because a skill you cannot confirm reached the model is indistinguishable
        # from one that did nothing — and because selection is a keyword score, so
        # "this phase got none" is a result the reviewer has to be able to see.
        "skills_used": list(result.skills_used),
        # One entry per model call. A repaired phase made two, and analytics counts
        # calls and averages latency across them — folding both into a single event
        # would report one call that took as long as two.
        "calls": [
            {
                "provider": c.provider,
                "model": c.model,
                "usage": c.usage.model_dump(),
                "latency_ms": c.latency_ms,
                "fallback_used": c.fallback_used,
            }
            for c in (result.calls or [result.response])
        ],
    }


def _last_debate(state: PipelineState) -> Optional[dict]:
    """The most recent recorded verdict — what a re-run of System Design freezes on.

    A redo of the architecture does not re-run the debate (the team settled that
    question once), so without this the second charter would lose the decision the
    first one recorded and the rebuild could land on a different database than the
    phases that survived the rewind.
    """
    debates = state.get("debates") or []
    return debates[-1] if debates else None


def _gather_context(state: PipelineState, phase_key: str) -> tuple[str, str]:
    """(rag_context, memory_context) — both degrade to '' when stores are unavailable."""
    idea = state["idea"]
    query = f"{idea}\n{PHASE_LABELS.get(phase_key, phase_key)}"
    rag = knowledge_base.query(query, k=4)
    mem = memory_store.recall(idea, k=2, exclude_project_id=state.get("project_id"))
    return rag, mem


def gather_skills(state: PipelineState, phase_key: str) -> tuple:
    """The procedures this phase should be working from, best first.

    Assembled here beside RAG and memory, and for the same reason: it is context the
    pipeline gathers, not something the agent goes looking for. What it is *not* is
    a finished block — how much of it fits belongs to the agent, which is the only
    part of this that knows which model is about to answer.

    Best effort, exactly like the two above it. A library that cannot be read leaves
    a phase with no skills and a build that runs as it did before skills existed;
    it never stops a run.
    """
    try:
        return tuple(
            skills.select(
                phase_key,
                state["idea"],
                state.get("prior_outputs", {}),
                skills.Overrides.from_dict(state.get("skill_overrides")),
            )
        )
    except Exception as e:  # noqa: BLE001 - skills must never fail a phase
        log.warning("Skill selection failed for %s (continuing without): %s", phase_key, e)
        return ()


def run_debate(state: PipelineState) -> tuple[Optional[dict], str]:
    """Settle the architecture question, before the architecture is drawn.

    Returns (record, summary). Both are empty when the debate is switched off or the
    call failed — it is best effort, and a moderator that cannot answer must not stop
    a build. What it must not do is answer *late*, which is what it used to do.
    """
    if not settings.enable_debate:
        return None, ""
    brief = state.get("prior_outputs", {}).get(Phase.PRODUCT_MANAGER.value, {})
    context = (
        f"Product: {brief.get('product_name')}\n"
        f"Problem: {brief.get('problem_statement')}\n"
        f"MVP scope: {brief.get('mvp_scope')}\n"
        f"Features: {brief.get('features')}"
    )
    try:
        record, resp = conduct_debate(
            topic=debate_step.TOPIC,
            context=context,
            mode=RoutingMode(state.get("routing_mode", "local_only")),
            preferred_model=state.get("preferred_model"),
        )
    except Exception as e:  # noqa: BLE001 - debate is best-effort
        log.warning("Debate step failed (continuing without it): %s", e)
        return None, ""
    record["_usage"] = resp.usage.model_dump()
    record["_provider"] = resp.provider
    record["_model"] = resp.model
    return record, decision_summary(record)


def _make_node(phase: Phase):
    agent = get_agent(phase.value)

    def node(state: PipelineState) -> dict:
        rag_ctx, mem_ctx = _gather_context(state, phase.value)
        skill_ctx = gather_skills(state, phase.value)
        extra = ""
        updates: dict = {}
        verdict: Optional[dict] = None

        # The debate runs once, immediately before the architecture it is about.
        if phase == Phase.SYSTEM_DESIGN:
            verdict, extra = run_debate(state)
            if verdict is not None:
                updates["debates"] = [*state.get("debates", []), verdict]

        ctx = AgentContext(
            idea=state["idea"],
            routing_mode=RoutingMode(state.get("routing_mode", "local_only")),
            preferred_model=state.get("preferred_model"),
            prior_outputs=state.get("prior_outputs", {}),
            rag_context=rag_ctx,
            memory_context=mem_ctx,
            skills=skill_ctx,
            feedback=(state.get("feedback") or {}).get(phase.value),
            extra_context=extra,
            # Everything from the Backend Engineer onwards builds against the same
            # frozen stack. System Design is the phase that decides it, so it is the
            # one phase given none — it cannot be held to a charter it is writing.
            charter=binding_on(phase.value, state.get("charter")),
        )
        result = agent.run(ctx)

        outputs = {**state.get("prior_outputs", {}), phase.value: result.output}
        if phase == Phase.SYSTEM_DESIGN:
            # Frozen here and never rewritten by a later phase. A charter a phase
            # could edit on its way past is a charter that says whatever the last
            # agent to run believed, which is the situation this replaces.
            charter = freeze(result.output, verdict or _last_debate(state))
            updates["charter"] = charter.as_dict() if charter else {}

        updates.update(
            {
                "prior_outputs": outputs,
                "last_phase": phase.value,
                "last_result": _serialize_result(phase.value, agent.title, result),
            }
        )
        return updates

    return node


def build_graph() -> StateGraph:
    g = StateGraph(PipelineState)
    for phase in PHASE_ORDER:
        g.add_node(phase.value, _make_node(phase))

    g.add_edge(START, PHASE_ORDER[0].value)
    for a, b in zip(PHASE_ORDER, PHASE_ORDER[1:]):
        g.add_edge(a.value, b.value)
    g.add_edge(PHASE_ORDER[-1].value, END)
    return g


# ── Compiled singleton with persistent SQLite checkpointing ──────────────────
def _build_checkpointer():
    from langgraph.checkpoint.sqlite import SqliteSaver

    path = settings.checkpoint_db_path
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    conn = sqlite3.connect(path, check_same_thread=False)
    saver = SqliteSaver(conn)
    saver.setup()
    return saver


_INTERRUPT_AFTER = [p.value for p in PHASE_ORDER]

graph = build_graph().compile(
    checkpointer=_build_checkpointer(),
    interrupt_after=_INTERRUPT_AFTER,
)


def mermaid_diagram() -> str:
    """Return the pipeline graph as Mermaid (handy for docs / the UI)."""
    try:
        return graph.get_graph().draw_mermaid()
    except Exception:  # noqa: BLE001
        nodes = " --> ".join(p.value for p in PHASE_ORDER)
        return f"flowchart TD\n  START --> {nodes} --> END"
