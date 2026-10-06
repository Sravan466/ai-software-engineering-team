from __future__ import annotations

from app.agents.base import BaseAgent
from app.core.constants import Phase
from app.schemas.agent_outputs import QAEngineerOutput


class QAEngineerAgent(BaseAgent):
    key = Phase.QA_ENGINEER.value
    title = "QA Engineer"
    complexity = "medium"
    role = (
        "Name each test after the criterion it checks; import real code by its real path."
    )
    depends_on = (
        Phase.PRODUCT_MANAGER.value,
        Phase.SYSTEM_DESIGN.value,
        Phase.BACKEND_ENGINEER.value,
        Phase.FRONTEND_ENGINEER.value,
    )
    output_model = QAEngineerOutput

    def task_instruction(self) -> str:
        return (
            "Test the code the Backend and Frontend phases wrote.\n"
            "- One test per P0 acceptance criterion from the Product Manager, named after it.\n"
            "- Use the registry's paths and the files in each digest; add the edge cases that matter.\n"
            "- pytest for a Python backend, the frontend's runner for the frontend; put the "
            "commands that run them in `command_backend` / `command_frontend`."
        )
