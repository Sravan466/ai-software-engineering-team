"""The typed operations every runtime adapter answers, and the records they return.

A model source is reached through exactly these: `list_models`, `model_info`, `chat`,
`embed` and `cancel`. Each takes and returns plain records rather than a runtime's
own JSON, so the router never learns which product is on the other end — and so
the same operations can later travel over a connector unchanged: every record here
is flat data that survives `as_dict()` and back.

Nothing in this file names a runtime. What a record *means* is decided once here,
and each adapter's job is only to fill one in from what its runtime reports.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Optional

#: What a model is for, normalised across runtimes. `None` means the runtime did not
#: say — which is not the same as any of these, and never blocks anything.
KIND_CHAT = "chat"
KIND_EMBEDDING = "embedding"
KIND_VISION = "vision"
KIND_BASE = "base"
KINDS = (KIND_CHAT, KIND_EMBEDDING, KIND_VISION, KIND_BASE)

#: How tightly a runtime can hold a reply to a shape, best first.
STRUCTURED_SCHEMA = "schema"  # decoding constrained to a JSON Schema
STRUCTURED_GRAMMAR = "grammar"  # a grammar the adapter compiles the schema to
STRUCTURED_JSON = "json"  # valid JSON, any shape — validation and repair do the rest
STRUCTURED_NONE = "none"  # free text; the shape lives only in the prompt

#: Where a context window came from.
CONTEXT_REPORTED = "reported"  # the runtime said so
CONTEXT_CONFIGURED = "configured"  # a configured fallback, because nobody could say
CONTEXT_USER = "user"  # the user set it


def writes(kind: Optional[str]) -> Optional[bool]:
    """Whether a model of this kind can write an agent's answer; None if unknown.

    The one place a kind is turned into a verdict, and it only says no on positive
    evidence: an embedding model returns vectors, not text. Every other kind — and
    no answer at all — lets the model through, because refusing a working model on
    a runtime's silence is worse than letting the run report a real error.
    """
    if kind is None:
        return None
    return kind != KIND_EMBEDDING


@dataclass(frozen=True)
class Hello:
    """Who answered at an address: which runtime, and which version of it."""

    runtime: str
    base_url: str
    version: Optional[str] = None

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class ModelEntry:
    """One model a source serves, as cheaply as the runtime can list it."""

    name: str
    kind: Optional[str] = None
    #: The runtime's own words for what the model does, quoted rather than
    #: interpreted. None when the runtime did not report any.
    capabilities: Optional[tuple[str, ...]] = None
    #: False for a model a runtime lists but sends elsewhere to run — a hosted model
    #: behind a local name. Such a model is not local, whatever serves its name.
    is_local: bool = True
    size_bytes: Optional[int] = None
    loaded: Optional[bool] = None

    def as_dict(self) -> dict:
        data = asdict(self)
        data["capabilities"] = list(self.capabilities) if self.capabilities is not None else None
        return data


@dataclass(frozen=True)
class ModelInfo:
    """Everything a source can say about one model, normalised.

    `build_profile` turns this into the window and budgets a run is sized with, so
    no runtime's field names reach the code that plans a prompt.
    """

    name: str
    #: The window the model can actually be run at, and where that number came from.
    context_window: Optional[int] = None
    context_source: Optional[str] = None
    parameters_total: Optional[int] = None
    #: For a mixture-of-experts model, the parameters that run per token.
    parameters_active: Optional[int] = None
    #: The size as the runtime writes it for a person ("7.6B"), when it does.
    parameter_label: Optional[str] = None
    quantization: Optional[str] = None
    kind: Optional[str] = None
    capabilities: Optional[tuple[str, ...]] = None
    structured_output: str = STRUCTURED_NONE
    #: "none", "toggle" or "always" — whether the model thinks before answering, and
    #: whether that can be switched off. None when the runtime does not say.
    thinking: Optional[str] = None
    is_local: bool = True
    architecture: Optional[str] = None
    #: Bytes of KV cache one token costs, when the architecture is reported in enough
    #: detail to compute it. What lets RAM clamp the window on a small machine.
    kv_bytes_per_token: Optional[int] = None
    runtime_version: Optional[str] = None
    listen_address: Optional[str] = None
    warnings: tuple[str, ...] = field(default_factory=tuple)

    def as_dict(self) -> dict:
        data = asdict(self)
        data["capabilities"] = list(self.capabilities) if self.capabilities is not None else None
        data["warnings"] = list(self.warnings)
        return data


@dataclass
class ChatRequest:
    """One completion, with every limit it runs under stated rather than defaulted."""

    model: str
    #: `[{role, content}]`.
    messages: list[dict]
    #: The output ceiling. Always sent: a runtime left to its own default has been
    #: known to stop at 512 tokens, mid-sentence, with no error.
    max_tokens: int
    #: The window the prompt was budgeted for. Runtimes that size the window per
    #: request are sent it; the rest were loaded with one, and it is reported there.
    context_window: int
    temperature: Optional[float] = None
    top_p: Optional[float] = None
    json_schema: Optional[dict] = None
    json_mode: bool = False
    #: The strongest structured mode the caller believes this model supports. The
    #: adapter may fall back from it, and says so in the result.
    structured_output: str = STRUCTURED_NONE
    #: Names this request for `cancel`.
    request_id: Optional[str] = None

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass
class ChatResult:
    text: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    #: Why generation stopped, normalised: "stop", "length", or the runtime's word.
    finish_reason: Optional[str] = None
    #: The structured mode that actually held this reply.
    structured_output: str = STRUCTURED_NONE
    #: True when the runtime refused the mode the caller asked for and a weaker one
    #: was used. The caller downgrades what it remembers, so the next call does not
    #: pay for the refusal again.
    structured_output_rejected: bool = False

    def as_dict(self) -> dict:
        return asdict(self)
