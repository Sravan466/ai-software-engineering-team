from __future__ import annotations

from app.agents.base import BaseAgent
from app.core.constants import Phase
from app.schemas.agent_outputs import FrontendEngineerOutput


class FrontendEngineerAgent(BaseAgent):
    key = Phase.FRONTEND_ENGINEER.value
    title = "Frontend Engineer"
    complexity = "high"
    role = (
        "A page that calls the real backend and handles its empty and error states beats a "
        "polished page wired to nothing."
    )
    depends_on = (
        Phase.PRODUCT_MANAGER.value,
        Phase.SYSTEM_DESIGN.value,
        Phase.BACKEND_ENGINEER.value,
    )
    output_model = FrontendEngineerOutput

    def task_instruction(self) -> str:
        return (
            "Build the frontend for the P0 stories in the Product Manager's hand-off.\n"
            "- Create the pages in the registry's page list, at those routes.\n"
            "- Call only the endpoints in the Backend's `endpoints_implemented`, at exactly "
            "those paths — a call to a path the backend does not serve is sent back.\n"
            "- Show a loading, an empty and an error state for every list.\n"
            "- Put each source file in `files` with complete code.\n"
            "Use the stack the charter fixed; default to Next.js + React + Tailwind if it "
            "fixed none."
        )
