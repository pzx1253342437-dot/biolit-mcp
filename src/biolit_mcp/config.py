"""Runtime settings, read from environment variables."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _default_cache_dir() -> Path:
    base = os.environ.get("XDG_CACHE_HOME") or str(Path.home() / ".cache")
    return Path(base) / "biolit-mcp"


@dataclass(frozen=True)
class Settings:
    ncbi_api_key: str | None = None
    ncbi_email: str | None = None
    tool_name: str = "biolit-mcp"
    cache_enabled: bool = True
    cache_dir: Path = field(default_factory=_default_cache_dir)
    search_ttl_s: int = 24 * 3600
    record_ttl_s: int = 7 * 24 * 3600
    timeout_s: float = 20.0
    max_retries: int = 4

    @property
    def ncbi_rate_per_s(self) -> float:
        # NCBI E-utilities policy: 3 req/s without a key, 10 req/s with one.
        return 10.0 if self.ncbi_api_key else 3.0

    @classmethod
    def from_env(cls) -> Settings:
        cache_dir = os.environ.get("BIOLIT_CACHE_DIR")
        return cls(
            ncbi_api_key=os.environ.get("NCBI_API_KEY") or None,
            ncbi_email=os.environ.get("NCBI_EMAIL") or None,
            cache_enabled=os.environ.get("BIOLIT_CACHE", "1") not in {"0", "false", "no"},
            cache_dir=Path(cache_dir) if cache_dir else _default_cache_dir(),
        )
