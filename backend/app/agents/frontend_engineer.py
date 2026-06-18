from __future__ import annotations

from app.agents.base import BaseAgent
from app.core.constants import Phase


class FrontendEngineerAgent(BaseAgent):
    key = Phase.FRONTEND_ENGINEER.value
    title = "Frontend Engineer"
    complexity = "high"
    role = (
        "You build the UI: components, pages, responsive layouts, and state management that "
        "consume the backend API."
    )
    depends_on = (
        Phase.PRODUCT_MANAGER.value,
        Phase.SYSTEM_DESIGN.value,
        Phase.BACKEND_ENGINEER.value,
    )
    output_spec = (
        "{\n"
        '  "framework": "string (e.g. Next.js + React)",\n'
        '  "summary": "string",\n'
        '  "pages": [{"route": "string", "purpose": "string"}],\n'
        '  "components": [{"name": "string", "purpose": "string"}],\n'
        '  "state_management": "string",\n'
        '  "files": [{"path": "string", "language": "string", "purpose": "string", '
        '"code": "string"}]\n'
        "}"
    )

    def task_instruction(self) -> str:
        return (
            "Build the frontend for the MVP. Create the pages and components needed for the P0 "
            "user stories, wired to the backend endpoints. Put each source file in the `files` "
            "array with complete code. Default to Next.js + React + Tailwind if unspecified."
        )
