"""Pass 2 — the page plan: routes, the sections on each, and the data behind them.

The single-shot generator was handed about four lines of the team's work. The PM's
feature list, the Frontend Engineer's page inventory and the System Design data model
were all in state and all thrown away before the model saw them. This pass is where
they come back: a plan of routes and sections derived from them, with the records
those sections show declared as typed collections the runtime can store and filter.

The plan is the one place the mockup's *guarantees* are enforced, because every later
pass works to it. Whatever the model proposes, the normalised plan has more than one
route, at least one list that can be filtered and sorted, and at least one form that
writes to a collection — the issue's definition of a mockup with logic. When the
model's plan is unusable, the same guarantees are met by a plan built from state alone.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable, Optional

from pydantic import BaseModel, Field

#: What a section can be, and what each one is for — shown to the planner verbatim.
KINDS: dict[str, str] = {
    "hero": "the page's opening: headline, supporting line, primary actions",
    "features": "a grid of the product's capabilities",
    "stats": "live figures computed from a collection (counts, totals, averages)",
    "list": "a filterable, sortable grid of cards from a collection",
    "table": "a sortable, searchable table of a collection's records",
    "form": "a validated form that creates a record in a collection",
    "testimonials": "quotes from believable users",
    "pricing": "plans and prices",
    "faq": "questions and answers",
    "cta": "a closing call to action",
    "content": "explanatory copy: how it works, steps, an about block",
}
#: The kinds that read or write a collection, and so must name one.
BOUND_KINDS = frozenset({"stats", "list", "table", "form"})
#: The kinds that are part of every page rather than any one of them.
SHARED_KINDS = ("nav", "footer")

FIELD_TYPES = (
    "text", "longtext", "number", "currency", "percent", "date", "datetime",
    "email", "url", "select", "boolean",
)


# ── the shape the planner answers in ─────────────────────────────────────────
class FieldSpec(BaseModel):
    name: str = Field(description="snake_case")
    label: str
    type: str = Field(description=" | ".join(FIELD_TYPES))
    options: list[str] = Field(default_factory=list, description="choices, for select only")
    required: bool = False


class CollectionSpec(BaseModel):
    name: str = Field(description="plural snake_case, e.g. tasks")
    label: str
    fields: list[FieldSpec]


class SectionSpec(BaseModel):
    id: str = Field(description="kebab-case, unique across the site")
    kind: str = Field(description=" | ".join(KINDS))
    label: str
    brief: str = Field(description="one sentence: what this section shows")
    collection: str = Field(default="", description="for stats/list/table/form: the collection name")


class RouteSpec(BaseModel):
    path: str = Field(description="starts with /, e.g. / or /tasks")
    title: str
    purpose: str
    sections: list[SectionSpec]


class PlanSpec(BaseModel):
    product_name: str
    collections: list[CollectionSpec]
    routes: list[RouteSpec]


# ── the normalised plan every later pass works to ────────────────────────────
@dataclass
class FieldPlan:
    name: str
    label: str
    type: str
    options: list[str] = field(default_factory=list)
    required: bool = False

    def as_dict(self) -> dict:
        out = {"name": self.name, "label": self.label, "type": self.type}
        if self.options:
            out["options"] = list(self.options)
        if self.required:
            out["required"] = True
        return out


@dataclass
class CollectionPlan:
    name: str
    label: str
    fields: list[FieldPlan]

    def field(self, name: str) -> Optional[FieldPlan]:
        return next((f for f in self.fields if f.name == name), None)

    @property
    def title_field(self) -> FieldPlan:
        """The field a record is called by — its name or title, or the first text."""
        for preferred in ("name", "title", "subject", "label", "headline"):
            found = self.field(preferred)
            if found:
                return found
        return next((f for f in self.fields if f.type == "text"), self.fields[0])

    def as_dict(self) -> dict:
        return {"label": self.label, "fields": [f.as_dict() for f in self.fields]}


@dataclass
class SectionPlan:
    id: str
    kind: str
    label: str
    brief: str
    collection: str = ""
    route: str = ""  # "" for the shared nav/footer

    def as_dict(self) -> dict:
        return {
            "id": self.id,
            "kind": self.kind,
            "label": self.label,
            "brief": self.brief,
            "collection": self.collection,
            "route": self.route,
        }


@dataclass
class RoutePlan:
    path: str
    title: str
    purpose: str
    sections: list[SectionPlan]


@dataclass
class SitePlan:
    product: str
    tagline: str
    collections: list[CollectionPlan]
    routes: list[RoutePlan]
    #: "model" or "fallback", and what normalisation had to change.
    source: str = "model"
    notes: list[str] = field(default_factory=list)

    def collection(self, name: str) -> Optional[CollectionPlan]:
        return next((c for c in self.collections if c.name == name), None)

    @property
    def nav(self) -> SectionPlan:
        return SectionPlan(
            id="site-nav",
            kind="nav",
            label="Navigation",
            brief=f"The {self.product} header: brand, a link to every page, one primary action.",
        )

    @property
    def footer(self) -> SectionPlan:
        return SectionPlan(
            id="site-footer",
            kind="footer",
            label="Footer",
            brief=f"The {self.product} footer: brand line, links to every page, the year.",
        )

    def all_sections(self) -> list[SectionPlan]:
        """Every section in document order: nav, each route's sections, footer."""
        out = [self.nav]
        for route in self.routes:
            out.extend(route.sections)
        out.append(self.footer)
        return out

    def as_dict(self) -> dict:
        return {
            "product": self.product,
            "tagline": self.tagline,
            "source": self.source,
            "notes": list(self.notes),
            "collections": {c.name: c.as_dict() for c in self.collections},
            "routes": [
                {
                    "path": r.path,
                    "title": r.title,
                    "purpose": r.purpose,
                    "sections": [s.id for s in r.sections],
                }
                for r in self.routes
            ],
        }


