"""Per-model generation settings: one runtime-neutral form, validated before any adapter.

Three groups, kept apart because they answer to different people:

  * **sampling** — how tokens are picked, and how hard the model thinks first. Travels
    with every chat request, translated by the adapter for its runtime.
  * **limits** — ceilings on the window, the reply and the reasoning budget. Lower
    only: a model's own limit is never raised by typing a bigger number.
  * **machine** — GPU layers, threads, how long the model stays loaded, and what the
    KV cache is stored as. About the computer the model runs on, so set only in this
    backend's own configuration when it reaches the runtime directly, and never
    pushed to a runtime by a server that does not own it.

Every value is checked against a range here, before it is saved and before it could
reach a runtime, and an out-of-range value is refused with a sentence that names the
field and the range. Nothing in this file is a default for any model: an unset field
means "whatever the running server or the model file says".
"""
from __future__ import annotations

import math
import re
import unicodedata
from dataclasses import asdict, dataclass
from typing import Any, Optional

from app.router.runtimes.types import THINKING_SETTINGS

SAMPLING = "sampling"
LIMITS = "limits"
MACHINE = "machine"
GROUPS = (SAMPLING, LIMITS, MACHINE)

#: What a KV cache can be stored as, and roughly what one element weighs next to f16.
#: A property of the format (bits per element, block scales included), not a guess
#: about any model: 16, 8.5 and 4.5 bits.
KV_CACHE_TYPES: dict[str, float] = {"f16": 1.0, "q8_0": 8.5 / 16, "q4_0": 4.5 / 16}

_KEEP_ALIVE = re.compile(r"^(-1|0|[1-9]\d{0,5}(ms|s|m|h)?)$")
_MAX_STOPS = 4
_MAX_STOP_CHARS = 32
#: Characters every agent's JSON is made of. A stop sequence that is one of these —
#: or only whitespace, which pretty-printed JSON is full of — ends every reply at its
#: first brace or line break, so no agent could ever return its deliverable.
_JSON_STRUCTURE = set('{}[]":,')


class SettingsError(ValueError):
    """A value that cannot be saved, with the sentence that says why."""


@dataclass(frozen=True)
class Field:
    key: str
    group: str
    label: str
    #: "float", "int", "choice", "stops" or "duration".
    kind: str
    help: str
    minimum: Optional[float] = None
    maximum: Optional[float] = None
    step: Optional[float] = None
    choices: tuple[str, ...] = ()

    def as_dict(self) -> dict:
        data = asdict(self)
        data["choices"] = list(self.choices)
        return data


FIELDS: tuple[Field, ...] = (
    Field(
        "thinking", SAMPLING, "Thinking", "choice",
        "How hard the model reasons before it answers. Off is cheaper and keeps JSON "
        "reliable; a level is for models that take one instead of a switch.",
        choices=THINKING_SETTINGS,
    ),
    Field("temperature", SAMPLING, "Temperature", "float",
          "Higher is more varied, lower more literal. 0 is greedy, which thinking models "
          "should not be run at.", 0.0, 2.0, 0.05),
    Field("top_p", SAMPLING, "Top-p", "float",
          "Sample only from the smallest set of tokens whose probability adds up to this.",
          0.0, 1.0, 0.01),
    Field("top_k", SAMPLING, "Top-k", "int",
          "Sample only from this many most likely tokens. 0 turns it off.", 0, 1000, 1),
    Field("min_p", SAMPLING, "Min-p", "float",
          "Drop tokens less likely than this share of the most likely one.", 0.0, 1.0, 0.01),
    Field("repeat_penalty", SAMPLING, "Repeat penalty", "float",
          "Above 1 discourages repeating recent tokens; 1 turns it off.", 0.5, 2.0, 0.01),
    Field("presence_penalty", SAMPLING, "Presence penalty", "float",
          "Discourages any token that has appeared at all.", -2.0, 2.0, 0.1),
    Field("frequency_penalty", SAMPLING, "Frequency penalty", "float",
          "Discourages tokens in proportion to how often they have appeared.", -2.0, 2.0, 0.1),
    Field("seed", SAMPLING, "Seed", "int",
          "The same seed, prompt and settings give the same reply on most runtimes.",
          0, 2_147_483_647, 1),
    Field("stop", SAMPLING, "Stop sequences", "stops",
          f"Generation stops at any of these. Up to {_MAX_STOPS}, {_MAX_STOP_CHARS} "
          "characters each. The agents' JSON is cut short if one appears in it."),
    Field("context_window", LIMITS, "Context ceiling", "int",
          "Run at most this many tokens of window. Only ever lowers what the runtime "
          "reports; below 4,096 no agent prompt fits.", 512, 1_048_576, 256),
    Field("max_output_tokens", LIMITS, "Reply ceiling", "int",
          "The most one answer may use, never more than half the window.", 64, 131_072, 64),
    Field("reasoning_tokens", LIMITS, "Reasoning budget", "int",
          "Extra tokens kept for thinking on top of the answer, when thinking is on.",
          0, 131_072, 64),
    Field("gpu_layers", MACHINE, "GPU layers", "int",
          "How many layers the runtime puts on the GPU. 0 runs on the CPU only.", 0, 999, 1),
    Field("threads", MACHINE, "Threads", "int",
          "CPU threads the runtime generates with.", 1, 256, 1),
    Field("keep_alive", MACHINE, "Keep loaded", "duration",
          "How long the model stays in memory after a call: 30s, 10m, 1h; 0 unloads at "
          "once, -1 keeps it loaded."),
    Field("kv_cache_type", MACHINE, "KV cache type", "choice",
          "What the runtime stores its KV cache as — set there, when it starts; this only "
          "tells the memory estimate. q8_0 is about half of f16, q4_0 about a quarter.",
          choices=tuple(KV_CACHE_TYPES)),
)

