"""Settings: an environment variable VAULT_MEMORY_<NAME> wins, then ~/.config/vault-memory/config.toml, then the default.

  # ~/.config/vault-memory/config.toml
  vault = "~/Notes"          # the Obsidian vault to index
  owner = "Sam"              # whose notes these are; card extraction resolves "he" or "I" to this name
"""
from __future__ import annotations

import os
import tomllib
from functools import cache
from pathlib import Path

CONFIG = Path(os.environ.get("VAULT_MEMORY_CONFIG", Path.home() / ".config/vault-memory/config.toml"))


@cache
def _file() -> dict:
    try:
        return tomllib.loads(CONFIG.read_text())
    except (FileNotFoundError, tomllib.TOMLDecodeError):
        return {}


def setting(name: str, default: str | None = None) -> str | None:
    return os.environ.get(f"VAULT_MEMORY_{name.upper()}") or _file().get(name) or default


def vault() -> Path:
    v = setting("vault")
    if not v:
        raise SystemExit(f"vault-memory: no vault set. Put vault = \"~/path/to/vault\" in {CONFIG} or set VAULT_MEMORY_VAULT.")
    return Path(v).expanduser()


def owner() -> str:
    return setting("owner", "the vault's owner")
