from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

from app.agents.base import BaseAgent
from app.core.constants import Phase
from app.schemas.agent_outputs import CostEstimationOutput

_PRICING = Path(__file__).resolve().parent.parent / "core" / "pricing.json"


@lru_cache(maxsize=1)
def pricing_table() -> dict:
    try:
        return json.loads(_PRICING.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def pricing_block(charter=None) -> str:
    """The platform's price list, narrowed to what this build uses when the charter says."""
    table = pricing_table()
    if not table:
        return ""
    words = set()
    if charter is not None:
        db = charter.get("database")
        if db is not None:
            words.add(str(db.token).lower())
        words |= {str(i).lower() for i in getattr(charter, "integrations", ()) or ()}

    def wanted(row: dict) -> bool:
        match = row.get("match")
        return not match or not words or any(m in w or w in m for m in match for w in words)

    def line(row: dict) -> str:
        tiers = row.get("tiers")
        if isinstance(tiers, dict):
            return f"- {row['item']}: " + "; ".join(f"{k} ${v}/mo" for k, v in tiers.items())
        return f"- {row['item']}: {row.get('pricing', '')}"

    rows = list(table.get("hosting") or [])
    rows += [r for r in table.get("databases") or [] if wanted(r)]
    rows += [r for r in table.get("services") or [] if words and wanted(r)]
    return (
        f"PRICE LIST (approximate, as of {table.get('as_of', 'recently')}) — base infrastructure "
        "figures on these, and name the tier in each figure's `assumption`:\n"
        + "\n".join(line(r) for r in rows)
    )


class CostEstimationAgent(BaseAgent):
    key = Phase.COST_ESTIMATION.value
    title = "Cost Estimation"
    complexity = "medium"
    role = (
        "An estimate is only useful with its assumptions beside it: every figure says what "
        "it takes for granted."
    )
    depends_on = (
        Phase.PRODUCT_MANAGER.value,
        Phase.SYSTEM_DESIGN.value,
        Phase.BACKEND_ENGINEER.value,
        Phase.FRONTEND_ENGINEER.value,
        Phase.DEVOPS_ENGINEER.value,
    )
    output_model = CostEstimationOutput

    def standing_brief(self, charter=None) -> str:
        return pricing_block(charter)

    def task_instruction(self) -> str:
        return (
            "Estimate the cost to run and to build this MVP.\n"
            "- Monthly infrastructure from the price list (low = the smallest tier that "
            "works, high = the next one up), and third-party/API costs for the services the "
            "charter uses.\n"
            "- Development effort by role, sized from the P0 features and the file counts in "
            "the Backend and Frontend digests, and the timeline in weeks.\n"
            "- Give every figure an `assumption`, and list the overall `assumptions` "
            "(traffic, team size, tiers). Total the monthly cost and suggest savings.\n"
            "Example: {\"item\": \"Render web service\", \"low_usd\": 0, \"high_usd\": 7, "
            "\"assumption\": \"Free tier until it needs to stay awake, then Starter\"}."
        )

    def to_markdown(self, output: dict) -> str:
        """Each figure with its assumption under it, labelled as an estimate."""
        lines = [f"## {self.title}", "", "_Estimates, not quotes — each rests on the assumption under it._", ""]
        if output.get("summary"):
            lines += [str(output["summary"]), ""]

        def money(v) -> str:
            try:
                return f"${float(v):,.0f}"
            except (TypeError, ValueError):
                return str(v)

        low, high = output.get("total_monthly_low_usd"), output.get("total_monthly_high_usd")
        if low is not None or high is not None:
            lines += [f"**Monthly total:** {money(low)}–{money(high)}", ""]
        infra = [r for r in output.get("monthly_infra_cost") or [] if isinstance(r, dict)]
        if infra:
            lines.append("### Infrastructure (per month)")
            for r in infra:
                lines.append(f"- **{r.get('item')}** — {money(r.get('low_usd'))}–{money(r.get('high_usd'))}")
                if r.get("assumption") or r.get("notes"):
                    lines.append(f"  - _Assumes:_ {r.get('assumption') or r.get('notes')}")
            lines.append("")
        third = [r for r in output.get("api_or_third_party_cost") or [] if isinstance(r, dict)]
        if third:
            lines.append("### Services and APIs (per month)")
            for r in third:
                lines.append(f"- **{r.get('item')}** — {money(r.get('monthly_usd'))}")
                if r.get("assumption"):
                    lines.append(f"  - _Assumes:_ {r['assumption']}")
            lines.append("")
        effort = [r for r in output.get("dev_effort") or [] if isinstance(r, dict)]
        if effort:
            lines.append("### Build effort")
            for r in effort:
                lines.append(f"- **{r.get('role')}** — {r.get('weeks')} weeks")
                if r.get("assumption"):
                    lines.append(f"  - _Assumes:_ {r['assumption']}")
            if output.get("estimated_timeline_weeks") is not None:
                lines.append(f"- **Timeline** — {output['estimated_timeline_weeks']} weeks")
            lines.append("")
        if output.get("assumptions"):
            lines.append("### Assumptions")
            lines += [f"- {a}" for a in output["assumptions"]]
            lines.append("")
        if output.get("cost_optimization_tips"):
            lines.append("### Ways to spend less")
            lines += [f"- {t}" for t in output["cost_optimization_tips"]]
        return "\n".join(lines).rstrip() + "\n"
