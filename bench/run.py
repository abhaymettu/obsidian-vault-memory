"""Run the vault benchmark: retrieve, answer with a fixed reader, grade, and print the table.

  uv run python -m bench.run --split test --methods grep_agent,ctx2,ctx5,dense5,vm_fast,vm_synth [--fresh] [--budget 900]

Results cache in bench/results/<method>/<qid>.json; --fresh reruns them. Spend (API-equivalent, from the CLI's own
cost report) accumulates in $VAULT_MEMORY_SPEND (default: the lane's bench/spend.json) and the run stops at $VAULT_MEMORY_SPEND_CAP."""
from __future__ import annotations

import argparse
import json
import os
import re
import statistics
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from vault_memory import llm
from vault_memory.recall import Params

from .methods import build, index_text

from . import QUESTIONS, RESULTS, ROOT
os.environ.setdefault("VAULT_MEMORY_SPEND", str(Path(__file__).parent / "spend.json"))
os.environ.setdefault("VAULT_MEMORY_SPEND_CAP", "45")
AGENTS = {"grep_agent", "vm_agent", "vm_agent_cards"}

READER = """Answer the question using only the memory below, retrieved from a personal Obsidian vault.
If the memory does not contain the answer, reply "Not in memory."
Answer in one or two sentences. Copy names, numbers, dates, paths and ids exactly as written.

<memory>
{block}
</memory>

Question: {question}"""

JUDGE = """You grade an answer against a gold answer for a question about someone's notes.

Question: {question}
Gold answer: {gold}
Candidate answer: {answer}

CORRECT: the candidate states the gold answer's key facts (wording may differ, extra correct detail is fine).
PARTIAL: it gets some key facts but misses or garbles others.
WRONG: wrong, missing, or says the answer is not available.
Reply with exactly one word: CORRECT, PARTIAL, or WRONG."""


spend = llm.record


def ask(method: str, prompt: str) -> llm.Reply:
    r = llm.ask(prompt)
    spend(method, r.cost_usd)
    return r


def norm(s: str) -> str:
    return re.sub(r"\s+", " ", s.replace("`", "").replace("*", "")).strip().lower()


def grade(method: str, q: dict, answer: str) -> dict:
    j = ask(method, JUDGE.format(question=q["question"], gold=q["gold"], answer=answer))
    verdict = (re.findall(r"CORRECT|PARTIAL|WRONG", j.text.upper()) or ["WRONG"])[0]
    judged = {"CORRECT": 1.0, "PARTIAL": 0.5, "WRONG": 0.0}[verdict]
    if q["type"] == "verbatim":
        exact = 1.0 if any(norm(e) in norm(answer) for e in q["exact"]) else 0.0
        return {"score": exact, "judge": verdict, "exact": exact}
    return {"score": judged, "judge": verdict}


def source_hits(q: dict, text: str) -> float:
    return sum(s in text for s in q["sources"]) / len(q["sources"])


def _empty_agent(fn) -> int:
    empty = fn({"question": "Reply with the single word ok. Use no tools."})
    spend("calibration", empty.meta["cost_usd"])
    return empty.meta["first_context"]


def _reader_base() -> int:
    reader = llm.ask(READER.format(block="", question=""))
    spend("calibration", reader.cost_usd)
    return reader.input_tokens


def _grep_agent_bases(cal: dict) -> dict:
    from .methods import grep_agent

    memory_tokens = 0
    if index := index_text():
        with_mem = llm.ask(READER.format(block=index, question=""))
        spend("calibration", with_mem.cost_usd)
        memory_tokens = with_mem.input_tokens - cal["reader_base"]
    return {"memory_md_tokens": memory_tokens, "agent_base": _empty_agent(grep_agent) - memory_tokens}


def calibrate(names: list[str]) -> dict:
    """Template overheads so 'tokens into Claude' counts only the question plus what the method put in context.
    Each overhead is measured once per results folder, and only for the methods being run."""
    from .methods import vm_agent

    path = RESULTS / "calibration.json"
    cal = json.loads(path.read_text()) if path.exists() else {}
    if "reader_base" not in cal:
        cal["reader_base"] = _reader_base()
    if "grep_agent" in names and "agent_base" not in cal:
        cal |= _grep_agent_bases(cal)
    if {"vm_agent", "vm_agent_cards"} & set(names) and "vm_agent_base" not in cal:
        cal["vm_agent_base"] = _empty_agent(vm_agent)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(cal, indent=1))
    return cal


