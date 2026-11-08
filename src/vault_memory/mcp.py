"""Minimal MCP stdio server: one tool, `recall`, with the models and the vector matrix kept warm in-process."""
from __future__ import annotations

import json
import sys

from .recall import Params, recall
from .store import Store, embedder, reranker

TOOL = {
    "name": "recall",
    "description": (
        "Recall what the user's Obsidian vault (and any agent memory folder in it) says about a question. Returns one small "
        "memory block: the most relevant passages from the right notes, plus an exact-facts section (dates, numbers, "
        "paths, ids copied verbatim), each tagged with its note path. Use it instead of grepping MEMORY.md or the vault. "
        "Ask a full natural question; one call usually suffices."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "question": {"type": "string", "description": "the question, in plain words"},
            "budget": {"type": "integer", "description": "token budget for the block (default 900)"},
            "mode": {"type": "string", "enum": ["fast", "synth"], "description": "fast (default, no model call) or synth"},
        },
        "required": ["question"],
    },
}


def serve() -> None:
    store = Store()
    warm = False

    def reply(id_, result=None, error=None):
        msg = {"jsonrpc": "2.0", "id": id_}
        msg.update({"error": error} if error else {"result": result})
        sys.stdout.write(json.dumps(msg) + "\n")
        sys.stdout.flush()

    for line in sys.stdin:
        try:
            req = json.loads(line)
        except json.JSONDecodeError:
            continue
        method, id_ = req.get("method"), req.get("id")
        if method == "initialize":
            reply(id_, {
                "protocolVersion": req.get("params", {}).get("protocolVersion", "2025-06-18"),
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "vault-memory", "version": "0.1.0"},
            })
        elif method == "tools/list":
            reply(id_, {"tools": [TOOL]})
        elif method == "tools/call":
            args = req.get("params", {}).get("arguments", {})
            try:
                if not warm:
                    embedder(), reranker(), store.refresh(), store.matrix()
                    warm = True
                m = recall(store, args["question"], args.get("mode", "fast"), Params(budget=int(args.get("budget", Params.budget))))
                reply(id_, {"content": [{"type": "text", "text": m.block}]})
            except Exception as e:  # report to the caller instead of killing the server
                reply(id_, {"content": [{"type": "text", "text": f"vault-memory error: {e}"}], "isError": True})
        elif method == "ping":
            reply(id_, {})
        elif id_ is not None:
            reply(id_, error={"code": -32601, "message": f"method not found: {method}"})