# ── normalisation ────────────────────────────────────────────────────────────
def _snake(text: object, fallback: str = "items") -> str:
    s = re.sub(r"[^a-z0-9]+", "_", str(text or "").strip().lower()).strip("_")
    if s and s[0].isdigit():
        s = "n_" + s
    return s or fallback


def _kebab(text: object, fallback: str = "section") -> str:
    s = re.sub(r"[^a-z0-9]+", "-", str(text or "").strip().lower()).strip("-")
    return s or fallback


def _path(text: object) -> str:
    raw = str(text or "").strip().split("?")[0].split("#")[-1]
    # Dynamic segments (`/tasks/[id]`, `/tasks/:id`, `/tasks/{id}`) are pages about
    # one record; the mockup shows the collection page instead, so they collapse into
    # their parent.
    parts = [
        _kebab(p, "")
        for p in raw.split("/")
        if p.strip() and p.strip()[0] not in ":[{"
    ]
    parts = [p for p in parts if p and p not in ("id", "slug")]
    return "/" + "/".join(parts)


def _title(text: object) -> str:
    words = re.sub(r"[_\-/]+", " ", str(text or "")).split()
    return " ".join(w[:1].upper() + w[1:] for w in words) or "Home"


def _plural(word: str) -> str:
    if word.endswith("s"):
        return word
    if word.endswith("y") and len(word) > 1 and word[-2] not in "aeiou":
        return word[:-1] + "ies"
    return word + "s"


_TYPE_HINTS: tuple[tuple[str, str], ...] = (
    (r"(email)", "email"),
    (r"(price|cost|amount|total|revenue|budget|salary|fee|balance)", "currency"),
    (r"(percent|rate|progress|ratio)", "percent"),
    (r"(created|updated|date|due|deadline|_at$|time|scheduled|start|end)", "date"),
    (r"(count|qty|quantity|number|age|score|points|rating|stock|views|clicks|size|duration|capacity)", "number"),
    (r"(^is_|^has_|done|active|enabled|completed|published|verified)", "boolean"),
    (r"(url|link|website|image|avatar|photo)", "url"),
    (r"(description|notes|body|content|bio|summary|message|comment|details)", "longtext"),
    (r"(status|type|category|priority|stage|role|level|tier|state|kind|genre|plan)", "select"),
)

