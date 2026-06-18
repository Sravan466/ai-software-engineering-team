from __future__ import annotations

from app.agents.base import BaseAgent
from app.core.constants import Phase


class BackendEngineerAgent(BaseAgent):
    key = Phase.BACKEND_ENGINEER.value
    title = "Backend Engineer"
    complexity = "high"
    role = (
        "You implement backend services, APIs, authentication, and database models that "
        "realise the system design. You write clean, runnable code."
    )
    depends_on = (Phase.PRODUCT_MANAGER.value, Phase.SYSTEM_DESIGN.value)
    output_spec = (
        "{\n"
        '  "framework": "string (e.g. FastAPI)",\n'
        '  "summary": "string",\n'
        '  "db_models": "string (code)",\n'
        '  "auth_flow": "string",\n'
        '  "files": [{"path": "string", "language": "string", "purpose": "string", '
        '"code": "string"}],\n'
        '  "setup_instructions": ["string"]\n'
        "}"
    )

    def task_instruction(self) -> str:
        return (
            "Implement the backend for the MVP. Generate the database models, the API endpoint "
            "handlers from the System Design's API surface, and the auth flow. Put each source "
            "file in the `files` array with complete, runnable code. Prefer the stack the System "
            "Design recommended; default to FastAPI + SQLAlchemy if unspecified."
        )
