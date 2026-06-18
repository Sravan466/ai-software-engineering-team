from __future__ import annotations

from app.agents.base import BaseAgent
from app.core.constants import Phase


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
    output_spec = (
        "{\n"
        '  "summary": "string",\n'
        '  "monthly_infra_cost": [{"item": "string", "low_usd": 0, "high_usd": 0, '
        '"notes": "string"}],\n'
        '  "api_or_third_party_cost": [{"item": "string", "monthly_usd": 0}],\n'
        '  "dev_effort": [{"role": "string", "weeks": 0}],\n'
        '  "estimated_timeline_weeks": 0,\n'
        '  "total_monthly_low_usd": 0,\n'
        '  "total_monthly_high_usd": 0,\n'
        '  "cost_optimization_tips": ["string"]\n'
        "}"
    )

    def task_instruction(self) -> str:
        return (
            "Estimate the cost to build and run this MVP. Break down monthly infrastructure "
            "cost (give low/high ranges), third-party/API costs, and development effort by role "
            "with a timeline in weeks. Total the monthly cost and suggest optimizations."
        )
