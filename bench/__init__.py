"""The benchmark. VAULT_MEMORY_BENCH_SET picks the question set: a name such as "public" reads
data/public/questions.jsonl and writes results-public/; empty means the owner's own set (data/questions.jsonl, results
in results/). Unset, it is the owner's set when that file exists and "public" otherwise."""
import os
from pathlib import Path

ROOT = Path(__file__).parent
SET = os.environ.get("VAULT_MEMORY_BENCH_SET", "" if (ROOT / "data" / "questions.jsonl").exists() else "public")
QUESTIONS = ROOT / "data" / SET / "questions.jsonl"
RESULTS = ROOT / (f"results-{SET}" if SET else "results")
