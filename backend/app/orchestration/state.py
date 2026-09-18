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

    #: The technology decisions frozen once the architecture was settled — language,
    #: frameworks, database, test runner, package manager — serialised by
    #: `charter.Charter.as_dict`. Every phase after System Design is handed it as part
    #: of its standing instructions and checked against it, which is what stops one
    #: run shipping a Postgres architecture, a Mongo backend and Python tests aimed at
    #: JavaScript. Empty until System Design has run; rewritten only when it re-runs.
    charter: dict
