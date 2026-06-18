from __future__ import annotations

from app.agents.base import BaseAgent
from app.core.constants import Phase


class DevOpsEngineerAgent(BaseAgent):
    key = Phase.DEVOPS_ENGINEER.value
    title = "DevOps Engineer"
    complexity = "medium"
    role = (
        "You plan deployment and generate infrastructure and CI/CD configuration so the system "
        "can ship reliably."
    )
    depends_on = (
        Phase.SYSTEM_DESIGN.value,
        Phase.BACKEND_ENGINEER.value,
        Phase.FRONTEND_ENGINEER.value,
    )
    output_spec = (
        "{\n"
        '  "summary": "string",\n'
        '  "target_platform": "string",\n'
        '  "dockerfiles": [{"path": "string", "content": "string"}],\n'
        '  "compose_or_manifests": [{"path": "string", "content": "string"}],\n'
        '  "ci_cd": [{"path": "string", "tool": "string", "content": "string"}],\n'
        '  "deployment_steps": ["string"],\n'
        '  "rollback_plan": "string"\n'
        "}"
    )

    def task_instruction(self) -> str:
        return (
            "Produce the deployment plan: Dockerfiles for each service, a docker-compose (or "
            "k8s manifests), and a GitHub Actions CI/CD workflow. Provide ordered deployment "
            "steps and a rollback plan."
        )
