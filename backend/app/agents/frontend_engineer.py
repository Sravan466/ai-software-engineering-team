from __future__ import annotations

from app.agents.base import BaseAgent
from app.core.constants import Phase
from app.schemas.agent_outputs import FrontendEngineerOutput


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
    output_model = FrontendEngineerOutput

    def task_instruction(self) -> str:
        return (
            "Build the frontend for the MVP. Create the pages and components needed for the P0 "
            "user stories, wired to the backend endpoints. Put each source file in the `files` "
            "array with complete code. Default to Next.js + React + Tailwind if unspecified."
        )
