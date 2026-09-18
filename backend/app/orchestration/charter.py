"""The stack charter: one set of technology decisions the whole crew is held to.

The debate step existed to settle the database question and ran immediately before
the Backend Engineer — who ignored its verdict, because the verdict arrived as free
prose in `extra_context` with no obligation attached. Downstream of that, System
Design said PostgreSQL and React, Backend wrote Mongoose, Frontend built Next.js,
and QA wrote pytest files against `.js` controllers. Every phase was plausible on its
own and `artifacts.assemble` shipped all of it in one `.zip`.

A charter closes that. It is frozen once, after the architecture is settled, and from
then on it is:

  **printed** — injected verbatim into every downstream system prompt, so no agent
  has to infer the stack from a wall of prior-phase JSON; and

  **enforced** — an agent that writes Mongoose under a PostgreSQL charter has failed
  its phase, in exactly the way an agent that misses its declared shape has. It is
  sent back with the contradiction named, in the same repair round, and if it comes
  back contradicting the charter again the run stops for a person rather than letting
  the `.zip` carry two incompatible halves.

What it is *not* is a guess. Every entry traces to something that was actually
decided: the debate's verdict, the architecture's own `tech_stack`, or an unavoidable
consequence of one of those (FastAPI means Python; Python means pip). A category that
nothing settled has no entry, and nothing is enforced for it.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Mapping, Optional

from app.core.constants import Phase
from app.core.logging import get_logger
from app.orchestration import stack

log = get_logger(__name__)

#: Where a charter entry came from, in the order of authority. The debate is the
#: settled decision; the architecture is what the designer wrote; an implication is
#: this application joining two dots nobody disputed.
SOURCE_DEBATE = "debate"
SOURCE_DESIGN = "system_design"
SOURCE_IMPLIED = "implied"

_SOURCE_PHRASE = {
    SOURCE_DEBATE: "settled by the team debate",
    SOURCE_DESIGN: "chosen by the architecture",
    SOURCE_IMPLIED: "follows from the choices above",
}

#: Which categories are checked against which phase's output. Scoped, because the
#: evidence only means something in context: a `.py` file is a contradiction in a
#: JavaScript project's *test suite* and completely normal in a Python backend, and
#: a check that cannot tell those apart fails correct work.
ENFORCED: dict[str, tuple[str, ...]] = {
    Phase.BACKEND_ENGINEER.value: ("database", "backend_framework", "language"),
    Phase.FRONTEND_ENGINEER.value: ("frontend_framework",),
    Phase.QA_ENGINEER.value: ("test_runner", "database"),
    Phase.DEVOPS_ENGINEER.value: ("database", "package_manager"),
}


@dataclass(frozen=True)
class Choice:
    """One frozen decision: what it is, what it was called, and who decided it."""

    token: str
    label: str
    source: str

    def as_dict(self) -> dict:
        return {"token": self.token, "label": self.label, "source": self.source}


@dataclass(frozen=True)
class Charter:
    """The decisions, keyed by category. Immutable once the run has frozen one."""

    choices: Mapping[str, Choice]

    # ── construction ─────────────────────────────────────────────────────────
    @classmethod
    def from_dict(cls, data: object) -> Optional["Charter"]:
        """Rebuild a charter from the pipeline state or the database row.

        Returns None for anything that is not a charter, so a run started before
        charters existed simply has none rather than half of one.
        """
        if not isinstance(data, dict):
            return None
        choices: dict[str, Choice] = {}
        for category, entry in data.items():
            if category not in stack.CATEGORY_LABELS or not isinstance(entry, dict):
                continue
            token = entry.get("token")
            if not isinstance(token, str) or not token:
                continue
            choices[category] = Choice(
                token=token,
                label=str(entry.get("label") or stack.label_for(token)),
                source=str(entry.get("source") or SOURCE_DESIGN),
            )
        return cls(choices) if choices else None

    def as_dict(self) -> dict:
        return {category: choice.as_dict() for category, choice in self.choices.items()}

    def __bool__(self) -> bool:
        return bool(self.choices)

    def get(self, category: str) -> Optional[Choice]:
        return self.choices.get(category)

    # ── what the agents are shown ────────────────────────────────────────────
    def prompt_block(self) -> str:
        """The charter as it appears in a system prompt — the same text every time.

        Written as instructions rather than as data. An agent handed a table of
        technologies with no verb attached treats it as context to consider; this
        has to read as the constraint it is.
        """
        if not self.choices:
            return ""
        lines = [
            f"- {stack.CATEGORY_LABELS[category]}: {self.choices[category].label}"
            for category, _ in stack.CATEGORIES
            if category in self.choices
        ]
        return (
            "STACK CHARTER — frozen when the architecture was approved, and binding "
            "on every phase after it:\n"
            + "\n".join(lines)
            + "\n\nWrite every file against exactly this stack. Do not substitute an "
            "equivalent — another team is writing the tests, the deployment and the "
            "security review against these same choices, and a substitution silently "
            "breaks their work. If you think a different choice is better, say so in "
            "your summary and still write the code against the charter."
        )

    def summary_line(self) -> str:
        """One line naming the stack, for a log or a note to a person."""
        return " · ".join(
            f"{stack.CATEGORY_LABELS[category]}: {self.choices[category].label}"
            for category, _ in stack.CATEGORIES
            if category in self.choices
        )


# ── freezing one ─────────────────────────────────────────────────────────────
def _strings(value: object) -> list[str]:
    """A list of strings out of whatever an agent put in a `tech_stack` field."""
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [str(v) for v in value.values()]
    if isinstance(value, (list, tuple)):
        out: list[str] = []
        for item in value:
            out.extend(_strings(item))
        return out
    return []


def _tech_stack(design: object) -> dict[str, list[str]]:
    """The architecture's `tech_stack`, read by normalised key and flattened."""
    from app.orchestration.approval import read_key  # local: approval imports nothing here

    block = read_key(design, "tech_stack", "techStack", "stack")
    if not isinstance(block, dict):
        return {}
    return {
        area: _strings(read_key(block, area))
        for area in ("frontend", "backend", "database", "infra")
    }


