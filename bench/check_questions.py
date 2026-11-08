#!/usr/bin/env python3
"""Check benchmark question files: schema, sources exist, evidence quotes appear verbatim in their files.

  python3 bench/check_questions.py FILE... [--frozen bench/data/questions.jsonl]

With --frozen, a new question may not reuse an id from that set, and a note that set already uses as a source is
reported (allowed only as the second note of a multi question)."""
import json
import os
import re
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from vault_memory.config import vault  # noqa: E402

VAULT = vault()
TYPES = {"single", "multi", "verbatim"}
FROZEN_IDS: set[str] = set()
FROZEN_SOURCES: set[str] = set()


def norm(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip()


def check(path: Path) -> int:
    bad = 0
    ids = set()
    counts = Counter()
    for n, line in enumerate(path.read_text().splitlines(), 1):
        if not line.strip():
            continue
        try:
            q = json.loads(line)
        except json.JSONDecodeError as e:
            print(f"line {n}: bad JSON: {e}")
            bad += 1
            continue
        problems = []
        for k in ("id", "type", "question", "gold", "sources", "evidence"):
            if not q.get(k):
                problems.append(f"missing {k}")
        if q.get("id") in ids or q.get("id") in FROZEN_IDS:
            problems.append("duplicate id")
        reused = [s for s in q.get("sources", []) if s in FROZEN_SOURCES]
        if reused and (q.get("type") != "multi" or q.get("sources", [""])[0] in FROZEN_SOURCES):
            problems.append(f"source already used by the frozen set: {reused[0]}")
        ids.add(q.get("id"))
        if q.get("type") not in TYPES:
            problems.append(f"type {q.get('type')!r}")
        if q.get("type") == "verbatim" and not q.get("exact"):
            problems.append("verbatim without exact")
        if q.get("type") == "multi" and len(set(q.get("sources", []))) < 2:
            problems.append("multi with fewer than 2 sources")
        texts = {}
        for s in q.get("sources", []):
            f = VAULT / s
            if not f.is_file():
                problems.append(f"source missing: {s}")
            else:
                texts[s] = norm(f.read_text(errors="replace"))
        cited = set()
        for ev in q.get("evidence", []):
            p, quote = ev.get("path"), ev.get("quote", "")
            if p not in texts:
                problems.append(f"evidence path not in sources: {p}")
                continue
            if norm(quote) not in texts[p]:
                problems.append(f"quote not found in {p}: {quote[:60]!r}")
            cited.add(p)
        for s in texts:
            if s not in cited:
                problems.append(f"source without evidence: {s}")
        if problems:
            bad += 1
            print(f"{q.get('id', f'line {n}')}: " + "; ".join(problems))
        else:
            counts[q["type"]] += 1
    print(f"{path.name}: {sum(counts.values())} ok ({dict(counts)}), {bad} with problems")
    return bad


if __name__ == "__main__":
    args = sys.argv[1:]
    if "--frozen" in args:
        i = args.index("--frozen")
        for line in Path(args[i + 1]).read_text().splitlines():
            if line.strip():
                q = json.loads(line)
                FROZEN_IDS.add(q["id"])
                FROZEN_SOURCES.update(q["sources"])
        del args[i : i + 2]
    sys.exit(1 if sum(check(Path(p)) for p in args) else 0)
