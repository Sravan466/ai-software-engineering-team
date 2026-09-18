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

#: A verdict longer than this is not a verdict. `debate._parse` falls back to
#: putting the model's entire raw reply in `decision` when the JSON will not parse,
#: and scanning a page of prose for technology names finds several of them.
_MAX_VERDICT_CHARS = 400

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


def binding_on(phase_key: str, stored: object) -> Optional["Charter"]:
    """The charter this phase is held to — which is *none* for System Design.

    System Design is the phase that decides the stack, so handing it the charter
    frozen from its own previous attempt would tell the architect that the
    architecture is already settled. On a redo that is exactly backwards: the
    reviewer sent it back to change something, and the thing they are most likely
    changing is a technology choice.
    """
    if phase_key == Phase.SYSTEM_DESIGN.value:
        return None
    return Charter.from_dict(stored)


#: Which categories are checked against which phase's output, and it is a short
#: list on purpose.
#:
#: The charter is *printed* to all six categories — every agent is told the whole
#: stack. Only these are allowed to **fail a phase**, because only these convict on
#: evidence that is unambiguous in a real repository. The ones deliberately absent:
#:
#:   `language` and `package_manager` — almost every build here is polyglot. A
#:   FastAPI backend beside a React frontend legitimately contains `.tsx` files and
#:   an `npm ci` line, and a check that reads those as contradicting a Python
#:   charter tells a correct agent to "rewrite it against pip", which is not an
#:   instruction anyone can follow.
#:
#:   `test_runner` as a token comparison — same reason. What a test suite must not
#:   do is far narrower, and `_foreign_language_tests` below says exactly what.
#:
#:   `database` on QA — a `sqlite:///:memory:` fixture under a Postgres charter is
#:   ordinary practice, and a QA suite importing the wrong driver only happens when
#:   the backend already did, which the backend's own check catches first.
#:
#: A false positive costs more than a missed contradiction: it fails work that was
#: correct, and it does it in a gate that stops the run.
ENFORCED: dict[str, tuple[str, ...]] = {
    Phase.BACKEND_ENGINEER.value: ("database", "backend_framework"),
    Phase.FRONTEND_ENGINEER.value: ("frontend_framework",),
    Phase.DEVOPS_ENGINEER.value: ("database",),
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

    # 1. the debate's verdict — and only for the question the debate was asked.
    #
    #    Two things make this the most dangerous input in the function, and both are
    #    handled here rather than in the matcher. The decision is *prose*, and the
    #    alias lists contain ordinary English words: "We should go with PostgreSQL"
    #    froze the language as Go, "gives us guard rails" froze the backend as Rails,
    #    "lets the team express relationships" froze it as Express. And because the
    #    debate outranks the architecture, each of those then convicted correct code
    #    and stopped the run.
    #
    #    So the verdict is read for the database alone, which is what the debate is
    #    convened to settle, and whose names ("postgresql", "mongodb", "mariadb") are
    #    not words anyone writes by accident. Anything else it may have opined on is
    #    the architecture's to decide.
    if isinstance(debate, dict):
        decision = debate.get("decision")
        if isinstance(decision, str) and 0 < len(decision.strip()) <= _MAX_VERDICT_CHARS:
            record("database", stack.name_to_choice("database", decision), SOURCE_DEBATE)

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
    if not charter:
        return []
    if phase_key == Phase.QA_ENGINEER.value:
        # The test suite gets its own check, not a category comparison. See
        # `_foreign_language_tests` for why the obvious version fails correct work.
        return _foreign_language_tests(charter, output)
    if phase_key not in ENFORCED:
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


#: Always allowed in a web build's test suite. Every frontend framework this
#: pipeline knows is JavaScript or TypeScript, and a build whose architecture named
#: no frontend at all still had a Frontend Engineer write one — so treating JS as
#: foreign because the charter did not mention it would fail a correct test suite.
#: The check is for a test in a language the build does not contain *at all*, and
#: erring towards a miss is the right direction: a missed contradiction costs a
#: reader's attention, a false one fails work that was right.
_ALWAYS_PRESENT = frozenset({"javascript", "typescript"})


def _build_languages(charter: Charter) -> frozenset[str]:
    """The languages this build legitimately contains.

    The charter's backend language, plus the web languages every build here has a
    front end in. A Python backend with a React front end is a Python *and* a
    TypeScript repository, and treating it as one language is how a correct
    `.tsx` test gets reported as a contradiction.
    """
    backend = charter.get("language")
    if not backend:
        return frozenset()
    return stack.language_family(backend.token) | _ALWAYS_PRESENT


def _foreign_language_tests(charter: Charter, output: object) -> list[str]:
    """Test files written in a language this build does not contain.

    This is the narrow, high-precision version of "the tests match the stack", and
    it is the one the reported failure actually needs: a run whose backend was
    Express and whose QA phase produced `tests/test_auth_controller.py` aimed at
    `authController.js`. Those tests cannot run, cannot pass, and cannot be fixed by
    anyone reading the archive.

    Comparing test-runner *names* instead would fail every polyglot build, where a
    Python backend and a React front end each correctly bring their own runner.
    """
    languages = _build_languages(charter)
    if not languages:
        return []
    from app.core.artifacts import iter_files

    found: list[str] = []
    reported: set[str] = set()
    for path, content, _lang in iter_files(output if isinstance(output, dict) else {}):
        detected = stack.detect("language", path, content)
        if detected is None or detected.token in languages:
            continue
        if detected.token in reported:
            continue
        reported.add(detected.token)
        written_in = charter.get("language")
        spoken = written_in.label if written_in else "the language this build uses"
        found.append(
            f"`{path}` — is a {detected.label} test, and nothing in this build is "
            f"written in {detected.label}. It is aimed at {spoken} code it cannot "
            f"import or run. Rewrite the suite in {spoken}."
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