def run_method(name: str, fn, qs: list[dict], fresh: bool, cal: dict, workers: int) -> list[dict]:
    out_dir = RESULTS / name
    out_dir.mkdir(parents=True, exist_ok=True)
    todo = [q for q in qs if fresh or not (out_dir / f"{q['id']}.json").exists()]
    retrieved = {}
    if name.split("@")[0] in AGENTS:
        with ThreadPoolExecutor(workers) as ex:
            for q, r in zip(todo, ex.map(fn, todo)):
                spend(name, r.meta["cost_usd"])
                retrieved[q["id"]] = r
    else:
        for q in todo:
            retrieved[q["id"]] = fn(q)
            su = retrieved[q["id"]].meta.get("synth_usage")
            if su:
                spend(name, su["cost_usd"])

    def finish(q: dict) -> None:
        r = retrieved[q["id"]]
        if r.answer is not None:
            answer, tokens = r.answer, r.meta["final_context"] - cal[r.meta["base_key"]]
            e2e, hits = r.seconds, source_hits(q, r.meta["seen"])
        else:
            rep = ask(name, READER.format(block=r.block, question=q["question"]))
            answer, tokens = rep.text, rep.input_tokens - cal["reader_base"]
            e2e, hits = r.seconds + rep.seconds, source_hits(q, r.block)
        rec = {"id": q["id"], "type": q["type"], "answer": answer, "tokens": tokens, "seconds": round(r.seconds, 3),
               "e2e_seconds": round(e2e, 2), "source_hits": hits, "block": r.block, "meta": r.meta, **grade(name, q, answer)}
        (out_dir / f"{q['id']}.json").write_text(json.dumps(rec, indent=1))

    with ThreadPoolExecutor(workers) as ex:
        list(ex.map(finish, todo))
    return [json.loads((out_dir / f"{q['id']}.json").read_text()) for q in qs]


def summarize(name: str, recs: list[dict]) -> dict:
    by_type = {t: statistics.mean([r["score"] for r in recs if r["type"] == t] or [0]) for t in ("single", "multi", "verbatim")}
    side = [r["meta"].get("synth_usage", {}) or {} for r in recs]
    return {
        "method": name,
        "n": len(recs),
        "correct": round(statistics.mean(r["score"] for r in recs), 3),
        "c/p/w": "/".join(str(sum(r["score"] == v for r in recs)) for v in (1.0, 0.5, 0.0)),
        **{t: round(v, 3) for t, v in by_type.items()},
        "source_hits": round(statistics.mean(r["source_hits"] for r in recs), 3),
        "tokens": round(statistics.mean(r["tokens"] for r in recs)),
        "side_tokens": round(statistics.mean(s.get("input_tokens", 0) for s in side)),
        "seconds_p50": round(statistics.median(r["seconds"] for r in recs), 3),
        "seconds_mean": round(statistics.mean(r["seconds"] for r in recs), 3),
        "e2e_p50": round(statistics.median(r["e2e_seconds"] for r in recs), 2),
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--split", default="dev", choices=["dev", "test", "all"])
    ap.add_argument("--methods", default="ctx2,ctx5,dense5,vm_fast")
    ap.add_argument("--fresh", action="store_true")
    ap.add_argument("--budget", type=int, default=Params.budget)
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--tag", default="", help="suffix for result dirs, e.g. a hill-climb round")
    ap.add_argument("--only", default="", help="comma-separated question ids")
    a = ap.parse_args()
    qs = [json.loads(l) for l in QUESTIONS.read_text().splitlines() if l.strip()]
    qs = [q for q in qs if a.split == "all" or q["split"] == a.split]
    if a.only:
        qs = [q for q in qs if q["id"] in a.only.split(",")]
    names = a.methods.split(",")
    cal = calibrate(names)
    fns = build(names, Params(budget=a.budget))
    rows = []
    for name in names:
        recs = run_method(name + (f"@{a.tag}" if a.tag else ""), fns[name], qs, a.fresh, cal, a.workers)
        rows.append(summarize(name + (f"@{a.tag}" if a.tag else ""), recs))
        print(json.dumps(rows[-1]), flush=True)
    table = ROOT / f"results/table-{a.split}{a.tag}.json"
    table.write_text(json.dumps(rows, indent=1))
    cols = ["method", "n", "correct", "c/p/w", "single", "multi", "verbatim", "source_hits", "tokens", "side_tokens", "seconds_p50", "e2e_p50"]
    print("| " + " | ".join(cols) + " |\n|" + "---|" * len(cols))
    for r in rows:
        print("| " + " | ".join(str(r[c]) for c in cols) + " |")


if __name__ == "__main__":
    main()
