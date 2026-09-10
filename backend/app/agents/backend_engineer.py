from __future__ import annotations

from app.agents.base import BaseAgent
from app.core.constants import Phase
from app.schemas.agent_outputs import BackendEngineerOutput


class BackendEngineerAgent(BaseAgent):
    key = Phase.BACKEND_ENGINEER.value
    title = "Backend Engineer"
    complexity = "high"
    role = (
        "You implement backend services, APIs, authentication, and database models that "
        "realise the system design. You write clean, runnable code."
    )
    depends_on = (Phase.PRODUCT_MANAGER.value, Phase.SYSTEM_DESIGN.value)
    output_model = BackendEngineerOutput

    def task_instruction(self) -> str:
        return (
            "Implement the backend for the MVP. Generate the database models, the API endpoint "
            "handlers from the System Design's API surface, and the auth flow. Put each source "
            "file in the `files` array with complete, runnable code. Prefer the stack the System "
            "Design recommended; default to FastAPI + SQLAlchemy if unspecified."
        )
