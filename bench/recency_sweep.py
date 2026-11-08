"""Tune the recency settings on one split without re-running retrieval.

Each question's reranked pool is built once with recency off; every setting then re-sorts and re-packs that pool, which
takes milliseconds. The split is cut into two folds by question id: a setting chosen on one fold is scored on the other,
so the out-of-fold gain it prints is not a fit to the questions the setting was picked on.

  uv run python -m bench.recency_sweep --split dev"""
from __future__ import annotations

import argparse
import dataclasses
import itertools
import json
import statistics
import zlib

from vault_memory.recall import Params, by_recency, pack, rank
from vault_memory.store import Store

from . import QUESTIONS
from .proxy import support

WEIGHTS = [0.5, 1, 2, 3, 4, 6]
HALF_LIVES = [7, 14, 30, 60, 120]
TOPS = [10, 20, 40]
RELATIONS = ["links", "titles", "both"]


def settings() -> list[dict]:
    decay = [{"recency_weight": 0.0}] + [{"recency_weight": w, "recency_half_life": h} for w, h in itertools.product(WEIGHTS, HALF_LIVES)]
    supersede = [{"supersede_top": 0}] + [{"supersede_top": t, "supersede_by": r} for t, r in itertools.product(TOPS, RELATIONS)]
    return [{**d, **s} for d, s in itertools.product(decay, supersede)]


def label(s: dict) -> str:
    return ",".join(f"{k}={v}" for k, v in s.items())


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--split", default="dev")
    ap.add_argument("--budget", type=int, default=1500)
    ap.add_argument("--out", default="", help="write every setting's per-fold scores to this JSON file")
    a = ap.parse_args()
    qs = [json.loads(l) for l in QUESTIONS.read_text().splitlines() if l.strip()]
    qs = [q for q in qs if q["split"] == a.split]
    store = Store()
    store.refresh()
    base = Params(budget=a.budget)
    pools = [rank(store, q["question"], base) for q in qs]
    fold = [zlib.crc32(q["id"].encode()) % 2 for q in qs]

    scores: dict[str, list[float]] = {}
    for s in settings():
        p = dataclasses.replace(base, **s)
        row = []
        for q, pool in zip(qs, pools):
            ranked = by_recency(store, [dataclasses.replace(ps) for ps in pool], p)
            row.append(support(q, pack(store, q["question"], ranked, p)[0]))
        scores[label(s)] = row

    def mean(name: str, f: int | None = None) -> float:
        return statistics.mean(v for v, k in zip(scores[name], fold) if f is None or k == f)

    off = label({"recency_weight": 0.0, "supersede_top": 0})
    print(f"questions {len(qs)} (fold sizes {fold.count(0)}, {fold.count(1)}); off: {mean(off):.4f}")
    for f in (0, 1):
        pick = max(scores, key=lambda n: mean(n, f))
        print(f"chosen on fold {f}: {pick}  in-fold {mean(pick, f) - mean(off, f):+.4f}  out-of-fold {mean(pick, 1 - f) - mean(off, 1 - f):+.4f}")
    ranked_settings = sorted(scores, key=mean, reverse=True)
    print("best on the whole split:")
    for name in ranked_settings[:8]:
        up = sum(x > y + 1e-9 for x, y in zip(scores[name], scores[off]))
        down = sum(x < y - 1e-9 for x, y in zip(scores[name], scores[off]))
        types = {t: statistics.mean(v - o for v, o, q in zip(scores[name], scores[off], qs) if q["type"] == t) for t in ("single", "multi", "verbatim")}
        print(f"  {mean(name):.4f} ({mean(name) - mean(off):+.4f}; up {up}, down {down}; "
              + " ".join(f"{t} {d:+.3f}" for t, d in types.items()) + f")  {name}")
    for only in ("supersede_top=0", "recency_weight=0.0,"):
        name = max((n for n in scores if only in n), key=mean)
        print(f"best with {'decay only' if only.startswith('supersede') else 'supersede only'}: {mean(name):.4f}  {name}")
    if a.out:
        with open(a.out, "w") as fh:
            json.dump({"ids": [q["id"] for q in qs], "fold": fold, "scores": scores}, fh)


if __name__ == "__main__":
    main()