_TYPE_WORDS = {
    "int": "number", "integer": "number", "float": "number", "decimal": "number",
    "number": "number", "numeric": "number", "bigint": "number", "double": "number",
    "bool": "boolean", "boolean": "boolean",
    "date": "date", "datetime": "date", "timestamp": "date", "time": "date",
    "text": "longtext", "email": "email", "url": "url", "money": "currency",
    "enum": "select",
}

#: Believable choices for a select whose options nobody specified.
_DEFAULT_OPTIONS = {
    "status": ["Active", "Pending", "Done"],
    "priority": ["Low", "Medium", "High"],
    "stage": ["New", "In progress", "Won", "Lost"],
    "role": ["Admin", "Member", "Viewer"],
    "level": ["Beginner", "Intermediate", "Advanced"],
    "tier": ["Free", "Pro", "Team"],
    "plan": ["Free", "Pro", "Team"],
}


def _hinted_type(name: str) -> Optional[str]:
    for pattern, kind in _TYPE_HINTS:
        if re.search(pattern, name):
            return kind
    return None


def _infer_type(name: str, declared: object = "") -> str:
    """A field's type: a specific declaration wins, a generic one defers to the name.

    "text" or "string" says nothing a name does not say better — `status: text` is a
    choice the list should filter on, `notes: text` is a paragraph, `email: string` is
    an address. A *specific* declaration (`date`, `boolean`, `select`) is kept as given,
    except that a declared number called `price` is still money.
    """
    word = str(declared or "").strip().lower()
    hinted = _hinted_type(name)
    mapped: Optional[str] = word if word in FIELD_TYPES else None
    if mapped is None:
        mapped = next((kind for key, kind in _TYPE_WORDS.items() if word.startswith(key)), None)
    if mapped in (None, "text", "longtext"):
        return hinted or mapped or "text"
    if mapped == "number" and hinted in ("currency", "percent"):
        return hinted
    return mapped


def _normalise_field(spec: FieldSpec | dict, notes: list[str]) -> Optional[FieldPlan]:
    data = spec.model_dump() if isinstance(spec, BaseModel) else dict(spec or {})
    name = _snake(data.get("name"), "")
    if not name or name in ("id", "_id", "uuid", "password", "password_hash", "hashed_password", "token"):
        return None
    kind = _infer_type(name, data.get("type"))
    options = [str(o).strip() for o in (data.get("options") or []) if str(o).strip()][:8]
    if kind == "select" and not options:
        options = _DEFAULT_OPTIONS.get(name.split("_")[-1], [])
        if not options:
            kind = "text"
    return FieldPlan(
        name=name,
        label=str(data.get("label") or _title(name)).strip()[:40],
        type=kind,
        options=options if kind == "select" else [],
        required=bool(data.get("required")),
    )


def _normalise_collection(spec, notes: list[str]) -> Optional[CollectionPlan]:
    data = spec.model_dump() if isinstance(spec, BaseModel) else dict(spec or {})
    name = _snake(data.get("name"), "")
    if not name:
        return None
    seen: set[str] = set()
    fields: list[FieldPlan] = []
    for raw in data.get("fields") or []:
        f = _normalise_field(raw, notes)
        if f and f.name not in seen:
            seen.add(f.name)
            fields.append(f)
    if not fields:
        return None
    fields = fields[:8]
    # A record needs a name to be listed by, and one required field to validate.
    if not any(f.type == "text" for f in fields):
        fields.insert(0, FieldPlan(name="name", label="Name", type="text", required=True))
    title = next((f for f in fields if f.name in ("name", "title")), None) or next(
        f for f in fields if f.type == "text"
    )
    title.required = True
    return CollectionPlan(
        name=_plural(name),
        label=str(data.get("label") or _title(_plural(name))).strip()[:40],
        fields=fields,
    )


