from __future__ import annotations

from app.agents.base import BaseAgent
from app.core.constants import Phase


class SystemDesignAgent(BaseAgent):
    key = Phase.SYSTEM_DESIGN.value
    title = "System Design"
    complexity = "high"
    role = (
        "You design the system architecture, choose the tech stack, model the database and "
        "APIs, and plan for scale. You justify each major choice."
    )
    depends_on = (Phase.PRODUCT_MANAGER.value,)
    output_spec = (
        "{\n"
        '  "architecture_overview": "string",\n'
        '  "tech_stack": {"frontend": ["string"], "backend": ["string"], '
        '"database": ["string"], "infra": ["string"]},\n'
        '  "components": [{"name": "string", "responsibility": "string"}],\n'
        '  "data_model": [{"entity": "string", "fields": ["name:type"], '
        '"relationships": ["string"]}],\n'
        '  "api_endpoints": [{"method": "string", "path": "string", "purpose": "string"}],\n'
        '  "scaling_considerations": ["string"],\n'
        '  "architecture_diagram_mermaid": "string (a Mermaid flowchart definition)"\n'
        "}"
    )

    def task_instruction(self) -> str:
        return (
            "Design the architecture for the MVP defined by the Product Manager. Produce an ER "
            "model, REST API surface, and a Mermaid diagram of the system. Recommend a concrete "
            "tech stack and note scaling considerations."
        )
