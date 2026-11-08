"""A warm recall server on a unix socket, so a recall costs retrieval time, not Python and model start-up."""
from __future__ import annotations

import json
import os
import signal
import socket
import sys
import socketserver
import threading

from .recall import Params, recall
from .store import Store, embedder, home, reranker


def sock_path() -> str:
    return str(home() / "sock")


def client_recall(question: str, mode: str, params: Params, timeout: float = 120.0) -> dict | None:
    path = sock_path()
    if not os.path.exists(path):
        return None
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(timeout)
            s.connect(path)
            s.sendall(json.dumps({"question": question, "mode": mode, "budget": params.budget, "fact_k": params.fact_k}).encode() + b"\n")
            data = b""
            while not data.endswith(b"\n"):
                chunk = s.recv(65536)
                if not chunk:
                    break
                data += chunk
        return json.loads(data)
    except (OSError, json.JSONDecodeError):
        return None


def serve() -> None:
    store = Store()
    store.refresh()
    embedder(), reranker(), store.matrix(), store.fact_matrix()
    lock = threading.Lock()

    class Handler(socketserver.StreamRequestHandler):
        def handle(self):
            req = json.loads(self.rfile.readline())
            with lock:
                params = Params(budget=req.get("budget", Params.budget), fact_k=req.get("fact_k", Params.fact_k))
                m = recall(store, req["question"], req.get("mode", "fast"), params)
            out = {"block": m.block, "notes": m.notes, "seconds": round(m.seconds, 3), "mode": m.mode, "synth_usage": m.synth_usage}
            self.wfile.write(json.dumps(out).encode() + b"\n")

    path = sock_path()
    if os.path.exists(path):
        os.unlink(path)
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    with socketserver.ThreadingUnixStreamServer(path, Handler) as srv:
        try:
            srv.serve_forever()
        finally:
            os.unlink(path)
