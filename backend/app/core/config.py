"""Application configuration, loaded from environment / .env.

All settings have offline-friendly defaults so the platform runs with zero API keys.
"""
from __future__ import annotations

import json
from functools import lru_cache
from typing import Optional

from pydantic import AliasChoices, BeforeValidator, Field
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
OptionalBool = Annotated[Optional[bool], BeforeValidator(_blank_is_unset)]


def BlankTolerantInt(default: int):  # noqa: N802 - reads as a type where it is used
    return Annotated[int, BeforeValidator(_blank_is_default(default))]


def BlankTolerantFloat(default: float):  # noqa: N802
    return Annotated[float, BeforeValidator(_blank_is_default(default))]


def _renamed(name: str, *old: str) -> AliasChoices:
    """Read a setting under its runtime-neutral name first, then its old ones.

    The context ceiling, the RAM share and the same-machine flag apply to any local
    runtime, but they were named after one. Renaming them would break every `.env`
    written before, so the old names stay readable as deprecated aliases, and the
    new name wins when both are set.
    """
    return AliasChoices(name, *old)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
        populate_by_name=True,
    )

    # ── App ──
    app_env: str = "development"
    log_level: str = "INFO"
    api_host: str = "0.0.0.0"
    api_port: int = 8000
    cors_origins: str = "http://localhost:3000"

    # ── Database ──
    database_url: str = "sqlite:///./data/aiteam.db"
    #: Where the pipeline graph checkpoints each build's position. Its own file, not a
    #: table in `database_url`: LangGraph owns the schema. Configurable so the test
    #: suite can keep its throwaway builds out of the real one.
    checkpoint_db_path: str = "./data/checkpoints.sqlite"

    # ── Routing ──
    default_routing_mode: str = "local_only"  # auto | manual | local_only
    #: Local model sources beyond the ones found on this machine, comma-separated:
    #: `http://127.0.0.1:1234`, or `label=url`. A JSON list of objects
    #: (`label`, `base_url`, `api_key`, `runtime`, `same_machine`) says more. An
    #: address on another computer is honoured here because whoever edits this file
    #: runs the backend; the Settings page asks for explicit confirmation instead.
    local_sources: str = ""
    #: Look for model runtimes on this machine — loopback only, on the default ports
    #: in the runtime adapter table. Nothing beyond loopback is ever probed.
    local_detect: bool = True
    #: Deprecated: a runtime address from before model sources existed. Set, it is
    #: one more configured source; unset, the runtime is found on loopback anyway.
    ollama_base_url: Optional[str] = None
    #: The local model every role falls back to, when nobody has chosen one in
    #: Settings. `source:model`, or a bare model name looked up on every running
    #: source. Nothing found under it, the first model that can write is used.
    local_default_model: str = Field(
        "qwen2.5:7b", validation_alias=_renamed("local_default_model", "ollama_default_model")
    )
    #: Sampling sent with every call to an OpenAI-compatible runtime. Sent always,
    #: because some servers default to a greedy temperature or a 512-token reply
    #: that no agent asked for. Callers that set their own temperature win.
    local_temperature: BlankTolerantFloat(0.7) = 0.7
    local_top_p: BlankTolerantFloat(0.9) = 0.9
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
    # window is asked of the runtime serving the model and clamped by RAM; these are
    # the ceilings and the last resorts, all of them user-settable.
    #: Lower the local context window to at most this many tokens (unset = only the
    #: model's own limit and RAM decide). Useful for handing memory back.
    local_context_ceiling: OptionalInt = Field(
        None, validation_alias=_renamed("local_context_ceiling", "ollama_context_ceiling")
    )
    #: Whether a source on another hostname shares this machine's memory. Unset, a
    #: loopback URL means yes and any other host means no — right for a runtime on
    #: another computer, wrong for one in a sibling container on the same host
    #: (a compose service name), which shares this machine's RAM and needs the
    #: clamp. Set it true there.
    local_same_machine: OptionalBool = Field(
        None, validation_alias=_renamed("local_same_machine", "ollama_same_machine")
    )
    #: Share of physical RAM the KV cache may claim once the weights are loaded.
    local_ram_fraction: BlankTolerantFloat(0.6) = Field(
        0.6, validation_alias=_renamed("local_ram_fraction", "ollama_ram_fraction")
    )
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

    # ── Skills (the procedural library injected into agent prompts) ──
    #: Whether agents are given skills at all. Off means an empty library and a
    #: pipeline that runs exactly as it did before skills existed — the same
    #: contract RAG and memory hold to when their store is unavailable.
    skills_enabled: bool = True
    #: The library shipped with this platform, cwd-relative like the database. When
    #: it does not resolve, the copy that ships beside `app/` is used instead, so a
    #: backend started from another directory still has its own skills.
    skills_bundled_dir: str = "./skills"
    #: Skills added on this machine. Gitignored, beside `providers.local.json`. A
    #: skill here shadows a bundled one of the same name, which is how the bundled
    #: library is editable without anything in the repository being written to.
    skills_user_dir: str = "./data/skills"
    #: How many skills one phase may be given, before the character budget has its
    #: say. A count, not a budget: how much room they get is always derived from the
    #: probed window of the model about to answer.
    skills_max_per_phase: BlankTolerantInt(4) = 4
    #: Ceiling on one skill's procedure. Everything selected is paid for on every
    #: phase it reaches — there is no second level that loads on demand — so a long
    #: skill is paid for by the knowledge base and the prior phases, which then get
    #: less room. A skill over this is listed with the reason and never injected.
    skill_body_max_chars: BlankTolerantInt(2400) = 2400

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
    #: The embedding model to look for when nobody has chosen one in Settings:
    #: `source:model`, or a bare name looked up on every running source. Not found,
    #: the first model a runtime reports as embedding-only is used.
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
    def configured_sources(self) -> list[dict]:
        """`LOCAL_SOURCES`, plus the deprecated runtime address when it was set.

        Each entry is `{label?, base_url, api_key?, runtime?, same_machine?}`. A
        malformed JSON value is not a startup failure — it reads as no sources, and
        the Settings page shows the runtimes it can find instead.
        """
        entries: list[dict] = []
        raw = (self.local_sources or "").strip()
        if raw.startswith("["):
            try:
                parsed = json.loads(raw)
            except ValueError:
                parsed = []
            entries += [e for e in parsed if isinstance(e, dict) and e.get("base_url")]
        elif raw:
            for item in raw.split(","):
                item = item.strip()
                if not item:
                    continue
                label, sep, url = item.partition("=")
                entries.append(
                    {"label": label.strip(), "base_url": url.strip()}
                    if sep and "://" not in label
                    else {"base_url": item}
                )
        if self.ollama_base_url and self.ollama_base_url.strip():
            # This name only ever held one runtime's address, so the source is that
            # runtime, under that runtime's id — which is what choices saved before
            # model sources existed refer to.
            entries.append({"base_url": self.ollama_base_url.strip(), "runtime_hint": "legacy"})
        return entries

    @property
    def fallback_pairs(self) -> list[tuple[str, str]]:
        """Parse FALLBACK_CHAIN like 'source:model,anthropic:claude-opus-4-8'.

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
