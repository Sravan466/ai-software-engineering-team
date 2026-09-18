"""Pass 4 — seed content: records the mockup's lists, tables and figures are made of.

A populated product instead of two paragraphs and a `<pre>` block. The model writes
believable rows for each collection the plan declared; every value is then coerced
to its field's type, so a price arrives as a number the runtime can sort and total,
a date as a date it can format, and a status as one of the options the filter offers.

What the model cannot or will not fill is generated here, deterministically from the
field's name and type — the lists are never empty because a call failed.
"""
from __future__ import annotations

import hashlib
import re
from datetime import date, datetime, timedelta
from typing import Optional

from app.core.reading import as_number
from app.preview.plan import CollectionPlan, FieldPlan, SitePlan

#: Character estimates per value, used to size the row count to the output budget
#: of the model actually chosen — never a fixed number of rows for every model.
_VALUE_CHARS = {
    "text": 26, "longtext": 110, "email": 30, "url": 40, "date": 14, "datetime": 22,
    "number": 6, "currency": 8, "percent": 5, "select": 14, "boolean": 6,
}

_PEOPLE = (
    "Maya Chen", "Daniel Okafor", "Priya Raman", "Lucas Moreau", "Aisha Bello",
    "Tomás García", "Hannah Kim", "Omar Haddad", "Sofia Rossi", "Ethan Walker",
    "Leila Nasser", "Kenji Watanabe", "Grace Mensah", "Noah Fischer", "Isabel Duarte",
)
_PEOPLE_WORDS = (
    "user", "member", "customer", "client", "contact", "employee", "student", "patient",
    "author", "attendee", "person", "people", "candidate", "lead", "owner", "guest",
    "player", "volunteer", "subscriber", "teacher", "instructor", "agent", "driver",
)
_ADJECTIVES = (
    "Quarterly", "Onboarding", "Northside", "Evergreen", "Launch", "Harbor", "Summit",
    "Atlas", "Weekly", "Riverside", "Pilot", "Studio", "Horizon", "Morning", "Core",
)
_SENTENCES = (
    "Kicked off with the whole team and agreed the first milestones.",
    "Waiting on a final review before it goes live next week.",
    "Customer asked for a follow-up call to walk through the details.",
    "Moved up after the planning session — now the top priority.",
    "Draft is ready; needs one more pass on the numbers.",
    "Blocked on access to the staging environment.",
)


class SeedSpecError(ValueError):
    """The model's rows could not be read at all."""


def row_budget(plan: SitePlan, output_chars: int, wanted: int) -> int:
    """How many rows per collection fit in one reply from the chosen model."""
    per_row = 0
    for c in plan.collections:
        per_row += sum(_VALUE_CHARS.get(f.type, 20) + len(f.name) + 6 for f in c.fields) + 4
    if per_row <= 0:
        return max(wanted, 1)
    fits = int(output_chars * 0.8) // per_row
    return max(3, min(wanted, fits))


def response_schema(plan: SitePlan, rows: int) -> dict:
    """A JSON Schema for exactly these collections, for constrained decoding."""
    def prop(f: FieldPlan) -> dict:
        if f.type in ("number", "currency", "percent"):
            return {"type": "number"}
        if f.type == "boolean":
            return {"type": "boolean"}
        if f.type == "select" and f.options:
            return {"type": "string", "enum": list(f.options)}
        return {"type": "string"}

    return {
        "type": "object",
        "properties": {
            c.name: {
                "type": "array",
                "minItems": min(rows, 3),
                "items": {
                    "type": "object",
                    "properties": {f.name: prop(f) for f in c.fields},
                    "required": [f.name for f in c.fields],
                },
            }
            for c in plan.collections
        },
        "required": [c.name for c in plan.collections],
    }


SYSTEM = (
    "You write realistic sample data for a product prototype: specific names, plausible "
    "numbers, recent dates, varied statuses. Never lorem ipsum, never 'Item 1'. You reply "
    "with ONLY a JSON object matching the requested shape."
)


def instructions(plan: SitePlan, rows: int, today: date) -> str:
    lines = []
    for c in plan.collections:
        fields = ", ".join(
            f"{f.name} ({f.type}{': ' + '|'.join(f.options) if f.options else ''})" for f in c.fields
        )
        lines.append(f"- {c.name} — {c.label}: {fields}")
    return (
        f"Write {rows} records for each collection below. Today is {today.isoformat()}; dates "
        "are ISO (YYYY-MM-DD) and fall within a few months of today. Money is a plain number "
        "without a currency symbol. Make the records varied so filtering and sorting them "
        "shows something.\n\nCollections:\n" + "\n".join(lines)
    )


# ── coercion ─────────────────────────────────────────────────────────────────
def _h(*parts: object) -> int:
    return int(hashlib.sha256("|".join(str(p) for p in parts).encode()).hexdigest()[:8], 16)


def _is_people(collection: CollectionPlan) -> bool:
    name = collection.name.lower()
    return any(word in name for word in _PEOPLE_WORDS)


