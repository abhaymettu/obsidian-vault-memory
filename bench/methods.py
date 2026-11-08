"""Every system under test, behind one shape: question -> Retrieved (a context block, or for the agent a final answer)."""
from __future__ import annotations

import dataclasses
import json
import os
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

from vault_memory import config, llm
from vault_memory.recall import Params, recall
from vault_memory.store import Store, embed_query, home, passage_text

VAULT = config.vault()
OWNER = config.owner()
INDEX = config.setting("memory_md")  # optional: a memory index a session preloads, such as Claude Code's MEMORY.md
INDEX_LINES = int(config.setting("memory_md_lines", "200"))  # how much of it a session sees; Claude Code cuts long ones
MEMORY_DIR = config.setting("memory_dir")  # optional: the agent memory folder inside the vault
CTX_DB = home() / "bench/ctx.db"
REPO = Path(__file__).resolve().parent.parent


@dataclass
class Retrieved:
    block: str | None
    seconds: float
    answer: str | None = None
    meta: dict = field(default_factory=dict)


class CtxSearch:
    """Baseline (b): context-mode's ContentStore over the vault, formatted like ctx_search."""

    def __init__(self, limit: int):
        self.limit = limit
        self.proc = subprocess.Popen(
            ["bun", str(REPO / "bench/ctx_search.ts"), "serve", str(CTX_DB)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, bufsize=1,
        )

    def __call__(self, q: dict) -> Retrieved:
        t0 = time.time()
        self.proc.stdin.write(json.dumps({"query": q["question"], "limit": self.limit}) + "\n")
        out = json.loads(self.proc.stdout.readline())
        return Retrieved(out["text"], time.time() - t0, meta={"engine_seconds": out["seconds"], "sources": out["sources"]})


class DenseTopK:
    """Baseline (c): plain embedding top-k over the same chunks (bge-small cosine), raw chunks out."""

    def __init__(self, store: Store, k: int = 5):
        self.store, self.k = store, k

    def __call__(self, q: dict) -> Retrieved:
        t0 = time.time()
        m, ids = self.store.matrix()
        sims = m @ embed_query(q["question"])
        top = ids[(-sims).argsort()[: self.k]]
        rows = []
        for cid in top:
            r = self.store.db.execute(
                "SELECT c.path, n.title, c.heading, c.text FROM chunks c JOIN notes n ON n.path=c.path WHERE c.id=?", (int(cid),)
            ).fetchone()
            rows.append(f"--- {r[0]} ---\n{passage_text(r[1], r[2], r[3])}")
        return Retrieved("\n\n".join(rows), time.time() - t0)


class VaultMemory:
    def __init__(self, store: Store, mode: str, params: Params):
        self.store, self.mode, self.params = store, mode, params

    def __call__(self, q: dict) -> Retrieved:
        m = recall(self.store, q["question"], self.mode, self.params, refresh=False)
        return Retrieved(m.block, m.seconds, meta={"notes": m.notes, "synth_usage": m.synth_usage})


AGENT_PROMPT = """You are a Claude Code session working for {owner}. {index_line}Their Obsidian vault is the current directory ({vault}){folder}.
Answer the question by finding it in the vault: use {tools} as you normally would.
Final reply: the answer in one or two sentences, copying names, numbers, dates, paths and ids exactly. If you cannot
find it, reply "Not in memory."
{index_block}
Question: {question}"""


def index_text() -> str:
    return "\n".join(Path(INDEX).expanduser().read_text().splitlines()[:INDEX_LINES]) if INDEX else ""


def agent_prompt(question: str) -> str:
    index = index_text()
    return AGENT_PROMPT.format(
        owner=OWNER, vault=VAULT, question=question,
        index_line="Your memory index, MEMORY.md, is loaded below exactly as a\nsession sees it at start. " if index else "",
        folder=f"; the memory folder is {MEMORY_DIR}" if MEMORY_DIR else "",
        tools="the index, Grep, Glob and Read" if index else "Grep, Glob and Read",
        index_block=f"\n<MEMORY.md>\n{index}\n</MEMORY.md>\n" if index else "",
    )


VM_AGENT_PROMPT = """You are a Claude Code session working for {owner}. Their Obsidian vault ({vault}) holds their notes{memory}. To look something up, run `vault-memory recall "<full question>"` with Bash: it returns a memory
block with the most relevant passages, exact facts and the note path for each. If the block is not enough, run it again
with a rephrased question or Read a note it cites (paths are relative to {vault}).
Final reply: the answer in one or two sentences, copying names, numbers, dates, paths and ids exactly. If you cannot
find it, reply "Not in memory."

Question: {question}"""

VM_BIN = REPO / ".venv/bin"


def run_agent(prompt: str, tool_args: list[str], base_key: str, env: dict | None = None) -> Retrieved:
    t0 = time.time()
    proc = subprocess.run(
        [llm.CLAUDE, "-p", "--model", "sonnet", "--output-format", "stream-json", "--verbose", "--max-turns", "25", *llm.LEAN, *tool_args],
        input=prompt, capture_output=True, text=True, cwd=VAULT, timeout=900, env=env,
    )
    seconds = time.time() - t0
    contexts, answer, cost, turns, seen = [], "", 0.0, 0, []
    for line in proc.stdout.splitlines():
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        if ev.get("type") == "assistant":
            u = ev["message"].get("usage", {})
            contexts.append(u.get("input_tokens", 0) + u.get("cache_creation_input_tokens", 0) + u.get("cache_read_input_tokens", 0))
            seen += [json.dumps(p.get("input", {})) for p in ev["message"].get("content", []) if p.get("type") == "tool_use"]
        elif ev.get("type") == "user":
            for p in ev.get("message", {}).get("content", []):
                if isinstance(p, dict) and p.get("type") == "tool_result":
                    c = p.get("content")
                    seen.append(c if isinstance(c, str) else json.dumps(c))
        elif ev.get("type") == "result":
            answer, cost, turns = ev.get("result", ""), ev.get("total_cost_usd", 0.0), ev.get("num_turns", 0)
    meta = {
        "first_context": contexts[0] if contexts else 0,
        "final_context": contexts[-1] if contexts else 0,
        "billed_input": sum(contexts),
        "turns": turns,
        "cost_usd": cost,
        "base_key": base_key,
        "seen": "\n".join(seen)[-200000:],
    }
    return Retrieved(None, seconds, answer=answer, meta=meta)


def grep_agent(q: dict) -> Retrieved:
    """Baseline (a): a session finding the answer with Grep, Glob and Read, with the memory index preloaded when one is set."""
    return run_agent(
        agent_prompt(q["question"]),
        ["--tools", "Read,Grep,Glob", "--permission-mode", "bypassPermissions"],
        "agent_base",
    )


def vm_agent(q: dict, cards: bool = False) -> Retrieved:
    """The layer as a session uses it: no MEMORY.md preload, `vault-memory recall` through Bash, Read for cited notes."""
    env = {**os.environ, "PATH": f"{VM_BIN}:{os.environ['PATH']}", "VAULT_MEMORY_CARDS": "1" if cards else "0"}
    return run_agent(
        VM_AGENT_PROMPT.format(owner=OWNER, vault=VAULT, question=q["question"], memory=" and your\nagent memory folder" if MEMORY_DIR else ""),
        ["--tools", "Bash,Read", "--allowedTools", "Bash(vault-memory recall:*),Read", "--permission-mode", "dontAsk"],
        "vm_agent_base",
        env,
    )


def vm_agent_cards(q: dict) -> Retrieved:
    """The same session, with recall searching memory-card facts beside chunks."""
    return vm_agent(q, cards=True)


def build(names: list[str], params: Params) -> dict:
    stores: list[Store] = []

    def store() -> Store:
        if not stores:
            stores.append(Store())
        return stores[0]

    table = {
        "grep_agent": lambda: grep_agent,
        "vm_agent": lambda: vm_agent,
        "vm_agent_cards": lambda: vm_agent_cards,
        "ctx2": lambda: CtxSearch(2),
        "ctx5": lambda: CtxSearch(5),
        "dense5": lambda: DenseTopK(store(), 5),
        "vm_fast": lambda: VaultMemory(store(), "fast", dataclasses.replace(params, fact_k=0)),
        "vm_fast_cards": lambda: VaultMemory(store(), "fast", params),
        "vm_synth": lambda: VaultMemory(store(), "synth", dataclasses.replace(params, fact_k=0)),
    }
    return {n: table[n]() for n in names}
