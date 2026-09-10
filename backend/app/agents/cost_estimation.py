from __future__ import annotations

from app.agents.base import BaseAgent
from app.core.constants import Phase
from app.schemas.agent_outputs import CostEstimationOutput


class CostEstimationAgent(BaseAgent):
    key = Phase.COST_ESTIMATION.value
    title = "Cost Estimation"
    complexity = "medium"
    role = (
        "You estimate infrastructure, third-party/API, and development costs, and the timeline "
        "to build the MVP."
    )
    depends_on = (
        Phase.SYSTEM_DESIGN.value,
        Phase.DEVOPS_ENGINEER.value,
    )
    output_model = CostEstimationOutput

    def task_instruction(self) -> str:
        return (
            "Estimate the cost to build and run this MVP. Break down monthly infrastructure "
            "cost (give low/high ranges), third-party/API costs, and development effort by role "
            "with a timeline in weeks. Total the monthly cost and suggest optimizations."
        )
