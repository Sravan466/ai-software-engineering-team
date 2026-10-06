"""Reading what a model actually wrote, in one place.

Three small things are needed wherever a model's output is interpreted: deciding
whether a value says anything, pulling a number out of the prose money gets wrapped
in, and getting a JSON object out of a reply that may be fenced or prefaced.

They live here because they were living in three places, and the copies drifted. The
JSON extractor was hardened against a reply that parses to something other than an
object in `agents/base.py`, and the identical function in `orchestration/debate.py`
kept the crash — a phase-killing `AttributeError` that existed only because there
were two of it. One home, imported twice.
"""
from __future__ import annotations
from typing import Any, Optional

import json
import re
from dataclasses import dataclass

#: A signed number, with or without a decimal part.
NUMBER = re.compile(r"-?\d+(?:\.\d+)?")


def has_content(value: Any) -> bool:
    """Whether a value says anything. `None`, `""`, `[]` and `{}` do not; `0` does.

    The distinction decides which of several names for one field is the answer, so
    it has to be sharper than "is not null": a model that writes `"findings": []`
    beside a populated `"security_findings"` has reported findings. Zero stays
    content, because a build really can cost nothing.
    """
    if value is None:
        return False
    if isinstance(value, (str, list, tuple, dict, set)):
        return bool(value)
    return True


def as_number(value: Any) -> Optional[float]:
    """A number, including one a model wrapped in prose: `"$1,240/mo"` -> `1240.0`.

    `None` when there is no number in there at all — which is the honest answer, and
    the one that lets a caller tell "nothing was reported" from "zero was reported".
    """
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, str):
        match = NUMBER.search(value.replace(",", ""))
        return float(match.group(0)) if match else None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def json_object(text: str) -> Optional[dict]:
    """The JSON object in a model's reply, or None if there isn't one.

    Handles the two things models do around JSON they were asked for: wrapping it in
    a ```json fence, and prefacing it with a sentence. Anything that parses to a
    value other than an object counts as no object — a reply of `123` or `"sorry"`
    is not a deliverable, and treating it as one is how a phase dies with an
    `AttributeError` several frames from the mistake.
    """
    text = (text or "").strip()
    # Greedy, so nested braces are not cut short.
    fence = re.search(r"```(?:json)?\s*(\{[\s\S]*\})\s*```", text)
    candidate = fence.group(1) if fence else text
    if not candidate.lstrip().startswith("{"):
        brace = re.search(r"\{.*\}", candidate, re.DOTALL)
        if brace:
            candidate = brace.group(0)
    try:
        parsed = json.loads(candidate)
    except (json.JSONDecodeError, ValueError):
        return None
    return parsed if isinstance(parsed, dict) else None


# ── code written as fenced blocks (#81) ──────────────────────────────────────
#: The line a model puts above a file: `### backend/app/main.py`, `### File: x.py`,
#: `**app/page.tsx**`, `` `x.py` `` — or the bare path alone on its line.
_MARKED = re.compile(r"^\s*(?:#{1,6}\s*|\*\*\s*`?|`)")
_NAMED = re.compile(r"^(?:file(?:name)?|path)\s*[:\-]\s*", re.IGNORECASE)
_FENCE_OPEN = re.compile(r"^(?P<indent>[ \t]{0,3})(?P<fence>`{3,}|~{3,})(?P<info>[^\n`]*)$")
#: Fence info strings that name a language, not a file.
_LANGUAGE_WORDS = frozenset(
    "python py javascript js jsx typescript ts tsx json css scss html sql bash sh shell "
    "yaml yml toml markdown md text txt plaintext dockerfile ini env diff".split()
)


@dataclass(frozen=True)
class CodeBlock:
    """One file a model wrote: where it goes, its exact text, and whether it ended.

    `complete` is False for a block whose closing fence never came — the reply was cut
    off inside it — so its `code` is the start of a file, not a file.
    """

    path: str
    code: str
    language: str = ""
    complete: bool = True


