from __future__ import annotations

from app.agents.base import BaseAgent
from app.core.config import settings
from app.core.constants import Phase
from app.schemas.agent_outputs import SystemDesignOutput


class SystemDesignAgent(BaseAgent):
    key = Phase.SYSTEM_DESIGN.value
    title = "System Design"
    complexity = "high"
    role = (
        "Your names become the team's vocabulary: every later phase must use your entity "
        "names, endpoint paths and page routes exactly, so choose them once and carefully."
    )
    depends_on = (Phase.PRODUCT_MANAGER.value,)
    output_model = SystemDesignOutput

    def task_instruction(self) -> str:
        return (
            "Design the architecture for the P0 features in the Product Manager's hand-off.\n"
            f"- Keep the MVP buildable in one pass: at most {settings.sd_max_entities} entities "
            f"in `data_model` and {settings.sd_max_endpoints} endpoints in `api_endpoints`. Put "
            "anything else in `later`.\n"
            "- Every endpoint names the entity it serves (`entity`) and who may call it "
            "(`auth`: public, user, owner or admin). Paths start with /api/.\n"
            "- List the page routes the frontend needs in `pages_expected` (\"/\", "
            "\"/todos/[id]\").\n"
            "- Recommend a concrete tech stack, draw a Mermaid flowchart of the system, and "
            "put anything the spec left undecided in `open_questions`.\n"
            "Example endpoint: {\"method\": \"GET\", \"path\": \"/api/todos/{id}\", \"purpose\": "
            "\"Read one todo\", \"entity\": \"Todo\", \"auth\": \"owner\"}."
        )
