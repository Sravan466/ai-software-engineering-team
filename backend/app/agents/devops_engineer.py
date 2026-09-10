from __future__ import annotations

from app.agents.base import BaseAgent
from app.core.constants import Phase
from app.schemas.agent_outputs import DevOpsEngineerOutput


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
    output_model = DevOpsEngineerOutput

    def task_instruction(self) -> str:
        return (
            "Produce the deployment plan: Dockerfiles for each service, a docker-compose (or "
            "k8s manifests), and a GitHub Actions CI/CD workflow. Provide ordered deployment "
            "steps and a rollback plan."
        )
