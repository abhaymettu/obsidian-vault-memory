"""Free, deterministic retrieval score for hill-climbing: does the block carry the gold evidence?

Per question: each evidence quote counts as covered by the share of its content words found in the block (full credit
at 80%); verbatim questions also need one accepted exact string in the block. Prints `support <mean>` and latency.
No model calls, so a round costs seconds, not dollars; LLM-judged correctness confirms the winner afterwards."""
from __future__ import annotations

import argparse
import json
import re
import statistics
from pathlib import Path

from vault_memory.recall import Params, recall
from vault_memory.store import Store

from . import QUESTIONS
WORD = re.compile(r"[a-z0-9][a-z0-9_.\-/]*")
STOP = {"the", "and", "for", "that", "with", "this", "from", "are", "was", "his", "not", "but", "its", "into", "has", "have"}


def words(s: str) -> set[str]:
    return {w.strip(".-/") for w in WORD.findall(s.lower()) if len(w) > 2 and w not in STOP}


def norm(s: str) -> str:
    return re.sub(r"\s+", " ", s.replace("`", "").replace("*", "")).strip().lower()


def support(q: dict, block: str) -> float:
    bw = words(block)
    covered = []
    for ev in q["evidence"]:
        w = words(ev["quote"])
        covered.append(min(1.0, (len(w & bw) / len(w)) / 0.8) if w else 1.0)
    s = statistics.mean(covered)
    if q["type"] == "verbatim" and not any(norm(e) in norm(block) for e in q["exact"]):
        s *= 0.5
    return s


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--split", default="dev")
    ap.add_argument("--budget", type=int, default=1500)
    ap.add_argument("--fact-k", type=int, default=Params.fact_k, help="card facts in the rerank pool; 0 = chunks only")
    ap.add_argument("--dump", default="", help="write per-question scores to this JSON file")
    a = ap.parse_args()
    qs = [json.loads(l) for l in QUESTIONS.read_text().splitlines() if l.strip()]
    qs = [q for q in qs if a.split == "all" or q["split"] == a.split]
    store = Store()
    store.refresh()
    p = Params(budget=a.budget, fact_k=a.fact_k)
    recall(store, "warm up the models", params=p, refresh=False)
    rows = []
    for q in qs:
        m = recall(store, q["question"], params=p, refresh=False)
        rows.append({"id": q["id"], "type": q["type"], "support": support(q, m.block), "ms": m.seconds * 1000, "chars": len(m.block)})
    by_type = {t: statistics.mean(r["support"] for r in rows if r["type"] == t) for t in ("single", "multi", "verbatim")}
    if a.dump:
        Path(a.dump).write_text(json.dumps(rows, indent=1))
    print(" ".join(f"{t}={v:.3f}" for t, v in by_type.items()))
    print(f"latency_p50_ms {statistics.median(r['ms'] for r in rows):.0f} max_chars {max(r['chars'] for r in rows)}")
    p50, max_chars = statistics.median(r["ms"] for r in rows), max(r["chars"] for r in rows)
    if p50 <= 950 and max_chars <= a.budget * 2.4 * 1.05:
        print("GATES OK")
    print(f"support {statistics.mean(r['support'] for r in rows):.4f}")


if __name__ == "__main__":
    main()
