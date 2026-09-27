"""Taking API keys out of text before it is logged, stored or sent to the browser.

Providers quote keys back in their errors — OpenAI's 401 says `Incorrect API key
provided: sk-qVL45***…D1Vi` — and that text used to reach the log, the project's
`last_error` and the page. One step, `scrub`, is applied wherever such text goes:

- known key shapes (`sk-…`, `sk-proj-…`, `sk-ant-…`, `AIza…`), whole or masked;
- the text after "API key provided:", whatever shape it is;
- any run of 8+ characters of a key this process holds (`register`).

`install_log_scrubber` backs that up for every logger, so a message nobody thought
to scrub is scrubbed anyway.
"""
from __future__ import annotations

import logging
import re
import threading

REDACTED = "[redacted]"

#: Shorter than this, a fragment of a key is too likely to be an ordinary word.
_MIN_FRAGMENT = 8

_PATTERNS = (
    # "Incorrect API key provided: <anything>" — the echo itself, in any shape.
    re.compile(r"(?i)(api[ _-]?key(?: provided)?\s*[:=]\s*)\S+"),
    # sk-, sk-proj-, sk-ant-… — whole, or masked with *** / … in the middle. Not
    # after a letter or digit, so "task-list" is left alone.
    re.compile(r"(?<![A-Za-z0-9])sk-[A-Za-z0-9_\-]*(?:[*…\.]{2,}[A-Za-z0-9_\-]*)?"),
    # Google API keys.
    re.compile(r"AIza[0-9A-Za-z_\-]{10,}"),
    # Bearer / x-api-key header values quoted in an error.
    re.compile(r"(?i)((?:bearer|x-api-key|x-goog-api-key)[\s:=']+)[A-Za-z0-9_\-\.]{8,}"),
    # A masked token on its own: "abcd****wxyz".
    re.compile(r"[A-Za-z0-9_\-]{2,}\*{3,}[.…]*[A-Za-z0-9_\-]*"),
)

_KNOWN: set[str] = set()
_KNOWN_LOCK = threading.Lock()


def register(secret: str | None) -> None:
    """Remember a key this process holds, so any fragment of it is scrubbed."""
    if secret and len(secret) >= _MIN_FRAGMENT:
        with _KNOWN_LOCK:
            _KNOWN.add(secret)


def _known_fragments(text: str) -> str:
    with _KNOWN_LOCK:
        known = list(_KNOWN)
    for secret in known:
        if secret in text:
            text = text.replace(secret, REDACTED)
        # The longest run of the key found in the text, from each start — so a
        # quoted prefix or suffix goes whole, not all but its last few characters.
        i = 0
        while i <= len(secret) - _MIN_FRAGMENT:
            piece = secret[i : i + _MIN_FRAGMENT]
            if piece not in text:
                i += 1
                continue
            end = i + _MIN_FRAGMENT
            while end < len(secret) and secret[i : end + 1] in text:
                end += 1
            text = text.replace(secret[i:end], REDACTED)
            i = end
    return text


def scrub(text: object) -> str:
    """`text` with every key, and every recognisable piece of one, replaced."""
    if text is None:
        return ""
    out = text if isinstance(text, str) else str(text)
    if not out:
        return out
    out = _known_fragments(out)
    for pattern in _PATTERNS:
        if pattern.groups:
            out = pattern.sub(lambda m: m.group(1) + REDACTED, out)
        else:
            out = pattern.sub(REDACTED, out)
    return out


_INSTALLED = False


def install_log_scrubber() -> None:
    """Scrub every log record as it is made, for every logger and handler."""
    global _INSTALLED
    if _INSTALLED:
        return
    _INSTALLED = True
    previous = logging.getLogRecordFactory()

    def factory(*args, **kwargs):
        record = previous(*args, **kwargs)
        try:
            message = record.getMessage()
        except Exception:  # noqa: BLE001 - a bad format string is the logger's to report
            return record
        record.msg = scrub(message)
        record.args = ()
        if record.exc_info and not record.exc_text:
            record.exc_text = scrub(logging.Formatter().formatException(record.exc_info))
        return record

    logging.setLogRecordFactory(factory)
