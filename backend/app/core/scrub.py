"""Taking API keys out of text before it is logged, stored or sent to the browser.

Providers quote keys back in their errors — OpenAI's 401 says `Incorrect API key
provided: sk-qVL45***…D1Vi` — and that text used to reach the log, the project's
`last_error` and the page. One step, `scrub`, is applied wherever such text goes:

- known key shapes (`sk-…`, `sk-proj-…`, `sk-ant-…`, `AIza…`), whole or masked;
- the password in a connection string (`postgres://user:<password>@host`);
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
    # (Never a `%s` — a log template is scrubbed too, and its placeholder must survive.)
    re.compile(r"(?i)(api[ _-]?key(?: provided)?\s*[:=]\s*)(?!%)\S+"),
    # sk-, sk-proj-, sk-ant-… — whole, or masked with *** / … in the middle. Not
    # after a letter or digit, so "task-list" is left alone.
    re.compile(r"(?<![A-Za-z0-9])sk-[A-Za-z0-9_\-]*(?:[*…\.]{2,}[A-Za-z0-9_\-]*)?"),
    # Google API keys.
    re.compile(r"AIza[0-9A-Za-z_\-]{10,}"),
    # Bearer / x-api-key header values quoted in an error.
    re.compile(r"(?i)((?:bearer|x-api-key|x-goog-api-key)[\s:=']+)[A-Za-z0-9_\-\.]{8,}"),
    # The password in a connection string: scheme://user:<password>@host. Only the
    # password goes; the user and host stay, so the line still says which database.
    re.compile(r"(\b[a-z][a-z0-9+.-]*://[^\s:/@]*:)[^\s@/]+(?=@)", re.IGNORECASE),
    # A masked token on its own: "abcd****wxyz".
    re.compile(r"[A-Za-z0-9_\-]{2,}\*{3,}[.…]*[A-Za-z0-9_\-]*"),
)

#: Keys this process holds, counted — two accounts may hold the same one, and one
#: forgetting it must not stop it being scrubbed for the other.
_KNOWN: dict[str, int] = {}
_KNOWN_LOCK = threading.Lock()
#: One pattern for every 8-character run of every held key: the cheap test that
#: nearly every log line fails, before anything slower runs.
_ANCHORS: "re.Pattern[str] | None" = None


def _rebuild() -> None:
    global _ANCHORS
    pieces = {
        secret[i : i + _MIN_FRAGMENT]
        for secret in _KNOWN
        for i in range(len(secret) - _MIN_FRAGMENT + 1)
    }
    _ANCHORS = re.compile("|".join(map(re.escape, sorted(pieces)))) if pieces else None


def register(secret: "str | None") -> None:
    """Remember a key this process holds, so any fragment of it is scrubbed."""
    if secret and len(secret) >= _MIN_FRAGMENT:
        with _KNOWN_LOCK:
            _KNOWN[secret] = _KNOWN.get(secret, 0) + 1
            if _KNOWN[secret] == 1:
                _rebuild()


def forget(secret: "str | None") -> None:
    """A key this process no longer holds (replaced, removed): stop keeping it."""
    if not secret:
        return
    with _KNOWN_LOCK:
        count = _KNOWN.get(secret, 0) - 1
        if count > 0:
            _KNOWN[secret] = count
        elif secret in _KNOWN:
            del _KNOWN[secret]
            _rebuild()


def _known_fragments(text: str) -> str:
    with _KNOWN_LOCK:
        anchors = _ANCHORS
        known = list(_KNOWN)
    if anchors is None or not anchors.search(text):
        return text
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


def holds_known(text: object) -> bool:
    """Whether `text` contains a run of 8+ characters of a secret this process holds."""
    with _KNOWN_LOCK:
        anchors = _ANCHORS
    return bool(anchors is not None and text and anchors.search(str(text)))


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


def _scrub_other(value: object) -> object:
    """An exception passed as a log argument is formatted with `str()` — scrub that."""
    return scrub(value) if isinstance(value, BaseException) else value


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
        # The template and each text argument, scrubbed in place — the arguments stay
        # arguments, because some formatters (uvicorn's access log) unpack them.
        if isinstance(record.msg, str):
            record.msg = scrub(record.msg)
        elif isinstance(record.msg, BaseException):
            record.msg = scrub(record.msg)
        if isinstance(record.args, tuple):
            record.args = tuple(scrub(a) if isinstance(a, str) else _scrub_other(a) for a in record.args)
        elif isinstance(record.args, dict):
            record.args = {k: scrub(v) if isinstance(v, str) else v for k, v in record.args.items()}
        if record.exc_info and not record.exc_text:
            record.exc_text = scrub(logging.Formatter().formatException(record.exc_info))
        return record

    logging.setLogRecordFactory(factory)
