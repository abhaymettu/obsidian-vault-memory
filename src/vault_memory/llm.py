"""One model call through the Claude Code CLI (`claude -p`), with the usage and cost it reports.

The command comes from the `claude` setting (config.py); a wrapper with the same flags works too. Each call starts lean:
no tools, no settings, hooks, MCP servers or slash commands, and a one-line system prompt."""
from __future__ import annotations

import fcntl
import json
import os
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from . import config

CLAUDE = os.path.expanduser(config.setting("claude", "claude"))
LEAN = ["--system-prompt", "Follow the instructions in the user message exactly. Answer in the format it asks for.",
        "--setting-sources", "", "--strict-mcp-config", "--disable-slash-commands"]
_spend_lock = threading.Lock()


class SpendCap(SystemExit):
    pass


def record(label: str, usd: float) -> None:
    """Add one call's API-equivalent cost to $VAULT_MEMORY_SPEND (a JSON ledger shared by every process) and stop the
    caller once the total passes $VAULT_MEMORY_SPEND_CAP. Without the variable, nothing is recorded."""
    path = os.environ.get("VAULT_MEMORY_SPEND")
    if not path:
        return
    ledger = Path(path)
    ledger.parent.mkdir(parents=True, exist_ok=True)
    cap = float(os.environ.get("VAULT_MEMORY_SPEND_CAP", "inf"))
    with _spend_lock, open(ledger.with_suffix(".lock"), "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        d = json.loads(ledger.read_text()) if ledger.exists() else {"total_usd": 0.0, "calls": 0, "by_method": {}}
        d["total_usd"] = round(d["total_usd"] + usd, 4)
        d["calls"] += 1
        d["by_method"][label] = round(d["by_method"].get(label, 0.0) + usd, 4)
        ledger.write_text(json.dumps(d, indent=1))
    if d["total_usd"] > cap:
        raise SpendCap(f"spend cap reached: ${d['total_usd']:.2f} API-equivalent (cap ${cap:.2f})")


@dataclass
class Reply:
    text: str
    usage: dict
    cost_usd: float
    seconds: float
    is_error: bool = False

    @property
    def input_tokens(self) -> int:
        u = self.usage
        return u.get("input_tokens", 0) + u.get("cache_creation_input_tokens", 0) + u.get("cache_read_input_tokens", 0)

    def usage_summary(self) -> dict:
        return {
            "input_tokens": self.input_tokens,
            "output_tokens": self.usage.get("output_tokens", 0),
            "cost_usd": self.cost_usd,
            "seconds": round(self.seconds, 2),
        }


def ask(prompt: str, model: str = "sonnet", extra_args: list[str] | None = None, timeout: int = 300) -> Reply:
    t0 = time.time()
    proc = subprocess.run(
        [CLAUDE, "-p", "--model", model, "--output-format", "json", *LEAN, *(extra_args or ["--tools", ""])],
        input=prompt,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    try:
        d = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return Reply(proc.stdout[-500:] + proc.stderr[-500:], {}, 0.0, time.time() - t0, True)
    return Reply(d.get("result", ""), d.get("usage", {}), d.get("total_cost_usd", 0.0), time.time() - t0, d.get("is_error", False))