BY_KEY: dict[str, Field] = {f.key: f for f in FIELDS}


def _number(field: Field, value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise SettingsError(f"{field.label} has to be a number.")
    # A JSON integer can be any length, and one too long for a float overflows on the
    # way in; anything that far out is outside every range here anyway.
    if isinstance(value, int) and abs(value) > 10**15:
        raise SettingsError(
            f"{field.label} has to be between {_shown(field.minimum)} and {_shown(field.maximum)}."
        )
    try:
        number = float(value)
    except (OverflowError, ValueError):
        raise SettingsError(f"{field.label} has to be a finite number.") from None
    if not math.isfinite(number):
        raise SettingsError(f"{field.label} has to be a finite number.")
    if field.kind == "int" and not number.is_integer():
        raise SettingsError(f"{field.label} has to be a whole number.")
    if (field.minimum is not None and number < field.minimum) or (
        field.maximum is not None and number > field.maximum
    ):
        raise SettingsError(
            f"{field.label} has to be between {_shown(field.minimum)} and {_shown(field.maximum)}; "
            f"{_shown(number)} is outside that."
        )
    return number


def _shown(value: Optional[float]) -> str:
    if value is None:
        return "?"
    return f"{int(value):,}" if float(value).is_integer() else f"{value:g}"


def _clean(field: Field, value: Any) -> Any:
    if field.kind == "float":
        return _number(field, value)
    if field.kind == "int":
        return int(_number(field, value))
    if field.kind == "choice":
        if value not in field.choices:
            raise SettingsError(f"{field.label} has to be one of {', '.join(field.choices)}.")
        return value
    if field.kind == "duration":
        text = str(value).strip() if isinstance(value, (str, int)) and not isinstance(value, bool) else ""
        if not _KEEP_ALIVE.match(text):
            raise SettingsError(
                f"{field.label} has to be a duration like 30s, 10m or 1h — or 0, or -1."
            )
        return text
    if field.kind == "stops":
        if not isinstance(value, list) or not all(isinstance(s, str) for s in value):
            raise SettingsError(f"{field.label} has to be a list of strings.")
        stops = [s for s in value if s != ""]
        if len(stops) > _MAX_STOPS:
            raise SettingsError(f"{field.label}: at most {_MAX_STOPS}.")
        for stop in stops:
            if len(stop) > _MAX_STOP_CHARS or any(
                unicodedata.category(c) in ("Cc", "Cf", "Zl", "Zp") and c not in "\n\t" for c in stop
            ):
                raise SettingsError(
                    f"{field.label}: each is at most {_MAX_STOP_CHARS} characters, with no "
                    "control or formatting characters other than newline and tab."
                )
            if not stop.strip() or (len(stop.strip()) == 1 and stop.strip() in _JSON_STRUCTURE):
                raise SettingsError(
                    f"{field.label}: {stop!r} would end every reply at the first place an agent's "
                    "JSON uses it, so no agent could finish. Use a longer sequence."
                )
        return list(dict.fromkeys(stops))
    raise SettingsError(f"{field.label} can't be set here.")


def validate(values: Any) -> dict[str, dict]:
    """`{group: {key: value}}` with every value checked; raises `SettingsError`.

    `None` (or a missing key) means "unset" and is dropped, so saving a form with a
    field cleared puts that field back on the server's default. A key in the wrong
    group, or one this file does not know, is refused rather than ignored — a
    setting that is silently dropped is exactly what this exists to prevent.
    """
    if not isinstance(values, dict):
        raise SettingsError("Settings have to be an object of groups.")
    out: dict[str, dict] = {}
    for group, fields in values.items():
        if group not in GROUPS:
            raise SettingsError(f"'{group}' isn't a group of settings ({', '.join(GROUPS)}).")
        if fields is None:
            continue
        if not isinstance(fields, dict):
            raise SettingsError(f"'{group}' has to be an object.")
        for key, value in fields.items():
            field = BY_KEY.get(key)
            if field is None or field.group != group:
                raise SettingsError(f"'{key}' isn't a {group} setting.")
            if value is None or value == [] or value == "":
                continue
            out.setdefault(group, {})[key] = _clean(field, value)
    limits = out.get(LIMITS, {})
    window, reply = limits.get("context_window"), limits.get("max_output_tokens")
    if window and reply and reply > window // 2:
        raise SettingsError(
            f"The reply ceiling ({reply:,}) can't be more than half the context ceiling "
            f"({window:,}): an agent needs room to read what it is answering."
        )
    return out


def read(stored: Any) -> dict[str, dict]:
    """Stored settings, re-validated field by field; a bad field is dropped, not fatal.

    The file is hand-editable. One mistyped value must not stop every call to the
    model, so what fails here is left out and the server's default applies.
    """
    out: dict[str, dict] = {}
    if not isinstance(stored, dict):
        return out
    for group in GROUPS:
        fields = stored.get(group)
        if not isinstance(fields, dict):
            continue
        for key, value in fields.items():
            try:
                cleaned = validate({group: {key: value}})
            except Exception:  # noqa: BLE001 - one bad hand-edited value never stops a call
                continue
            out.setdefault(group, {}).update(cleaned.get(group, {}))
    return out
