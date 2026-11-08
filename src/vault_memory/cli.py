"""vault-memory: a memory layer for Claude over an Obsidian vault.

  vault-memory index                     encode new and changed notes (idempotent)
  vault-memory cards [--model haiku]     extract memory cards for notes without a current one (model calls)
  vault-memory recall "question" [--budget N] [--mode fast|synth] [--json]
  vault-memory stats
  vault-memory serve                     keep models warm; recall calls go through ~/.local/share/vault-memory/sock
  vault-memory mcp                       MCP stdio server exposing the recall tool
"""
from __future__ import annotations

import argparse
import json
import os
import sys

from .recall import Params, recall
from .store import Store


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="vault-memory", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("index")
    sub.add_parser("stats")
    c = sub.add_parser("cards")
    c.add_argument("--model", default="haiku")
    c.add_argument("--workers", type=int, default=8)
    c.add_argument("--limit", type=int, default=0, help="at most this many notes")
    c.add_argument("--folder", action="append", help="only notes under this vault folder (repeatable)")
    c.add_argument("--skip", action="append", default=[], help="skip notes under this folder (repeatable)")
    sub.add_parser("serve")
    sub.add_parser("mcp")
    r = sub.add_parser("recall")
    r.add_argument("question")
    r.add_argument("--budget", type=int, default=int(os.environ.get("VAULT_MEMORY_BUDGET", Params.budget)), help="token budget for the block")
    r.add_argument("--mode", choices=["fast", "synth", "trained"], default="fast")
    r.add_argument("--no-cards", action="store_true", default=os.environ.get("VAULT_MEMORY_CARDS") == "0",
                   help="search chunks only, not memory-card facts (also VAULT_MEMORY_CARDS=0)")
    r.add_argument("--json", action="store_true")
    r.add_argument("--no-daemon", action="store_true")
    a = ap.parse_args(argv)

    if a.cmd == "recall":
        from . import daemon

        params = Params(budget=a.budget, fact_k=0 if a.no_cards else Params.fact_k)
        out = None if a.no_daemon else daemon.client_recall(a.question, a.mode, params)
        if out is None:
            m = recall(Store(), a.question, a.mode, params)
            out = {"block": m.block, "notes": m.notes, "seconds": round(m.seconds, 3), "mode": m.mode, "synth_usage": m.synth_usage}
        print(json.dumps(out, indent=1) if a.json else out["block"])
        return 0
    if a.cmd == "index":
        s = Store().refresh()
        print(f"added {s.added}, changed {s.changed}, removed {s.removed}, chunks embedded {s.chunks_embedded}, {s.seconds:.1f}s")
        return 0
    if a.cmd == "cards":
        from .cards import extract

        store = Store()
        store.refresh()
        st = extract(store, a.model, a.workers, a.limit, a.folder, tuple(a.skip))
        print(f"cards: {st.notes} notes, {st.facts} facts, {st.failed} failed, {st.calls} calls, ${st.cost_usd:.2f}, {st.seconds:.0f}s")
        return 0
    if a.cmd == "stats":
        print(json.dumps(Store().counts()))
        return 0
    if a.cmd == "serve":
        from . import daemon

        daemon.serve()
        return 0
    if a.cmd == "mcp":
        from . import mcp

        mcp.serve()
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