def normalise(
    spec: PlanSpec | dict,
    brief,
    *,
    max_routes: int,
    max_sections: int,
    source: str = "model",
) -> SitePlan:
    """Make any plan — the model's or the fallback's — one the later passes can trust."""
    data = spec.model_dump() if isinstance(spec, BaseModel) else dict(spec or {})
    notes: list[str] = []
    max_routes = max(max_routes, 2)
    max_sections = max(max_sections, 2)

    collections: list[CollectionPlan] = []
    for raw in data.get("collections") or []:
        c = _normalise_collection(raw, notes)
        if c and not any(existing.name == c.name for existing in collections):
            collections.append(c)
    if not collections:
        collections = fallback_collections(brief)
        notes.append("no usable collections were planned; derived them from the data model")
    collections = collections[:3]
    names = {c.name for c in collections}

    def resolve_collection(value: object) -> str:
        wanted = _snake(value, "")
        if wanted in names:
            return wanted
        plural = _plural(wanted) if wanted else ""
        if plural in names:
            return plural
        return collections[0].name

    routes: list[RoutePlan] = []
    used_ids: set[str] = set()
    for raw in data.get("routes") or []:
        r = raw if isinstance(raw, dict) else {}
        path = _path(r.get("path"))
        if any(existing.path == path for existing in routes):
            continue
        route = RoutePlan(
            path=path,
            title=str(r.get("title") or _title(path.strip("/") or "Home")).strip()[:40],
            purpose=str(r.get("purpose") or "").strip()[:200],
            sections=[],
        )
        for s in (r.get("sections") or [])[:max_sections]:
            s = s if isinstance(s, dict) else {}
            kind = str(s.get("kind") or "").strip().lower()
            if kind not in KINDS:
                kind = "content"
            sid = _kebab(s.get("id") or s.get("label") or kind)
            if sid in used_ids or sid in ("site-nav", "site-footer"):
                sid = _kebab(f"{route.path.strip('/') or 'home'}-{sid}")
            while sid in used_ids:
                sid = f"{sid}-2"
            used_ids.add(sid)
            route.sections.append(
                SectionPlan(
                    id=sid,
                    kind=kind,
                    label=str(s.get("label") or _title(kind)).strip()[:48],
                    brief=str(s.get("brief") or KINDS[kind]).strip()[:300],
                    collection=resolve_collection(s.get("collection")) if kind in BOUND_KINDS else "",
                    route=route.path,
                )
            )
        if route.sections:
            routes.append(route)
        if len(routes) >= max_routes:
            break

    if not routes or routes[0].path != "/":
        home = next((r for r in routes if r.path == "/"), None)
        if home:
            routes.remove(home)
            routes.insert(0, home)
        else:
            routes.insert(0, _home_route(brief, collections, used_ids))
            notes.append("added a home page")
    routes = routes[:max_routes]

    _guarantee_logic(routes, collections, used_ids, max_routes, max_sections, notes)

    product = str(data.get("product_name") or brief.product).strip()[:60] or brief.product
    return SitePlan(
        product=product,
        tagline=brief.tagline,
        collections=collections,
        routes=routes,
        source=source,
        notes=notes,
    )


def _unique(sid: str, used: set[str]) -> str:
    out = sid
    n = 2
    while out in used:
        out = f"{sid}-{n}"
        n += 1
    used.add(out)
    return out


def _home_route(brief, collections: list[CollectionPlan], used: set[str]) -> RoutePlan:
    main = collections[0]
    return RoutePlan(
        path="/",
        title="Home",
        purpose=f"Introduce {brief.product} and route people into it.",
        sections=[
            SectionPlan(_unique("home-hero", used), "hero", "Hero",
                        f"Introduce {brief.product}: {brief.idea}", route="/"),
            SectionPlan(_unique("home-features", used), "features", "Features",
                        "What the product does, from the requirements' feature list.", route="/"),
            SectionPlan(_unique("home-stats", used), "stats", "At a glance",
                        f"Live figures about the {main.label.lower()}.", main.name, route="/"),
        ],
    )


