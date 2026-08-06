"""Runtime configuration.

Held in an object rather than module-level `os.getenv` calls so tests can point a
server at a temporary database without reimporting anything.
"""

from __future__ import annotations

import ipaddress
import os
from dataclasses import dataclass, field

DEFAULT_PORT = 8889
LOOPBACK_HOSTS = {"localhost"}


class ConfigError(Exception):
    """Configuration that would be unsafe or impossible to run."""


def _is_loopback(host: str) -> bool:
    """True only for addresses the OS actually confines to this machine.

    An empty host is NOT loopback: asyncio binds "" to INADDR_ANY. Treating it as
    local previously let `MEMORY_HOST=` in a compose file open an unauthenticated
    server — with a destructive /clear — to the whole network.
    """
    if not host:
        return False
    if host in LOOPBACK_HOSTS:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


@dataclass
class Config:
    db_path: str
    auth_token: str | None = None
    host: str = "127.0.0.1"
    port: int = DEFAULT_PORT
    model: str = "default"
    consolidate_every_minutes: int = 30
    watch_dir: str | None = None
    max_body_bytes: int = 1_000_000
    retrieval_limit: int = 12
    # Contradiction detection is unreliable on small models, so it stays off until a
    # judge model is named explicitly. See reconcile.py for the full rationale.
    judge_model: str | None = None
    reconcile_min_confidence: float = 0.7
    reconcile_min_overlap: float = 0.2
    reconcile_max_comparisons: int = 24
    max_query_terms: int = 32
    recency_halflife_days: float = 30.0
    weights: dict[str, float] = field(
        default_factory=lambda: {"relevance": 0.6, "recency": 0.25, "importance": 0.15}
    )

    @property
    def binds_publicly(self) -> bool:
        return not _is_loopback(self.host)

    @property
    def auth_required(self) -> bool:
        return self.auth_token is not None

    def validate(self) -> Config:
        """Refuse a configuration that would expose a wipeable memory store."""
        if self.binds_publicly and not self.auth_token:
            raise ConfigError(
                f"Refusing to bind {self.host} without authentication: /clear and /restore would "
                "be reachable by anyone. Set MEMORY_AUTH_TOKEN, or bind 127.0.0.1 for local use."
            )
        if not 0 <= self.port <= 65535:
            raise ConfigError(f"Port out of range: {self.port}")
        return self

    @classmethod
    def from_env(cls, **overrides) -> Config:
        here = os.path.dirname(os.path.abspath(__file__))
        values = {
            "db_path": os.getenv("MEMORY_DB", os.path.join(here, "memory.db")),
            "auth_token": os.getenv("MEMORY_AUTH_TOKEN") or None,
            # `or` rather than a getenv default: a set-but-empty variable must also
            # fall back to the safe address, not through to INADDR_ANY.
            "host": os.getenv("MEMORY_HOST") or "127.0.0.1",
            "port": int(os.getenv("PORT") or os.getenv("MEMORY_PORT") or DEFAULT_PORT),
            "model": os.getenv("MEMORY_MODEL", "default"),
            "judge_model": os.getenv("MEMORY_JUDGE_MODEL") or None,
            "consolidate_every_minutes": int(os.getenv("CONSOLIDATE_INTERVAL", "30")),
            "watch_dir": os.getenv("MEMORY_WATCH") or None,
        }
        values.update({k: v for k, v in overrides.items() if v is not None})
        return cls(**values)
