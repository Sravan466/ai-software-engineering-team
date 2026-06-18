"""The LangGraph StateGraph wiring the eight agents into a sequential pipeline.

Compiled with a SQLite checkpointer and `interrupt_after` on every phase node, so each
`invoke`/resume executes exactly one phase and then pauses — the mechanism behind the
human-approval gates. The Backend phase additionally runs an agent debate first.
"""
from __future__ import annotations

import os
import sqlite3

from langgraph.graph import END, START, StateGraph

from app.agents import get_agent
from app.agents.base import AgentContext
from app.core.constants import PHASE_ORDER, RoutingMode, Phase, PHASE_LABELS
from app.core.config import settings
from app.core.logging import get_logger
from app.memory.store import memory_store
from app.orchestration.debate import conduct_debate, decision_summary
from app.orchestration.state import PipelineState
from app.rag.knowledge_base import knowledge_base

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
    }


def _gather_context(state: PipelineState, phase_key: str) -> tuple[str, str]:
    """(rag_context, memory_context) — both degrade to '' when stores are unavailable."""
    idea = state["idea"]
    query = f"{idea}\n{PHASE_LABELS.get(phase_key, phase_key)}"
    rag = knowledge_base.query(query, k=4)
    mem = memory_store.recall(idea, k=2, exclude_project_id=state.get("project_id"))
    return rag, mem


def _make_node(phase: Phase):
    agent = get_agent(phase.value)

    def node(state: PipelineState) -> dict:
        rag_ctx, mem_ctx = _gather_context(state, phase.value)
        extra = ""
        updates: dict = {}

        # Agent debate runs once, immediately before the Backend phase.
        if phase == Phase.BACKEND_ENGINEER and settings.enable_debate:
            design = state.get("prior_outputs", {}).get(Phase.SYSTEM_DESIGN.value, {})
            context = (
                f"Proposed tech stack: {design.get('tech_stack')}\n"
                f"Data model: {design.get('data_model')}"
            )
            try:
                record, resp = conduct_debate(
                    topic="Which database and core architecture best fit this MVP?",
                    context=context,
                    mode=RoutingMode(state.get("routing_mode", "local_only")),
                    preferred_model=state.get("preferred_model"),
                )
                extra = decision_summary(record)
                record["_usage"] = resp.usage.model_dump()
                record["_provider"] = resp.provider
                record["_model"] = resp.model
                updates["debates"] = [*state.get("debates", []), record]
            except Exception as e:  # noqa: BLE001 - debate is best-effort
                log.warning("Debate step failed (continuing without it): %s", e)

        ctx = AgentContext(
            idea=state["idea"],
            routing_mode=RoutingMode(state.get("routing_mode", "local_only")),
            preferred_model=state.get("preferred_model"),
            prior_outputs=state.get("prior_outputs", {}),
            rag_context=rag_ctx,
            memory_context=mem_ctx,
            feedback=(state.get("feedback") or {}).get(phase.value),
            extra_context=extra,
        )
        result = agent.run(ctx)

        outputs = {**state.get("prior_outputs", {}), phase.value: result.output}
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

    os.makedirs("./data", exist_ok=True)
    conn = sqlite3.connect("./data/checkpoints.sqlite", check_same_thread=False)
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
