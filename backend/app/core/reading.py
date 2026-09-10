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
