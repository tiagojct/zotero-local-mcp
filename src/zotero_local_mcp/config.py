"""Settings read from environment variables."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _path(value: str | None) -> Path | None:
    if not value:
        return None
    return Path(os.path.expandvars(value)).expanduser()


ENV_FILE = Path("~/.config/zotero-local-mcp/env").expanduser()


def load_env_file(path: Path | None = None) -> None:
    """Read KEY=VALUE lines (email, API keys, paths) shared by all entry points.
    Variables already set in the environment win."""
    path = path or Path(os.environ.get("ZOTERO_MCP_ENV", ENV_FILE)).expanduser()
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip("'\""))


@dataclass(frozen=True)
class Settings:
    api_url: str = "http://127.0.0.1:23119/api"
    vocab_path: Path | None = None
    marker: str = "_agent"
    state_dir: Path = Path("~/.local/share/zotero-local-mcp").expanduser()
    auth_timeout: float = 300.0
    # Research and import services
    email: str | None = None          # sent to Unpaywall (required), Crossref, OpenAlex, NCBI
    ncbi_api_key: str | None = None
    openalex_api_key: str | None = None
    vault: Path | None = None         # Obsidian vault: import queue and alert notes
    alerts_config: Path | None = None

    @property
    def queue_path(self) -> Path | None:
        return self.vault / "Inbox" / "Zotero import queue.md" if self.vault else None

    @classmethod
    def from_env(cls) -> "Settings":
        load_env_file()
        env = os.environ
        vault = _path(env.get("ZOTERO_VAULT"))
        return cls(
            api_url=env.get("ZOTERO_API_URL", cls.api_url).rstrip("/"),
            vocab_path=_path(env.get("ZOTERO_VOCAB")),
            marker=env.get("ZOTERO_AGENT_MARKER", cls.marker),
            state_dir=_path(env.get("ZOTERO_MCP_STATE")) or cls.state_dir,
            auth_timeout=float(env.get("ZOTERO_AUTH_TIMEOUT", cls.auth_timeout)),
            email=env.get("ZOTERO_CONTACT_EMAIL") or None,
            ncbi_api_key=env.get("NCBI_API_KEY") or None,
            openalex_api_key=env.get("OPENALEX_API_KEY") or None,
            vault=vault,
            alerts_config=_path(env.get("ZOTERO_ALERTS"))
            or (vault / "Systems" / "Literature alerts.md" if vault else None),
        )
