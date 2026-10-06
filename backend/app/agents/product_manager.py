from __future__ import annotations

from app.agents.base import BaseAgent
from app.core.config import settings
from app.core.constants import Phase
from app.schemas.agent_outputs import ProductManagerOutput


class ProductManagerAgent(BaseAgent):
    key = Phase.PRODUCT_MANAGER.value
    title = "Product Manager"
    complexity = "medium"
    role = (
        "A good spec here is one a small team can build in a single pass: few features, "
        "each with stories a tester can check."
    )
    depends_on = ()
    output_model = ProductManagerOutput

    def task_instruction(self) -> str:
        cap = settings.pm_max_p0_features
        return (
            "Write the product spec for this idea.\n"
            f"- Prioritise every feature P0, P1 or P2, with at most {cap} P0 features — the "
            "ones the app cannot launch without.\n"
            "- Give every feature at least one user story, and set each story's `feature` to "
            "that feature's exact name. No feature without a story.\n"
            "- Make every acceptance criterion testable: an action and an observable result.\n"
            "- List what is deliberately out (`out_of_scope`) and what you assumed "
            "(`assumptions`), so later phases can tell a guess from a requirement.\n"
            "Example story: as_a \"shopper\", i_want \"to save an item to a wishlist\", "
            "feature \"Wishlist\", acceptance_criteria [\"Clicking Save adds the item to "
            "/wishlist\", \"Saving twice keeps one copy\"]."
        )
