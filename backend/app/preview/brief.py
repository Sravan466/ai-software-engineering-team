"""What the team designed, gathered for the mockup — all of it, not four lines of it.

`build_context` used to compress the Product Manager's features, the Frontend
Engineer's page and component inventory and the System Design data model down to a
paragraph, and the model generating the whole product saw 483 prompt tokens. The
brief keeps the structure instead, so each pass can take the part it needs: the
planner reads pages and entities, the seed pass reads fields, a section reads the
features its brief is about.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Optional


def _rows(value: object) -> list[dict]:
    if isinstance(value, dict):
        return [value]
    if not isinstance(value, list):
        return []
    return [row for row in value if isinstance(row, dict)]


def _strings(value: object) -> list[str]:
    if isinstance(value, str):
        return [value] if value.strip() else []
    if isinstance(value, list):
        return [str(v).strip() for v in value if str(v).strip()]
    return []


def _one_line(value: object, limit: int) -> str:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[: max(limit - 1, 0)] + "…"


@dataclass
class SiteBrief:
    idea: str
    product: str
    tagline: str = ""
    problem: str = ""
    target_users: list[str] = field(default_factory=list)
    #: [{name, priority, description}] from the Product Manager.
    features: list[dict] = field(default_factory=list)
    #: [{as_a, i_want, so_that}] from the Product Manager.
    stories: list[dict] = field(default_factory=list)
    #: [{route, purpose}] from the Frontend Engineer.
    pages: list[dict] = field(default_factory=list)
    #: [{name, purpose}] from the Frontend Engineer.
    components: list[dict] = field(default_factory=list)
    #: [{entity, fields: ["name:type"]}] from System Design.
    entities: list[dict] = field(default_factory=list)
    #: [{method, path, purpose}] from System Design.
    endpoints: list[dict] = field(default_factory=list)
    frontend_framework: str = ""

    @property
    def main_noun(self) -> str:
        """The thing this product is mostly about, when nothing else names it."""
        if self.entities:
            return str(self.entities[0].get("entity") or "")
        words = re.findall(r"[a-zA-Z]+", self.idea.lower())
        stop = {"a", "an", "the", "for", "with", "and", "app", "tool", "platform", "that",
                "to", "of", "tiny", "simple", "small", "web", "site", "website", "my", "our"}
        content = [w for w in words if w not in stop and len(w) > 3]
        return content[0] if content else "items"

    @property
    def has_design(self) -> bool:
        return bool(self.features or self.pages or self.entities)

    # ── prompt text, by the part a pass needs ────────────────────────────────
    def product_block(self) -> str:
        lines = [f"Product: {self.product}", f"Idea: {self.idea}"]
        if self.tagline:
            lines.append(f"Tagline: {self.tagline}")
        if self.problem:
            lines.append(f"Problem: {self.problem}")
        if self.target_users:
            lines.append("Users: " + "; ".join(self.target_users[:5]))
        return "\n".join(lines)

    def features_block(self) -> str:
        if not self.features:
            return ""
        return "Features:\n" + "\n".join(
            f"- [{f.get('priority') or '-'}] {f.get('name')}: {f.get('description') or ''}".rstrip(": ")
            for f in self.features[:12]
        )

    def stories_block(self) -> str:
        if not self.stories:
            return ""
        return "User stories:\n" + "\n".join(
            f"- As {s.get('as_a')}, I want {s.get('i_want')} so that {s.get('so_that')}"
            for s in self.stories[:8]
        )

    def pages_block(self) -> str:
        if not self.pages:
            return ""
        return "Frontend pages:\n" + "\n".join(
            f"- {p.get('route') or p.get('path')}: {p.get('purpose') or ''}" for p in self.pages[:10]
        )

    def components_block(self) -> str:
        if not self.components:
            return ""
        return "Frontend components: " + ", ".join(
            str(c.get("name")) for c in self.components[:16] if c.get("name")
        )

    def entities_block(self) -> str:
        if not self.entities:
            return ""
        return "Data model:\n" + "\n".join(
            f"- {e.get('entity') or e.get('name')}: {', '.join(_strings(e.get('fields')))}"
            for e in self.entities[:6]
        )

    def endpoints_block(self) -> str:
        if not self.endpoints:
            return ""
        return "API: " + "; ".join(
            f"{e.get('method', '')} {e.get('path', '')}".strip() for e in self.endpoints[:12]
        )

    def planning_text(self) -> str:
        """Everything the planner reads, most important first — clipped from the end."""
        parts = [
            self.product_block(),
            self.features_block(),
            self.pages_block(),
            self.entities_block(),
            self.stories_block(),
            self.components_block(),
            self.endpoints_block(),
        ]
        return "\n\n".join(p for p in parts if p)


def site_brief(project) -> SiteBrief:
    """Read the current attempt at each phase into a brief.

    The current attempt, not every attempt: a rejected Frontend phase's pages are the
    pages the reviewer turned down, and drawing them would picture work that was
    replaced.
    """
    from app.core.artifacts import current_phases  # local: artifacts imports db models

    by_phase = {
        ph.phase: ph.output for ph in current_phases(project) if isinstance(ph.output, dict)
    }
    pm = by_phase.get("product_manager", {})
    fe = by_phase.get("frontend_engineer", {})
    sd = by_phase.get("system_design", {})

    name = str(pm.get("product_name") or project.name or "").strip()
    product = name or _one_line(project.idea, 40)

    features = []
    for row in _rows(pm.get("features")):
        if row.get("name"):
            features.append(
                {
                    "name": _one_line(row.get("name"), 80),
                    "priority": _one_line(row.get("priority") or "", 8),
                    "description": _one_line(row.get("description") or "", 240),
                }
            )
    if not features:
        features = [{"name": _one_line(s, 80), "priority": "", "description": ""}
                    for s in _strings(pm.get("mvp_scope"))[:8]]

    return SiteBrief(
        idea=_one_line(project.idea, 600),
        product=product,
        problem=_one_line(pm.get("problem_statement") or "", 400),
        target_users=[_one_line(u, 120) for u in _strings(pm.get("target_users"))],
        features=features,
        stories=[
            {k: _one_line(s.get(k) or "", 160) for k in ("as_a", "i_want", "so_that")}
            for s in _rows(pm.get("user_stories"))
        ],
        pages=[
            {"route": _one_line(p.get("route") or p.get("path") or "", 60),
             "purpose": _one_line(p.get("purpose") or "", 200)}
            for p in _rows(fe.get("pages"))
        ],
        components=[
            {"name": _one_line(c.get("name") or "", 60), "purpose": _one_line(c.get("purpose") or "", 120)}
            for c in _rows(fe.get("components"))
        ],
        entities=[
            {"entity": _one_line(e.get("entity") or e.get("name") or "", 60),
             "fields": [_one_line(f, 60) for f in _strings(e.get("fields"))][:12]}
            for e in _rows(sd.get("data_model"))
            if e.get("entity") or e.get("name")
        ],
        endpoints=[
            {"method": _one_line(e.get("method") or "", 8), "path": _one_line(e.get("path") or "", 80)}
            for e in _rows(sd.get("api_endpoints"))
        ],
        frontend_framework=_one_line(fe.get("framework") or "", 60),
    )


def brief_from_idea(idea: str, name: Optional[str] = None) -> SiteBrief:
    """A brief with nothing but the idea — for a mockup drawn before any phase ran."""
    return SiteBrief(idea=_one_line(idea, 600), product=(name or _one_line(idea, 40)).strip())
