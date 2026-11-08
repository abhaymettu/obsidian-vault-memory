"""Print the results table from cached per-question results (no model calls) and write it as JSON for the figure.

  uv run python -m bench.report --split test"""
import argparse
import json
from pathlib import Path

from .run import QUESTIONS, RESULTS, summarize

METHODS = ["grep_agent", "ctx2", "ctx5", "dense5", "vm_fast", "vm_fast_cards", "vm_agent", "vm_agent_cards",
           "vm_fast_cards@recency", "vm_agent_cards@recency"]
COLS = ["method", "n", "correct", "c/p/w", "single", "multi", "verbatim", "source_hits", "tokens", "side_tokens", "seconds_p50", "e2e_p50"]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--split", default="test", choices=["dev", "test", "all"])
    a = ap.parse_args()
    qs = [json.loads(l) for l in QUESTIONS.read_text().splitlines() if l.strip()]
    ids = [q["id"] for q in qs if a.split == "all" or q["split"] == a.split]
    rows = []
    for m in METHODS:
        files = [RESULTS / m / f"{i}.json" for i in ids]
        if all(f.exists() for f in files):
            rows.append(summarize(m, [json.loads(f.read_text()) for f in files]))
    out = RESULTS / f"report-{a.split}.json"
    out.write_text(json.dumps(rows, indent=1))
    print("| " + " | ".join(COLS) + " |\n|" + "---|" * len(COLS))
    for r in rows:
        print("| " + " | ".join(str(r[c]) for c in COLS) + " |")


if __name__ == "__main__":
    main()
