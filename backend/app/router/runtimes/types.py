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

#: How hard a model is asked to think before it answers, in one runtime-neutral
#: vocabulary. Each adapter translates it into its runtime's own switch — a boolean,
#: an effort level, a chat-template flag — or reports that it cannot.
THINKING_OFF = "off"
THINKING_ON = "on"
THINKING_LOW = "low"
THINKING_MEDIUM = "medium"
THINKING_HIGH = "high"
THINKING_EFFORTS = (THINKING_LOW, THINKING_MEDIUM, THINKING_HIGH)
THINKING_SETTINGS = (THINKING_OFF, THINKING_ON, *THINKING_EFFORTS)

#: What a model's thinking can be set to, as its runtime reports it (`ModelInfo.thinking`).
THINKS_NONE = "none"  # it answers directly
THINKS_TOGGLE = "toggle"  # it thinks, and can be told not to
THINKS_LEVELS = "levels"  # it thinks at an effort level, and cannot be switched off
THINKS_ALWAYS = "always"  # it thinks, and nothing about that can be changed


def thinking_for(style: Optional[str], wanted: Optional[str]) -> Optional[str]:
    """The setting a request can actually carry, for a model whose thinking is `style`.

    `None` means "say nothing about thinking": the model does not think, or nobody
    asked. A model that cannot be switched off is sent its lowest level for "off"
    rather than an instruction it would ignore, and a toggle-only model hears any
    level as "on" — the budget is reserved either way, because it will think.
    """
    if wanted is None or style == THINKS_NONE:
        return None
    if style == THINKS_ALWAYS:
        return THINKING_ON
    if style == THINKS_LEVELS:
        if wanted == THINKING_OFF:
            return THINKING_LOW
        return THINKING_MEDIUM if wanted == THINKING_ON else wanted
    if style == THINKS_TOGGLE:
        return THINKING_OFF if wanted == THINKING_OFF else THINKING_ON
    return wanted  # the runtime did not say; pass the wish through as stated


def thinks(level: Optional[str]) -> bool:
    """Whether a request at this setting will spend tokens reasoning."""
    return level is not None and level != THINKING_OFF


#: Settings a request can carry that shape which tokens are picked. Stored per model
#: in one form and translated by each adapter; one it cannot send is reported as
#: unsupported, never dropped without a word.
SAMPLING_KEYS = (
    "temperature",
    "top_p",
    "top_k",
    "min_p",
    "repeat_penalty",
    "presence_penalty",
    "frequency_penalty",
    "seed",
    "stop",
    "thinking",
)
#: Settings about the machine the model runs on. Set locally only — this backend's
#: own config when it reaches the runtime directly — and never pushed by a server.
MACHINE_KEYS = ("gpu_layers", "threads", "keep_alive")


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
    #: One of the `THINKS_*` styles — whether the model thinks before answering, and
    #: how that can be set. None when the runtime does not say.
    thinking: Optional[str] = None
    is_local: bool = True
    architecture: Optional[str] = None
    #: Bytes of KV cache one token costs in the layers that attend to the whole
    #: window, when the architecture is reported in enough detail to compute it.
    #: What lets RAM clamp the window on a small machine.
    kv_bytes_per_token: Optional[int] = None
    #: The same, for layers that attend only to the last `sliding_window` tokens — a
    #: cost that stops growing once the window passes that. Zero when there are none.
    kv_bytes_per_token_windowed: int = 0
    sliding_window: Optional[int] = None
    #: For a mixture-of-experts model: how many experts it has, and how many run per
    #: token. Memory follows the total; speed follows the ones that run.
    experts_total: Optional[int] = None
    experts_active: Optional[int] = None
    #: What the weights weigh on disk, which is close to what they need in memory.
    weights_bytes: Optional[int] = None
    #: Sampling defaults the running server or the model file declares, in the
    #: runtime-neutral names of `SAMPLING_KEYS`. What applies when nobody sets one.
    defaults: dict = field(default_factory=dict)
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
    top_k: Optional[int] = None
    min_p: Optional[float] = None
    repeat_penalty: Optional[float] = None
    presence_penalty: Optional[float] = None
    frequency_penalty: Optional[float] = None
    seed: Optional[int] = None
    stop: Optional[list[str]] = None
    #: One of `THINKING_SETTINGS`, already fitted to the model by `thinking_for`, or
    #: None to say nothing about thinking at all.
    thinking: Optional[str] = None
    #: Machine-resource settings (`MACHINE_KEYS`), from this backend's own config.
    gpu_layers: Optional[int] = None
    threads: Optional[int] = None
    keep_alive: Optional[str] = None
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
    #: The answer, and only the answer. Whatever the model reasoned first is in
    #: `reasoning`, so nothing downstream ever parses a thought as a deliverable.
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
    #: What the model reasoned before answering, when it did. Model output like any
    #: other — untrusted, never executed, and never fed back into a prompt.
    reasoning: Optional[str] = None
    #: Settings the request carried that this runtime has no way to send.
    unsent: tuple[str, ...] = ()

    def as_dict(self) -> dict:
        data = asdict(self)
        data["unsent"] = list(self.unsent)
        return data
