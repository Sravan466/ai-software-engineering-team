"""Separating what a model reasoned from what it answered.

Runtimes hand reasoning back in three ways: a field of its own (`thinking`,
`reasoning_content`, `reasoning`), or inline in the answer between tags — which is
what a server started without a reasoning parser does. The agent parser reads the
answer, so an inline block has to come out first; left in, the first `{` it finds
may be inside a thought, and a plausible JSON sketch the model considered and
discarded is parsed as the deliverable.

Reasoning is model output like any other. It is kept apart so it can be logged or
shown, and never fed back into a prompt or acted on.
"""
from __future__ import annotations

import re
from typing import Optional

_TAGS = "think|thinking|reasoning"
_OPEN = re.compile(rf"<({_TAGS})>", re.IGNORECASE)
_CLOSE = re.compile(rf"</({_TAGS})>", re.IGNORECASE)
#: What an answer starts with: JSON, or a fenced block. Reasoning a template opened
#: is prose; text before a closing tag that holds one of these is the answer itself.
_ANSWER_MARKS = ("{", "[", "`")


def split_reasoning(text: Optional[str]) -> tuple[str, Optional[str]]:
    """`(answer, reasoning)` from a reply that may carry reasoning inline.

    Three shapes are recognised, all at the head of the reply, where a chat template
    puts them:

      * `<think>…</think>answer` — a whole block (several in a row are all taken);
      * `…</think>answer` — the template opened the block in the prompt, so only
        the close is in the reply. Taken only as the very first thing, and only when
        nothing before the tag looks like an answer — no brace, bracket or code
        fence — so a tag quoted inside generated code is never mistaken for one;
      * `<think>…` never closed — the reasoning ran out of budget, and there is no
        answer at all. Reported as such rather than parsed.
    """
    if not text:
        return text or "", None
    answer = text
    thoughts: list[str] = []
    first = True
    while True:
        stripped = answer.lstrip()
        opened = _OPEN.match(stripped)
        if opened:
            closed = _CLOSE.search(stripped, opened.end())
            if closed is None:
                thoughts.append(stripped[opened.end():].strip())
                return "", _joined(thoughts)
            thoughts.append(stripped[opened.end() : closed.start()].strip())
            answer = stripped[closed.end():]
            first = False
            continue
        if first:
            closed = _CLOSE.search(stripped)
            head = stripped[: closed.start()] if closed else ""
            if closed and not _OPEN.search(head) and not any(c in head for c in _ANSWER_MARKS):
                thoughts.append(head.strip())
                answer = stripped[closed.end():]
        break
    if not thoughts:
        return text, None
    return answer.strip(), _joined(thoughts)


def _joined(parts: list[str]) -> Optional[str]:
    joined = "\n\n".join(p for p in parts if p)
    return joined or None


def merge(field: Optional[str], inline: Optional[str]) -> Optional[str]:
    """The reasoning a runtime returned in its own field, plus any left inline."""
    parts = [p.strip() for p in (field, inline) if isinstance(p, str) and p.strip()]
    return "\n\n".join(parts) or None
