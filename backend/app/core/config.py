"""Application configuration, loaded from environment / .env.

All settings have offline-friendly defaults so the platform runs with zero API keys.
"""
from __future__ import annotations

from functools import lru_cache
from typing import Optional

from pydantic import BeforeValidator
from pydantic_settings import BaseSettings, SettingsConfigDict
from typing_extensions import Annotated


def _blank_is_unset(value: object) -> object:
    """`FOO=` in a .env file means "I did not set this", not "parse this as a number".

    Every knob below is documented in `.env.example` as something to change, so
    blanking one to put it back to its default is the obvious move — and without
    this it raises at import of this module instead, and the server never starts.
    """
    if isinstance(value, str) and not value.strip():
        return None
    return value


def _blank_is_default(default):
    """The same, for a setting that has a real default rather than being optional."""

    def coerce(value: object) -> object:
        return default if isinstance(value, str) and not value.strip() else value

    return coerce


#: Numeric settings that survive being left blank in the environment.
OptionalInt = Annotated[Optional[int], BeforeValidator(_blank_is_unset)]


def BlankTolerantInt(default: int):  # noqa: N802 - reads as a type where it is used
    return Annotated[int, BeforeValidator(_blank_is_default(default))]


def BlankTolerantFloat(default: float):  # noqa: N802
    return Annotated[float, BeforeValidator(_blank_is_default(default))]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # ── App ──
    app_env: str = "development"
    log_level: str = "INFO"
    api_host: str = "0.0.0.0"
    api_port: int = 8000
    cors_origins: str = "http://localhost:3000"

    # ── Database ──
    database_url: str = "sqlite:///./data/aiteam.db"

    # ── Routing ──
    default_routing_mode: str = "local_only"  # auto | manual | local_only
    ollama_base_url: str = "http://localhost:11434"
    #: The local model every role falls back to — the one place a model name is
    #: configured at all. Settings writes the user's choice over it at runtime.
    ollama_default_model: str = "qwen2.5:7b"
    #: Extra `provider:model` links to try after the primary choice, comma-separated.
    #: Deliberately empty. The router already appends the local default as the last
    #: link of every chain, so naming a model here as well wrote it down twice —
    #: change one and the chain still pointed at the other, and the first transient
    #: failure fell through to a model that was never pulled, erroring in the name of
    #: a model the user had not chosen.
    fallback_chain: str = ""
    #: How many extra times one provider call is retried before the chain moves on.
    #: A local runtime drops the occasional request while it loads a model, and a
    #: single hiccup used to fail the whole run.
    provider_retry_attempts: BlankTolerantInt(2) = 2
    #: Seconds before the first retry. Each subsequent wait doubles, up to the cap.
    provider_retry_backoff_seconds: BlankTolerantFloat(1.0) = 1.0
    provider_retry_max_backoff_seconds: BlankTolerantFloat(8.0) = 8.0

    # ── Model capability & prompt budgets ──
    # Nothing below is a context size this app assumes about a local model. That
    # window is probed from the model itself (`POST /api/show`) and clamped by RAM;
    # these are the ceilings and the last resorts, all of them user-settable.
    #: Lower the local context window to at most this many tokens (unset = only the
    #: model's own limit and RAM decide). Useful for handing memory back.
    ollama_context_ceiling: OptionalInt = None
    #: Share of physical RAM the KV cache may claim once the weights are loaded.
    ollama_ram_fraction: BlankTolerantFloat(0.6) = 0.6
    #: The window assumed *only* when a provider will not report one at all.
    model_context_fallback_tokens: BlankTolerantInt(8192) = 8192
    #: Ceiling on tokens one call may generate. The resolved window can lower this,
    #: never raise it — half the window is the hard cap.
    max_output_tokens: BlankTolerantInt(4096) = 4096
    #: Ceiling on tokens one *prompt* may occupy, whatever the window allows. A
    #: 200k-token cloud window would otherwise let every phase inline every earlier
    #: phase's full generated source — correct, and roughly forty times the input
    #: cost per call. Local runs never reach this; it is a bill guard, not a budget.
    max_prompt_tokens: BlankTolerantInt(24576) = 24576
    #: Characters per token, used to turn a token budget into a truncation length.
    #: An estimate by nature, and the one the whole "the prompt fits the window"
    #: property rests on — so it is set below the ~3.2 break-even for the indented
    #: JSON and source code these prompts actually carry, not at a prose-like 3.5.
    #: Raise it if your phases are mostly prose and you want more context inlined.
    approx_chars_per_token: BlankTolerantFloat(3.0) = 3.0
    #: A model below this many parameters produces thin output whatever the prompt
    #: says. The Settings page says so rather than leaving the user to wonder why.
    small_model_parameter_count: BlankTolerantInt(4_000_000_000) = 4_000_000_000
    #: How many times a schema-invalid agent response is sent back for repair before
    #: the run keeps the best attempt and flags it. Each round is a whole extra call.
    schema_repair_rounds: BlankTolerantInt(1) = 1
    #: Whether the stack charter frozen after System Design is enforced on the phases
    #: that follow it. Off means the charter is still recorded and shown — it just
    #: stops failing a phase that contradicts it.
    enforce_stack_charter: bool = True
    #: How many times a build is sent back to fix its own severe security findings
    #: before the reviewer is asked to decide. Each round re-runs the owning phase
    #: and everything after it, so this is expensive; zero hands every finding
    #: straight to the gate instead.
    security_remediation_rounds: BlankTolerantInt(1) = 1

    # ── Mockup (the clickable site drawn from the design) ──
    # Counts, not budgets: how *big* each call may be is always derived from the
    # probed window of the model answering it. These say how much site to build.
    #: Pages in a generated mockup. Each page costs one model call per section.
    preview_max_routes: BlankTolerantInt(4) = 4
    #: Sections per page, not counting the shared header and footer.
    preview_max_sections_per_route: BlankTolerantInt(4) = 4
    #: Sample records per collection — lowered automatically when the chosen model's
    #: output budget cannot hold that many in one reply.
    preview_seed_rows: BlankTolerantInt(8) = 8
    #: Section calls run side by side on a cloud provider. A local runtime always
    #: runs one at a time: it serves one request at once and the rest just queue.
    preview_concurrency: BlankTolerantInt(3) = 3
    #: Load the finished mockup in a headless browser and count console errors.
    #: Only takes effect when Playwright is installed in the backend environment.
    preview_render_check: bool = True

    # ── Scaffold + compile gate (the generated code has to build) ──
    #: Whether a code phase whose files do not parse, or import things that do not
    #: exist, is sent back with the errors — and whether a build that still does not
    #: compile is stopped at the Ship gate instead of being labelled finished.
    enforce_build_check: bool = True
    #: Where the compile gate keeps its own toolchain (the TypeScript parser it uses
    #: to read JavaScript and TypeScript). cwd-relative, like the database.
    build_toolchain_dir: str = "./data/toolchain"
    #: Install that toolchain with npm on first use when it is missing. Off means
    #: JavaScript files are reported as unchecked rather than checked.
    build_check_provision: bool = True
    #: Seconds one compile check may take before it is reported as unchecked.
    build_check_timeout_seconds: BlankTolerantInt(90) = 90

    # ── Cloud providers ──
    anthropic_api_key: Optional[str] = None
    anthropic_default_model: str = "claude-opus-4-8"
    #: Published context windows. Cloud providers expose no probe, so these are
    #: configuration rather than a guess — override them when a model differs.
    anthropic_context_tokens: BlankTolerantInt(200_000) = 200_000
    openai_api_key: Optional[str] = None
    openai_default_model: str = "gpt-4o"
    openai_context_tokens: BlankTolerantInt(128_000) = 128_000
    gemini_api_key: Optional[str] = None
    gemini_default_model: str = "gemini-1.5-pro"
    gemini_context_tokens: BlankTolerantInt(1_000_000) = 1_000_000

    # ── GitHub publishing (OAuth "Connect" flow) ──
    # Register a free OAuth App at https://github.com/settings/developers and set
    # these. Leave blank to disable the "Push to GitHub" button gracefully.
    github_client_id: Optional[str] = None
    github_client_secret: Optional[str] = None
    # "repo" lets users create private repos too; "public_repo" = public only.
    github_scope: str = "repo"
    # Public URLs used to build the OAuth redirect + return targets. The callback
    # URL (backend + /api/github/oauth/callback) must match the OAuth App exactly.
    backend_public_url: str = "http://localhost:8000"
    frontend_base_url: str = "http://localhost:3000"

    # ── Vector store ──
    chroma_persist_dir: str = "./data/chroma"
    embedding_model: str = "nomic-embed-text"

    # ── Pipeline ──
    require_approval: bool = True
    enable_debate: bool = True
    #: A `running` project whose heartbeat is older than this is reported stalled,
    #: which is what makes Resume available instead of an endless poll. Generous
    #: enough for a slow local model to finish one phase without being written off.
    stall_after_seconds: int = 900
    #: How often the live runner touches `heartbeat_at` while a phase generates.
    heartbeat_interval_seconds: int = 5

    # ── Derived helpers ──
    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    @property
    def fallback_pairs(self) -> list[tuple[str, str]]:
        """Parse FALLBACK_CHAIN like 'ollama:qwen2.5:7b,anthropic:claude-opus-4-8'.

        Only the first ':' separates provider from model, so model names containing
        a colon (e.g. 'qwen2.5:7b') survive intact.
        """
        pairs: list[tuple[str, str]] = []
        for entry in self.fallback_chain.split(","):
            entry = entry.strip()
            if not entry or ":" not in entry:
                continue
            provider, model = entry.split(":", 1)
            pairs.append((provider.strip(), model.strip()))
        return pairs

    def configured_cloud_providers(self) -> list[str]:
        """Cloud providers that have an API key set (eligible for Auto mode)."""
        out: list[str] = []
        if self.anthropic_api_key:
            out.append("anthropic")
        if self.openai_api_key:
            out.append("openai")
        if self.gemini_api_key:
            out.append("gemini")
        return out


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
