from __future__ import annotations

from app.agents.base import BaseAgent
from app.core.constants import Phase


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
    output_spec = (
        "{\n"
        '  "summary": "string",\n'
        '  "test_strategy": "string",\n'
        '  "test_files": [{"path": "string", "framework": "string", "targets": "string", '
        '"code": "string"}],\n'
        '  "edge_cases": ["string"],\n'
        '  "coverage_estimate": "string",\n'
        '  "risks": ["string"]\n'
        "}"
    )

    def task_instruction(self) -> str:
        return (
            "Write a test suite for the backend and frontend produced upstream. Cover the P0 "
            "acceptance criteria, key edge cases, and failure modes. Provide runnable test files "
            "(pytest for backend, the framework's test runner for frontend) and estimate coverage."
        )
