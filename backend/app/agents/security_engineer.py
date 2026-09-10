from __future__ import annotations

from app.agents.base import BaseAgent
from app.core.constants import Phase
from app.schemas.agent_outputs import SecurityEngineerOutput


class SecurityEngineerAgent(BaseAgent):
    key = Phase.SECURITY_ENGINEER.value
    title = "Security Engineer"
    complexity = "high"
    role = (
        "You audit the generated design and code for security issues and recommend concrete "
        "fixes. You are thorough but precise — no hand-waving."
    )
    depends_on = (
        Phase.SYSTEM_DESIGN.value,
        Phase.BACKEND_ENGINEER.value,
        Phase.FRONTEND_ENGINEER.value,
    )
    output_model = SecurityEngineerOutput

    def task_instruction(self) -> str:
        return (
            "Perform a security review of the architecture and generated code. Check for SQL "
            "injection, XSS, CSRF, broken authentication/authorization, and exposed secrets. "
            "Report each finding with a severity and a concrete remediation. Give an overall "
            "risk assessment."
        )