def _guarantee_logic(
    routes: list[RoutePlan],
    collections: list[CollectionPlan],
    used: set[str],
    max_routes: int,
    max_sections: int,
    notes: list[str],
) -> None:
    """More than one route, one list that filters or sorts, one form that stores.

    Added where they fit best rather than bolted onto the end: the list goes to the
    page named after its collection when there is one, and a new page is only made
    when no existing page can take another section.
    """
    main = collections[0]

    def page_for(collection: CollectionPlan) -> RoutePlan:
        slug = "/" + collection.name.replace("_", "-")
        existing = next((r for r in routes if r.path == slug), None)
        if existing:
            return existing
        room = next((r for r in routes[1:] if len(r.sections) < max_sections), None)
        if room:
            return room
        if len(routes) < max_routes:
            page = RoutePlan(slug, collection.label, f"Browse and manage {collection.label.lower()}.", [])
            routes.append(page)
            notes.append(f"added a {collection.label} page")
            return page
        return routes[-1]

    def list_section(page: RoutePlan) -> SectionPlan:
        return SectionPlan(
            _unique(f"{main.name.replace('_', '-')}-list", used), "list",
            f"All {main.label.lower()}",
            f"Every {main.label.lower()} record, searchable, filterable and sortable.",
            main.name, route=page.path,
        )

    if len(routes) < 2:
        # A second page is made here, so it is given something to show: the list of
        # the main collection, which is also what makes navigating to it worthwhile.
        page = page_for(main)
        if not page.sections:
            page.sections.append(list_section(page))

    has = lambda *kinds: any(s.kind in kinds for r in routes for s in r.sections)  # noqa: E731

    if not has("list", "table"):
        page = page_for(main)
        page.sections.insert(0 if page.path != "/" else len(page.sections), list_section(page))
        notes.append("added a filterable list")

    if not has("form"):
        page = page_for(main)
        page.sections.append(
            SectionPlan(_unique(f"new-{main.name.replace('_', '-')}", used), "form",
                        f"Add {main.label.lower()}",
                        f"Create a new {main.label.lower()} record; it appears in the list at once.",
                        main.name, route=page.path),
        )
        notes.append("added a form that stores records")

    routes[:] = [r for r in routes if r.sections]
    for route in routes:
        for section in route.sections:
            section.route = route.path


def site_from_data(data: dict) -> SitePlan:
    """The plan a site document recorded about itself, for checking a later edit."""
    collections = []
    for name, spec in (data.get("collections") or {}).items():
        spec = spec if isinstance(spec, dict) else {}
        fields = [
            FieldPlan(
                name=str(f.get("name")),
                label=str(f.get("label") or f.get("name")),
                type=str(f.get("type") or "text"),
                options=[str(o) for o in (f.get("options") or [])],
                required=bool(f.get("required")),
            )
            for f in spec.get("fields") or []
            if isinstance(f, dict) and f.get("name")
        ]
        if fields:
            collections.append(CollectionPlan(name=str(name), label=str(spec.get("label") or name), fields=fields))
    sections = [s for s in data.get("sections") or [] if isinstance(s, dict)]
    routes = []
    for r in data.get("routes") or []:
        if not isinstance(r, dict) or not r.get("path"):
            continue
        path = str(r["path"])
        routes.append(
            RoutePlan(
                path=path,
                title=str(r.get("title") or path),
                purpose="",
                sections=[
                    SectionPlan(
                        id=str(s.get("id")),
                        kind=str(s.get("kind") or "content"),
                        label=str(s.get("label") or s.get("id")),
                        brief=str(s.get("brief") or ""),
                        collection=str(s.get("collection") or ""),
                        route=path,
                    )
                    for s in sections
                    if s.get("route") == path
                ],
            )
        )
    return SitePlan(
        product=str(data.get("product") or ""),
        tagline=str(data.get("tagline") or ""),
        collections=collections,
        routes=routes,
        source="document",
    )


def section_from_data(data: dict, section_id: str) -> Optional[SectionPlan]:
    for s in data.get("sections") or []:
        if isinstance(s, dict) and s.get("id") == section_id:
            return SectionPlan(
                id=section_id,
                kind=str(s.get("kind") or "content"),
                label=str(s.get("label") or section_id),
                brief=str(s.get("brief") or ""),
                collection=str(s.get("collection") or ""),
                route=str(s.get("route") or ""),
            )
    return None


