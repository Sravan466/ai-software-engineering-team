from __future__ import annotations

from app.agents.base import BaseAgent
from app.core.constants import Phase


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
    output_spec = (
        "{\n"
        '  "summary": "string",\n'
        '  "findings": [{"title": "string", "severity": "critical|high|medium|low", '
        '"category": "string (e.g. SQL injection, XSS, CSRF, authz, secret exposure)", '
        '"location": "string", "description": "string", "recommendation": "string"}],\n'
        '  "secrets_check": "string",\n'
        '  "risk_assessment": "string",\n'
        '  "overall_posture": "string"\n'
        "}"
    )

    def task_instruction(self) -> str:
        return (
            "Perform a security review of the architecture and generated code. Check for SQL "
            "injection, XSS, CSRF, broken authentication/authorization, and exposed secrets. "
            "Report each finding with a severity and a concrete remediation. Give an overall "
            "risk assessment."
        )
