"""Runtime configuration, read once from the environment."""
from __future__ import annotations

import os
import secrets
from dataclasses import dataclass, field
from pathlib import Path


def _bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    return default if raw is None else raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    env: str = os.environ.get("VERITAS_ENV", "dev")
    db_path: Path = Path(os.environ.get("VERITAS_DB", Path(__file__).resolve().parent.parent / "data" / "veritas.db"))
    jwt_secret: str = os.environ.get("JWT_SECRET") or secrets.token_urlsafe(48)
    jwt_ttl_minutes: int = int(os.environ.get("JWT_TTL_MINUTES", "480"))

    # Generation. Without an Anthropic credential the app runs in extractive mode.
    llm_model: str = os.environ.get("LLM_MODEL", "claude-opus-5-5")
    llm_effort: str = os.environ.get("LLM_EFFORT", "low")
    llm_enabled: bool = _bool("LLM_ENABLED", bool(os.environ.get("ANTHROPIC_API_KEY")))
    # USD per million tokens, used for per-org cost metering and budgets.
    price_in_per_mtok: float = float(os.environ.get("LLM_PRICE_IN", "4.0"))
    price_out_per_mtok: float = float(os.environ.get("LLM_PRICE_OUT", "20.0"))

    # Embeddings: "voyage" when VOYAGE_API_KEY is set, otherwise the local hashing embedder.
    voyage_api_key: str | None = os.environ.get("VOYAGE_API_KEY")
    voyage_model: str = os.environ.get("VOYAGE_MODEL", "voyage-3.5")

    # Retrieval
    top_k: int = int(os.environ.get("TOP_K", "5"))
    chunk_tokens: int = int(os.environ.get("CHUNK_TOKENS", "180"))
    chunk_overlap: int = int(os.environ.get("CHUNK_OVERLAP", "30"))

    # Governance
    rate_limit_per_min: int = int(os.environ.get("RATE_LIMIT_PER_MIN", "30"))
    query_retention_days: int = int(os.environ.get("QUERY_RETENTION_DAYS", "90"))
    seed_demo: bool = _bool("SEED_DEMO", True)
    cors_origins: list[str] = field(
        default_factory=lambda: os.environ.get("CORS_ORIGINS", "http://localhost:5173").split(",")
    )
    static_dir: Path = Path(os.environ.get("STATIC_DIR", Path(__file__).resolve().parent.parent / "static"))


settings = Settings()
