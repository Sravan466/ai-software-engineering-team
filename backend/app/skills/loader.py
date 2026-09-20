"""Reading a skill off disk: one `SKILL.md`, frontmatter and body, checked as it loads.

A skill is procedural knowledge that is true across projects — how to write an
acceptance criterion that can be tested, what a paginated list endpoint returns —
and it reaches the agent as text in its prompt. That is the only delivery mechanism
that behaves identically on `qwen2.5:7b` and on Claude, GPT or Gemini: all four
providers in `app/router/providers/` take the same `list[ChatMessage]`, so an
injected block is byte-identical across them. Provider tool-calling is not, which is
why nothing here builds one.

Two rules are enforced *here*, at load, rather than left to whoever reviews a pull
request — because a skill that breaks either one is worse than no skill at all on
the local path, which is this project's default:

  **A length ceiling.** Everything selected is paid for on every phase; there is no
  second level that loads lazily. A 9,000-character skill is 9,000 characters that
  RAG, memory and the prior phase outputs no longer get.

  **No format instructions.** Since every agent answers in a declared JSON shape, a
  skill body that says "present this as a table" competes with that shape — and on a
  small model the prose instruction wins, buying a repair round and sometimes an
  invalid phase. Skills shape *content*. The shape is the schema's job.

A skill that fails either check is still *loaded* and still listed, with the reason
attached; it is simply never selected. Silently dropping it would leave a file on
disk, visible in the library, that does nothing and says nothing about why.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from app.core.logging import get_logger

log = get_logger(__name__)

#: The file inside a skill directory. One directory per skill, so a skill can grow
#: files beside it later without the format changing.
SKILL_FILE = "SKILL.md"

#: What a skill may be called: a slug, because it is also a directory name, a URL
#: path segment and the token a build pins or excludes by.
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,63}$")

#: Phrases that tell a model how to *shape its reply*. Deliberately narrow: a skill
#: about API design has every right to say "JSON body" or "the response envelope",
#: and banning the word outright would make the library unable to describe the thing
#: it exists to describe. What is banned is the second-person directive — the one
#: that competes with the schema the agent is already decoding against.
_FORMAT_RULES: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\brespond (with|in|using|only)\b", re.I), "tells the agent how to respond"),
    (re.compile(r"\breply (with|in|using)\b", re.I), "tells the agent how to reply"),
    (re.compile(r"\banswer (with|in|using)\b", re.I), "tells the agent how to answer"),
    (
        re.compile(r"\b(format|structure|present|return|write) (your|the) (answer|reply|response|output)\b", re.I),
        "dictates the shape of the agent's output",
    ),
    (re.compile(r"\boutput format\b", re.I), "dictates an output format"),
    (re.compile(r"\bmarkdown\b", re.I), "names a rendering format"),
    (re.compile(r"\bcode (block|fence)s?\b", re.I), "names a rendering format"),
    (re.compile(r"\bbullet(ed)? (point|list)s?\b", re.I), "dictates a layout"),
    (re.compile(r"\bnumbered list\b", re.I), "dictates a layout"),
    (re.compile(r"\bas a table\b", re.I), "dictates a layout"),
)


@dataclass(frozen=True)
class Skill:
    """One piece of procedural knowledge, and everything needed to place it."""

    name: str
    title: str
    description: str
    #: Which phases may receive it. Empty means every phase.
    agents: tuple[str, ...] = ()
    #: What makes it relevant to a particular build.
    keywords: tuple[str, ...] = ()
    body: str = ""
    #: `bundled` (shipped in this repo) or `user` (added on this machine).
    source: str = "bundled"
    path: str = ""
    #: Why this skill may not be injected, or empty when it may be. A skill that
    #: breaks a rule stays in the library with its reason showing — the alternative
    #: is a file that exists, does nothing, and never says so.
    problems: tuple[str, ...] = field(default_factory=tuple)

    @property
    def usable(self) -> bool:
        return not self.problems

    @property
    def chars(self) -> int:
        """What this skill costs a prompt: the whole injected block, not just the body.

        The title and the description are injected too, so counting the body alone
        under-reports what every phase that selects this skill actually pays.
        """
        return len(render(self))

    def serves(self, phase: str) -> bool:
        """Whether this skill is bound to `phase`. An empty list serves all of them."""
        return not self.agents or phase in self.agents


def parse(text: str) -> tuple[dict, str]:
    """Split `---`-delimited frontmatter from the body.

    A deliberately small parser rather than PyYAML: the frontmatter here is four
    scalars and two lists, PyYAML is not a declared dependency of this backend (it
    arrives transitively, which is not the same as being depended on), and a skill
    library that stops loading because a transitive pin moved is a library that
    takes the pipeline's procedural knowledge with it.

    Returns ({}, text) when there is no frontmatter, so a body-only file is a skill
    with nothing declared rather than a parse error.
    """
    stripped = text.lstrip("﻿")
    if not stripped.startswith("---"):
        return {}, stripped.strip()
    lines = stripped.splitlines()
    end = next((i for i in range(1, len(lines)) if lines[i].strip() == "---"), None)
    if end is None:
        return {}, stripped.strip()
    return _frontmatter(lines[1:end]), "\n".join(lines[end + 1 :]).strip()


def _frontmatter(lines: list[str]) -> dict:
    """`key: value`, `key: [a, b]` and a `- item` block. Nothing else is needed."""
    data: dict = {}
    key: Optional[str] = None
    for raw in lines:
        line = raw.rstrip()
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if line.lstrip().startswith("- ") and key is not None:
            data.setdefault(key, [])
            if isinstance(data[key], list):
                data[key].append(_scalar(line.lstrip()[2:]))
            continue
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        key = key.strip()
        value = value.strip()
        if not value:
            data[key] = []  # a list block follows, or the key is simply empty
        elif value.startswith("[") and value.endswith("]"):
            data[key] = [_scalar(p) for p in value[1:-1].split(",") if p.strip()]
        else:
            data[key] = _scalar(value)
    return data


def _scalar(value: str) -> str:
    return value.strip().strip('"').strip("'").strip()


def _as_list(value: object) -> tuple[str, ...]:
    if isinstance(value, str):
        parts = [p.strip() for p in value.split(",")]
    elif isinstance(value, (list, tuple)):
        parts = [str(p).strip() for p in value]
    else:
        return ()
    return tuple(p for p in parts if p)


def check(name: str, title: str, description: str, body: str, max_chars: int) -> list[str]:
    """Everything that would stop this skill being injected, in a person's words.

    Checked against the *rendered* block — the title and the description go into the
    prompt beside the procedure, so a ceiling applied to the body alone is a ceiling
    a nine-thousand-character description walks straight past, and a format rule
    applied to the body alone leaves "reply in markdown" in the one field that is
    injected verbatim and never read.
    """
    problems: list[str] = []
    injected = rendered(title, description, body)
    if not NAME_RE.match(name or ""):
        problems.append(
            "The name has to be a slug — lowercase letters, digits and hyphens, "
            "2–64 characters — because it is also a folder name and the token a "
            "build pins or excludes by."
        )
    if not title.strip():
        problems.append("It has no title, so nothing can say what it is in a list.")
    if not description.strip():
        problems.append(
            "It has no description. That sentence is what tells a reader when this "
            "skill applies, and it is the only thing a preview can show."
        )
    for field, value in (("title", title), ("description", description)):
        if "\n" in value or "\r" in value:
            problems.append(
                f"The {field} runs over more than one line. It is stored as a single "
                "line of frontmatter, so everything after the first would be lost the "
                "next time this skill is read — along with, if the break happened to "
                "be a `---`, the list of agents it serves."
            )
    if not body.strip():
        problems.append("It has no body, so there is no procedure to give an agent.")
    elif len(injected) > max_chars:
        problems.append(
            f"The procedure is {len(injected):,} characters and the ceiling is "
            f"{max_chars:,}. Everything selected is paid for on every phase it "
            "reaches, so a long skill is paid for by the knowledge base and the "
            "prior phases that then get less room."
        )
    for pattern, why in _FORMAT_RULES:
        found = pattern.search(injected)
        if found:
            problems.append(
                f"“{found.group(0)}” {why}. Every agent already answers in a declared "
                "JSON shape; a skill that competes with it buys a repair round, and "
                "on a small model it wins. Skills shape content, never format."
            )
            break
    return problems


def load_file(
    path: Path,
    source: str,
    max_chars: int,
    known_phases: frozenset[str],
) -> Optional[Skill]:
    """One `SKILL.md` as a `Skill`, or None when the file cannot be read at all.

    Unreadable is not the same as invalid: a file this cannot open has nothing to
    show a person, so it is logged and skipped. A file it *can* read but that breaks
    a rule comes back with the reason attached and is listed, disabled.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except Exception as e:  # noqa: BLE001 - one bad file must not empty the library
        log.warning("Skill at %s could not be read: %s", path, e)
        return None

    meta, body = parse(text)
    name = _scalar(str(meta.get("name") or "")) or path.parent.name
    title = _scalar(str(meta.get("title") or ""))
    description = _scalar(str(meta.get("description") or ""))
    agents = _as_list(meta.get("agents"))
    keywords = tuple(k.lower() for k in _as_list(meta.get("keywords")))

    problems = check(name, title, description, body, max_chars)
    unknown = [a for a in agents if a not in known_phases]
    if unknown:
        problems.append(
            f"It is bound to {', '.join(unknown)}, which {'is not a phase' if len(unknown) == 1 else 'are not phases'} "
            "of this pipeline, so no agent would ever receive it."
        )

    return Skill(
        name=name,
        title=title or name,
        description=description,
        agents=agents,
        keywords=keywords,
        body=body,
        source=source,
        path=str(path),
        problems=tuple(problems),
    )


def rendered(title: str, description: str, body: str) -> str:
    """The block an agent is given, from the three fields that make it up.

    One function so the rules, the ceiling, the character count on screen and the
    prompt itself all measure the same string. Written apart from `render` because
    the checks run before there is a `Skill` to render.
    """
    head = f"## {title}"
    intro = f"\n{description}" if description else ""
    return f"{head}{intro}\n{body}".strip()


def render(skill: Skill) -> str:
    """One skill as it appears in an agent's prompt.

    The title leads and the description follows it, because the description says
    *when* the procedure applies — an agent handed a procedure with no trigger
    applies it to everything.
    """
    return rendered(skill.title, skill.description, skill.body)


def to_markdown(skill: Skill) -> str:
    """A skill back as the file it came from — what `PUT` writes and `GET` returns."""
    lines = [
        "---",
        f"name: {skill.name}",
        f"title: {skill.title}",
        f"description: {skill.description}",
        f"agents: [{', '.join(skill.agents)}]",
        f"keywords: [{', '.join(skill.keywords)}]",
        "---",
        "",
        skill.body.strip(),
        "",
    ]
    return "\n".join(lines)
