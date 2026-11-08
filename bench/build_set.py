"""Grow the question set (bench/data/[<set>/]questions.jsonl) from checked question files, keeping every existing question and its split.

  uv run python -m bench.build_set DIR [DIR ...]

Run it once per batch: every question in the files must be new (the checker rejects a reused id). Within the new
ones, per type, a third is held out: sorted by
the SHA-1 of the id, every third (0, 3, 6, ...) goes to test. The same rule split the first 96, so the held-out third
stays seeded and nothing already in the set changes split."""
import hashlib
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT.parent))
from bench import QUESTIONS as OUT  # noqa: E402  (VAULT_MEMORY_BENCH_SET picks the set)


def main() -> int:
    files = sorted(f for d in sys.argv[1:] for f in Path(d).glob("*.jsonl"))
    frozen = ["--frozen", str(OUT)] if OUT.exists() else []
    check = subprocess.run([sys.executable, str(ROOT / "check_questions.py"), *map(str, files), *frozen])
    if check.returncode:
        print("checker reported problems; fix the files before merging")
        return 1
    old = [json.loads(l) for l in OUT.read_text().splitlines() if l.strip()] if OUT.exists() else []
    new = [json.loads(l) for f in files for l in f.read_text().splitlines() if l.strip()]
    by_type: dict[str, list[dict]] = {}
    for q in new:
        by_type.setdefault(q["type"], []).append(q)
    for group in by_type.values():
        group.sort(key=lambda q: hashlib.sha1(q["id"].encode()).hexdigest())
        for i, q in enumerate(group):
            q["split"] = "test" if i % 3 == 0 else "dev"
    qs = old + new
    OUT.write_text("".join(json.dumps(q) + "\n" for q in qs))
    n = {s: sum(q["split"] == s for q in qs) for s in ("dev", "test")}
    print(f"{len(old)} kept + {len(new)} new = {len(qs)} questions -> {OUT} {n}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