def _parse_date(value: object) -> Optional[str]:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text[:19].replace("Z", "")).date().isoformat()
    except ValueError:
        pass
    for fmt in ("%d/%m/%Y", "%m/%d/%Y", "%B %d, %Y", "%b %d, %Y", "%d %B %Y"):
        try:
            return datetime.strptime(text, fmt).date().isoformat()
        except ValueError:
            continue
    match = re.match(r"(\d{4})-(\d{2})-(\d{2})", text)
    if match:
        try:
            return date(int(match.group(1)), int(match.group(2)), int(match.group(3))).isoformat()
        except ValueError:
            return None
    return None


def generated(field: FieldPlan, collection: CollectionPlan, index: int, today: date) -> object:
    """A believable value for one field of one row, from its name and type alone."""
    name = field.name
    seed = _h(collection.name, name, index)
    kind = field.type
    if kind == "select":
        return field.options[(index + seed) % len(field.options)] if field.options else ""
    if kind == "boolean":
        return (index + seed) % 3 != 0
    if kind in ("date", "datetime"):
        ahead = re.search(r"(due|deadline|start|end|scheduled|expires|event|appointment)", name)
        offset = (seed % 40) + index
        day = today + timedelta(days=offset) if ahead else today - timedelta(days=offset)
        return day.isoformat()
    if kind == "currency":
        base = 1200 if re.search(r"(revenue|budget|salary|total|balance)", name) else 49
        return float(round(base * (1 + (seed % 90) / 10), 2 if base < 100 else 0))
    if kind == "percent":
        return 5 + seed % 95
    if kind == "number":
        if "rating" in name or "stars" in name:
            return 1 + seed % 5
        if re.search(r"(point|estimate)", name):
            return (1, 2, 3, 5, 8, 13)[seed % 6]
        return 1 + seed % 48
    if kind == "email":
        person = _PEOPLE[(index + seed) % len(_PEOPLE)].lower().split()
        return f"{person[0]}.{person[-1]}@example.com"
    if kind == "url":
        return f"https://example.com/{collection.name}/{index + 1}"
    if kind == "longtext":
        return _SENTENCES[(index + seed) % len(_SENTENCES)]
    if name in ("name", "full_name", "owner", "assignee", "author") or (
        name in ("title",) and _is_people(collection)
    ) or (_is_people(collection) and field is collection.title_field):
        return _PEOPLE[(index * 3 + seed) % len(_PEOPLE)]
    noun = collection.label.rstrip("s") if collection.label.endswith("s") else collection.label
    return f"{_ADJECTIVES[(index + seed) % len(_ADJECTIVES)]} {noun.lower()}".capitalize()


def coerce(value: object, field: FieldPlan) -> Optional[object]:
    """The value as its field's type, or None when it cannot be one."""
    kind = field.type
    if value is None:
        return None
    if kind in ("number", "currency", "percent"):
        number = as_number(value) if isinstance(value, str) else value
        if isinstance(number, bool) or not isinstance(number, (int, float)):
            return None
        return int(number) if float(number).is_integer() and kind != "currency" else float(number)
    if kind == "boolean":
        if isinstance(value, bool):
            return value
        text = str(value).strip().lower()
        if text in ("true", "yes", "1", "y"):
            return True
        if text in ("false", "no", "0", "n"):
            return False
        return None
    if kind in ("date", "datetime"):
        return _parse_date(value)
    text = " ".join(str(value).split())
    if not text:
        return None
    if kind == "select" and field.options:
        lowered = {o.lower(): o for o in field.options}
        if text.lower() in lowered:
            return lowered[text.lower()]
        close = next((o for o in field.options if o.lower() in text.lower() or text.lower() in o.lower()), None)
        return close
    if kind == "email" and "@" not in text:
        return None
    return text[:280]


def seed_rows(
    plan: SitePlan, raw: Optional[dict], rows: int, today: Optional[date] = None
) -> tuple[dict[str, list[dict]], dict[str, str]]:
    """Every collection's rows, and where each came from ("model", "mixed", "generated")."""
    today = today or date.today()
    raw = raw if isinstance(raw, dict) else {}
    out: dict[str, list[dict]] = {}
    sources: dict[str, str] = {}
    for c in plan.collections:
        given = raw.get(c.name)
        given = [r for r in given if isinstance(r, dict)] if isinstance(given, list) else []
        records: list[dict] = []
        filled = 0
        for index in range(rows):
            source = given[index] if index < len(given) else {}
            record: dict = {}
            for f in c.fields:
                value = coerce(source.get(f.name), f)
                if value is None:
                    value = generated(f, c, index, today)
                    filled += 1
                record[f.name] = value
            records.append(record)
        out[c.name] = records
        total = len(records) * max(len(c.fields), 1)
        sources[c.name] = "generated" if not given else ("mixed" if filled > total // 4 else "model")
    return out, sources
