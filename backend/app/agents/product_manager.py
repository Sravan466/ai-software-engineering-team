from __future__ import annotations

from app.agents.base import BaseAgent
from app.core.constants import Phase


class ProductManagerAgent(BaseAgent):
    key = Phase.PRODUCT_MANAGER.value
    title = "Product Manager"
    complexity = "medium"
    role = (
        "You clarify the product, define a focused MVP, prioritise features, and write a "
        "crisp PRD with user stories and acceptance criteria."
    )
    depends_on = ()
    output_spec = (
        "{\n"
        '  "product_name": "string",\n'
        '  "problem_statement": "string",\n'
        '  "target_users": ["string"],\n'
        '  "mvp_scope": ["string"],\n'
        '  "out_of_scope": ["string"],\n'
        '  "features": [{"name": "string", "priority": "P0|P1|P2", "description": "string"}],\n'
        '  "user_stories": [{"as_a": "string", "i_want": "string", "so_that": "string", '
        '"acceptance_criteria": ["string"]}],\n'
        '  "success_metrics": ["string"],\n'
        '  "roadmap": [{"milestone": "string", "deliverables": ["string"]}]\n'
        "}"
    )

    def task_instruction(self) -> str:
        return (
            "Write the Product Requirements Document for this idea. Keep the MVP scope tight "
            "and realistic. Prioritise features (P0 = must-have for launch). Make acceptance "
            "criteria testable."
        )