def _looks_like_path(text: str, spaced: bool = False) -> bool:
    """Whether `text` names a file. `spaced` allows a space, for a line marked as a
    file name (a heading, bold, backticks), where a folder can have one in it."""
    text = text.strip()
    if not text or len(text) > 200 or text.endswith(".") or any(c in text for c in "<>|\"'"):
        return False
    if " " in text and not (spaced and "/" in text):
        return False
    if "/" in text:
        return True
    # A bare file name: one word with an extension (`main.py`, `.env.example`).
    return "." in text.strip(".")


#: What a model puts after the path on the same line: `— the entry point`,
#: `(updated)`, `: models`. Everything from the first of these on is not the path.
_AFTER_PATH = re.compile(r":\s|\s+(?:[—–]|-\s|\(|\[)|\s*:$")



def _named_path(line: str) -> str:
    """The path a line above a fence names, or "" when it names none.

    `### backend/app/main.py — FastAPI entry point` names `backend/app/main.py`, and
    `` ### `frontend/app/page.tsx` (updated) `` names the backticked part: a heading
    that says more than the path still names only the path.
    """
    marked = bool(_MARKED.match(line))
    text = line.strip()
    text = re.sub(r"^#{1,6}\s*", "", text)
    text = re.sub(r"^\d+[.)]\s+", "", text.strip("*").strip())  # a numbered list item
    ticked = re.search(r"`([^`\n]+)`", text)
    if ticked:
        candidate = _NAMED.sub("", ticked.group(1).strip()).strip()
        if _looks_like_path(candidate, spaced=True):
            return candidate
    text = _NAMED.sub("", text.strip("`").strip()).strip()
    cut = _AFTER_PATH.search(text)
    if cut:
        text = text[: cut.start()]
    text = text.strip().strip("*").strip("`").strip()
    return text if _looks_like_path(text, spaced=marked) else ""



def _path_in_info(info: str) -> tuple[str, str]:
    """(language, path) from a fence's info string: ```tsx app/page.tsx, ```py title="x.py"."""
    words = info.strip().split()
    language, path = "", ""
    for word in words:
        value = word.split("=", 1)[1].strip("\"'") if "=" in word else word.strip("\"'")
        if not language and value.lower() in _LANGUAGE_WORDS:
            language = value.lower()
        elif not path and _looks_like_path(value):
            path = value
    return language, path


def code_blocks(text: str) -> list[CodeBlock]:
    """Every file in a reply written as `### path` followed by one fenced block.

    The code is the text between the fences exactly as written — quotes, backslashes
    and template literals untouched, because nothing here was ever a JSON string. A
    longer fence (```` or ~~~) holds a file that itself contains ```; the block ends at
    the first line that is only the same fence character, at least as long. A block
    whose closing fence never came is returned with `complete=False`.

    The path comes from the nearest path-like line above the fence (a heading, a bold
    line, a backticked name), or from the fence's own info string. A block with no
    path at all is returned with `path=""` — the caller knows whether it asked for one
    file and can place it.
    """
    lines = (text or "").splitlines(keepends=True)
    out: list[CodeBlock] = []
    pending = ""  # the path named since the last block
    i = 0
    while i < len(lines):
        raw = lines[i].rstrip("\r\n")
        opened = _FENCE_OPEN.match(raw)
        if not opened:
            named = _named_path(raw) if raw.strip() else ""
            if named:
                pending = named
            i += 1
            continue
        fence = opened.group("fence")
        language, in_info = _path_in_info(opened.group("info"))
        body: list[str] = []
        j = i + 1
        closed = False
        while j < len(lines):
            stripped = lines[j].strip()
            if stripped and set(stripped) == {fence[0]} and len(stripped) >= len(fence):
                closed = True
                break
            body.append(lines[j])
            j += 1
        code = "".join(body)
        path = (pending or in_info).strip().lstrip("/")
        if path.startswith("./"):
            path = path[2:]
        out.append(CodeBlock(path=path, code=code, language=language, complete=closed))
        pending = ""
        i = j + 1
    return out
