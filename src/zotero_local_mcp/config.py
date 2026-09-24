"""Settings read from environment variables."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _path(value: str | None) -> Path | None:
    if not value:
        return None
    return Path(os.path.expandvars(value)).expanduser()


@dataclass(frozen=True)
class Settings:
    api_url: str = "http://127.0.0.1:23119/api"
    vocab_path: Path | None = None
    marker: str = "_agent"
    state_dir: Path = Path("~/.local/share/zotero-local-mcp").expanduser()
    auth_timeout: float = 300.0

    @classmethod
    def from_env(cls) -> "Settings":
        env = os.environ
        return cls(
            api_url=env.get("ZOTERO_API_URL", cls.api_url).rstrip("/"),
            vocab_path=_path(env.get("ZOTERO_VOCAB")),
            marker=env.get("ZOTERO_AGENT_MARKER", cls.marker),
            state_dir=_path(env.get("ZOTERO_MCP_STATE")) or cls.state_dir,
            auth_timeout=float(env.get("ZOTERO_AUTH_TIMEOUT", cls.auth_timeout)),
        )
