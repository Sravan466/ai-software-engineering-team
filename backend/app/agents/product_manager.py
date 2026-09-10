from __future__ import annotations

from app.agents.base import BaseAgent
from app.core.constants import Phase
from app.schemas.agent_outputs import ProductManagerOutput


class ProductManagerAgent(BaseAgent):
    key = Phase.PRODUCT_MANAGER.value
    title = "Product Manager"
    complexity = "medium"
    role = (
        "You clarify the product, define a focused MVP, prioritise features, and write a "
        "crisp PRD with user stories and acceptance criteria."
    )
    depends_on = ()
    output_model = ProductManagerOutput

    def task_instruction(self) -> str:
        return (
            "Write the Product Requirements Document for this idea. Keep the MVP scope tight "
            "and realistic. Prioritise features (P0 = must-have for launch). Make acceptance "
            "criteria testable."
        )