# ── the fallback: a plan from state alone ────────────────────────────────────
def fallback_collections(brief) -> list[CollectionPlan]:
    """Collections from the System Design data model, or one shaped by the idea."""
    out: list[CollectionPlan] = []
    for entity in brief.entities[:3]:
        fields = []
        for raw in entity.get("fields") or []:
            text = str(raw)
            name, _, declared = text.partition(":")
            fields.append({"name": name, "type": declared, "label": _title(name)})
        c = _normalise_collection({"name": entity.get("entity") or entity.get("name"), "fields": fields}, [])
        if c and not any(existing.name == c.name for existing in out):
            out.append(c)
    if out:
        return out
    noun = _snake(brief.main_noun or "items")
    return [
        CollectionPlan(
            name=_plural(noun),
            label=_title(_plural(noun)),
            fields=[
                FieldPlan("name", "Name", "text", required=True),
                FieldPlan("category", "Category", "select", ["General", "Priority", "Archive"]),
                FieldPlan("status", "Status", "select", ["Active", "Pending", "Done"]),
                FieldPlan("created_at", "Created", "date"),
                FieldPlan("notes", "Notes", "longtext"),
            ],
        )
    ]


def fallback_spec(brief, max_routes: int) -> dict:
    """A plan built from the Frontend Engineer's pages and the PM's features."""
    collections = [
        {"name": c.name, "label": c.label, "fields": [f.as_dict() for f in c.fields]}
        for c in fallback_collections(brief)
    ]
    main = collections[0]["name"]
    routes = [
        {
            "path": "/",
            "title": "Home",
            "purpose": brief.idea,
            "sections": [
                {"id": "home-hero", "kind": "hero", "label": "Hero", "brief": brief.idea},
                {"id": "home-features", "kind": "features", "label": "Features",
                 "brief": "The product's key capabilities."},
                {"id": "home-stats", "kind": "stats", "label": "At a glance",
                 "brief": "Live figures from the data.", "collection": main},
                {"id": "home-cta", "kind": "cta", "label": "Get started",
                 "brief": "Invite the visitor into the product."},
            ],
        }
    ]
    for page in brief.pages:
        path = _path(page.get("route") or page.get("path"))
        if path == "/" or any(r["path"] == path for r in routes):
            continue
        purpose = str(page.get("purpose") or "")
        words = f"{path} {purpose}".lower()
        target = next(
            (c["name"] for c in collections if c["name"].rstrip("s") in words), main
        )
        if re.search(r"(new|create|add|submit|post|signup|sign-up|register|apply|book)", words):
            sections = [{"id": f"{_kebab(path)}-form", "kind": "form", "label": _title(path),
                         "brief": purpose, "collection": target}]
        else:
            sections = [{"id": f"{_kebab(path)}-list", "kind": "list", "label": _title(path),
                         "brief": purpose, "collection": target}]
        routes.append({"path": path, "title": _title(path.strip("/")), "purpose": purpose,
                       "sections": sections})
        if len(routes) >= max_routes:
            break
    return {"product_name": brief.product, "collections": collections, "routes": routes}


# ── the prompt ───────────────────────────────────────────────────────────────
SYSTEM = (
    "You are a product designer planning a clickable prototype of a web app. You decide "
    "which pages it has, which sections each page shows, and what records the app stores. "
    "You reply with ONLY a JSON object matching the requested shape."
)


def instructions(max_routes: int, max_sections: int) -> str:
    kinds = "\n".join(f'- "{k}": {v}' for k, v in KINDS.items())
    return (
        f"Plan between 2 and {max_routes} pages. The first page is \"/\" (home). Give each "
        f"page 2 to {max_sections} sections. The shared header and footer are added for you — "
        "do not plan them.\n\n"
        f"Section kinds:\n{kinds}\n\n"
        "Rules:\n"
        "- Plan 1 to 3 collections — the records this product manages — with 4 to 7 fields "
        "each. Field types: " + ", ".join(FIELD_TYPES) + ". Give select fields their options.\n"
        "- Every stats/list/table/form section names one of those collections.\n"
        "- Include at least one list or table AND at least one form, so the prototype can "
        "create, browse, search, filter and sort records.\n"
        "- Pages come from the Frontend Engineer's page list when there is one; sections "
        "cover the product manager's P0 features.\n"
        "- Section ids are kebab-case and unique across the whole site."
    )


def iter_briefs(sections: Iterable[SectionPlan]) -> str:
    return "\n".join(f"- {s.id} ({s.kind}): {s.brief}" for s in sections)
