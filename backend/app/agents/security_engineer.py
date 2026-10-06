from __future__ import annotations

from app.agents.base import BaseAgent
from app.core.constants import Phase
from app.schemas.agent_outputs import SecurityEngineerOutput


class SecurityEngineerAgent(BaseAgent):
    key = Phase.SECURITY_ENGINEER.value
    title = "Security Engineer"
    complexity = "high"
    role = (
        "Report only what you can point at: a file and line you were shown, or an endpoint "
        "in the design. A finding in code that is not in your hand-off is a guess."
    )
    depends_on = (
        Phase.SYSTEM_DESIGN.value,
        Phase.BACKEND_ENGINEER.value,
        Phase.FRONTEND_ENGINEER.value,
    )
    output_model = SecurityEngineerOutput

    def task_instruction(self) -> str:
        return (
            "Review the architecture and the generated code you were handed. Check each "
            "endpoint's `auth` against what its handler enforces, then look for SQL "
            "injection, XSS, CSRF and exposed secrets. Report each finding with its file "
            "(`location`), a severity and a concrete fix. Where the code you were shown was "
            "cut, say so rather than assume. Give an overall risk assessment."
        )
