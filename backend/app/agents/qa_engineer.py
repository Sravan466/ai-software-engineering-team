from __future__ import annotations

from app.agents.base import BaseAgent
from app.core.constants import Phase
from app.schemas.agent_outputs import QAEngineerOutput


class QAEngineerAgent(BaseAgent):
    key = Phase.QA_ENGINEER.value
    title = "QA Engineer"
    complexity = "medium"
    role = (
        "You generate unit, integration, and edge-case tests for the generated code and report "
        "on coverage and risk."
    )
    depends_on = (
        Phase.SYSTEM_DESIGN.value,
        Phase.BACKEND_ENGINEER.value,
        Phase.FRONTEND_ENGINEER.value,
    )
    output_model = QAEngineerOutput

    def task_instruction(self) -> str:
        return (
            "Write a test suite for the backend and frontend produced upstream. Cover the P0 "
            "acceptance criteria, key edge cases, and failure modes. Provide runnable test files "
            "(pytest for backend, the framework's test runner for frontend) and estimate coverage."
        )