def freeze(design_output: object, debate: Optional[dict] = None) -> Optional[Charter]:
    """Derive the charter from the settled architecture, plus the debate's verdict.

    Order of authority, highest first:

      1. **the debate** — it exists to settle a question, and a verdict that loses to
         the very phase it was run before is not a verdict at all. It now runs before
         System Design, so normally the two agree and this is a no-op; when they do
         not, the decision the team recorded is the one that binds.
      2. **the architecture's own `tech_stack`** — what the designer actually chose.
      3. **implication** — FastAPI means Python, Python means pytest and pip. Filled
         in only where nothing above said anything, and marked as such, because an
         implied choice is a weaker claim than a stated one.
    """
    chosen: dict[str, Choice] = {}

    def record(category: str, choice: Optional[stack.Choice], source: str) -> None:
        if choice is None or category in chosen:
            return
        chosen[category] = Choice(token=choice.token, label=choice.label, source=source)

    # 1. the debate's verdict, read from the decision only — the rationale weighs
    #    alternatives out loud, and a mention there is not a choice.
    if isinstance(debate, dict):
        decision = debate.get("decision")
        if isinstance(decision, str) and decision.strip():
            for category, _ in stack.CATEGORIES:
                record(category, stack.name_to_choice(category, decision), SOURCE_DEBATE)

    # 2. the architecture. Each category is read from the areas that can name it, so
    #    "Node.js" under `backend` settles the language and "React" under `frontend`
    #    cannot be mistaken for one.
    tech = _tech_stack(design_output)
    everything = [v for values in tech.values() for v in values]
    for category, areas in (
        ("database", ("database", "backend")),
        ("backend_framework", ("backend",)),
        ("frontend_framework", ("frontend",)),
        ("language", ("backend",)),
        ("test_runner", ()),
        ("package_manager", ()),
    ):
        values = [v for area in areas for v in tech.get(area, ())] or (
            everything if not areas else []
        )
        record(category, stack.names_to_choice(category, values), SOURCE_DESIGN)

    # 3. what those choices settle on their own, most specific decision first.
    for category in ("backend_framework", "frontend_framework", "language", "test_runner"):
        entry = chosen.get(category)
        source = stack.BY_TOKEN.get(entry.token) if entry else None
        if source is None:
            continue
        for implied_category, token in stack.implications(source).items():
            record(implied_category, stack.BY_TOKEN.get(token), SOURCE_IMPLIED)

    if not chosen:
        log.warning(
            "The architecture named no technology this pipeline recognises, so no "
            "stack charter was frozen and nothing downstream will be checked against "
            "one. The run continues; the build is on its own for consistency."
        )
        return None

    charter = Charter(chosen)
    log.info("Stack charter frozen — %s", charter.summary_line())
    return charter


# ── enforcing one ────────────────────────────────────────────────────────────
def violations(charter: Optional[Charter], phase_key: str, output: object) -> list[str]:
    """Where `output` contradicts the charter, as lines an agent can act on.

    Each line names the file and both choices, because "this violates the charter" is
    not something a model can fix and "`models/user.js` uses MongoDB where the charter
    says PostgreSQL" is. The wording matches the shape-validation errors these are
    merged with, so the repair round reads as one list of problems rather than two.
    """
    if not charter or phase_key not in ENFORCED:
        return []
    from app.core.artifacts import iter_files  # local: artifacts imports db models

    categories = [c for c in ENFORCED[phase_key] if c in charter.choices]
    if not categories:
        return []

    found: list[str] = []
    #: One line per (category, offending choice) rather than per file — a 40-file
    #: Mongo backend is one decision made forty times, and forty identical lines
    #: would crowd every real problem out of the repair prompt.
    reported: set[tuple[str, str]] = set()

    for path, content, _lang in iter_files(output if isinstance(output, dict) else {}):
        for category in categories:
            expected = charter.choices[category]
            detected = stack.detect(category, path, content)
            if detected is None or stack.satisfies(expected.token, detected.token):
                continue
            if (category, detected.token) in reported:
                continue
            reported.add((category, detected.token))
            found.append(
                f"`{path}` — uses {detected.label} where the stack charter says the "
                f"{stack.CATEGORY_LABELS[category].lower()} is {expected.label}"
                f" ({_SOURCE_PHRASE.get(expected.source, 'agreed for this build')}). "
                f"Rewrite it, and every other file like it, against {expected.label}."
            )
    return found


def note(found: Iterable[str]) -> Optional[str]:
    """The one-line version a reviewer reads on a stopped run."""
    lines = list(found)
    if not lines:
        return None
    count = len(lines)
    subject = "contradiction" if count == 1 else "contradictions"
    return (
        f"This phase still contradicts the stack charter in {count} {subject} after "
        "being sent back. Shipping it would put two incompatible halves in one "
        "archive — read them below before approving."
    )
