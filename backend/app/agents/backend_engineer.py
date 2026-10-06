from __future__ import annotations

from app.agents.base import BaseAgent
from app.core.constants import Phase
from app.schemas.agent_outputs import BackendEngineerOutput


class BackendEngineerAgent(BaseAgent):
    key = Phase.BACKEND_ENGINEER.value
    title = "Backend Engineer"
    complexity = "high"
    role = (
        "Prefer the scaffold's conventions over your own: a smaller backend that runs beats "
        "a larger one that does not."
    )
    depends_on = (Phase.PRODUCT_MANAGER.value, Phase.SYSTEM_DESIGN.value)
    output_model = BackendEngineerOutput

    def task_instruction(self) -> str:
        return (
            "Implement the backend for the P0 stories only.\n"
            "- Serve exactly the endpoints in the name registry, at those paths, using its "
            "entity names for models and tables. Add no endpoint the frontend has no use for.\n"
            "- Enforce each endpoint's `auth` from System Design, and describe the flow in "
            "`auth_flow`.\n"
            "- Include seed or demo data so the app shows something on first run.\n"
            "- Put each source file in `files` with complete, runnable code; give each a "
            "one-line `purpose`.\n"
            "Use the stack the charter fixed; default to FastAPI + SQLAlchemy if it fixed none."
        )
