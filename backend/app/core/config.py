"""Application configuration, loaded from environment / .env.

All settings have offline-friendly defaults so the platform runs with zero API keys.
"""
from __future__ import annotations

from functools import lru_cache
from typing import Optional

from pydantic_settings import BaseSettings, SettingsConfigDict


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
    ollama_default_model: str = "qwen2.5:7b"
    fallback_chain: str = "ollama:qwen2.5:7b"

    # ── Cloud providers ──
    anthropic_api_key: Optional[str] = None
    anthropic_default_model: str = "claude-opus-4-8"
    openai_api_key: Optional[str] = None
    openai_default_model: str = "gpt-4o"
    gemini_api_key: Optional[str] = None
    gemini_default_model: str = "gemini-1.5-pro"

    # ── Vector store ──
    chroma_persist_dir: str = "./data/chroma"
    embedding_model: str = "nomic-embed-text"

    # ── Pipeline ──
    require_approval: bool = True
    enable_debate: bool = True

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
