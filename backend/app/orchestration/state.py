"""LangGraph pipeline state.

A plain dict state (LastValue channels). Nodes write their phase output into
`prior_outputs` and surface a serialized result in `last_result` so the runner can
persist it to the database after each interrupt.
"""
from __future__ import annotations
from typing import Optional

from typing import TypedDict


class PipelineState(TypedDict, total=False):
    project_id: str
    idea: str
    routing_mode: str
    preferred_model: Optional[str]

    # phase key -> structured agent output
    prior_outputs: dict[str, dict]
    # phase key -> reviewer feedback (set on re-run after a rejection)
    feedback: dict[str, str]

    # Set by the most recently executed node; read by the runner.
    last_phase: str
    last_result: dict

    # Recorded agent debates produced during the run.
    debates: list[dict]
