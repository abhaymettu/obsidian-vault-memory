"""Spot-check memory cards against their notes before a full extraction.

  uv run python -m bench.card_check --model haiku --n 30

Samples n notes (seeded, outside 04-Archives), extracts their cards with the given model into the index, then scores:
- exact fidelity: share of facts whose exact strings (numbers, dates, paths, ids, commands) all appear in the note;
- support and coverage: a Sonnet judge reads note and card, lists facts the note does not support and important facts
  the card misses, and rates coverage 1 to 5.
Pass bar, fixed before the first run: unsupported facts at most 5%, exact fidelity at least 95%, mean coverage at least
3.5. A model that fails the bar is not used for the full run."""
from __future__ import annotations

import argparse
import json
import random
import re
import statistics
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from vault_memory import cards, llm
from vault_memory.store import Store

from . import run  # noqa: F401  (sets the spend ledger and cap)

OUT = Path(__file__).parent / "results/card_check"
EXACT = re.compile(r"`[^`]+`|\$[\d,.]+|\b\d[\d,.:/-]*\d\b|\b[\w.-]*/[\w./-]+|\b[A-Z]{2,}-\d+\b")

JUDGE = """Check a memory card against the note it was extracted from.

<note path="{path}">
{note}
</note>

<card>
{card}
</card>

Reply with only JSON:
{{"unsupported": [numbers of card facts the note does not support or gets wrong],
 "missing": ["important specific facts in the note that the card leaves out, at most 5"],
 "coverage": 1 to 5 (5: every important specific fact is on the card)}}"""


def norm(s: str) -> str:
    return re.sub(r"\s+", " ", s.replace("`", "").replace("*", "")).lower()


def exact_ok(fact: str, note: str) -> bool:
    return all(norm(x.strip("`")) in note for x in EXACT.findall(fact))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="haiku")
    ap.add_argument("--n", type=int, default=30)
    ap.add_argument("--seed", type=int, default=7)
    a = ap.parse_args()
    store = Store()
    store.refresh()
    rows = store.db.execute("SELECT path, title, description, body FROM notes WHERE path NOT LIKE '04-Archives/%'").fetchall()
    rows = [r for r in rows if len(r[3] or "") >= 300]
    sample = random.Random(a.seed).sample(sorted(rows), a.n)
    notes = []
    for path, title, desc, body in sample:
        text = (f"description: {desc}\n" if desc else "") + (body or "")
        notes.append(cards.NoteIn(path, title, text[: cards.NOTE_CHARS], cards.note_hash(title, desc or "", body or "")))

    def one(batch):
        reply = llm.ask(cards.render_prompt(batch), a.model, None, 600)
        llm.record(f"card_check:{a.model}", reply.cost_usd)
        return batch, reply

    got: dict[str, cards.Card] = {}
    with ThreadPoolExecutor(8) as ex:
        for batch, reply in ex.map(one, cards.batches(notes)):
            parsed = cards.parse(reply.text, batch)
            size = sum(len(n.text) for n in batch)
            for n in batch:
                if n.path in parsed:
                    store.save_card(parsed[n.path], n.hash, a.model, reply.cost_usd * len(n.text) / size)
                    got[n.path] = parsed[n.path]

    def judge(n: cards.NoteIn) -> dict:
        c = got[n.path]
        listing = "\n".join(f"{i}. {f.text}" + (f" ({f.date})" if f.date else "") for i, f in enumerate(c.facts, 1))
        reply = llm.ask(JUDGE.format(path=n.path, note=n.text, card=f"about: {c.about}\n{listing}"), "sonnet")
        llm.record("card_check:judge", reply.cost_usd)
        m = re.search(r"\{.*\}", reply.text, re.S)
        v = json.loads(m.group(0)) if m else {"unsupported": [], "missing": ["judge reply unparsed"], "coverage": 0}
        nt = norm(n.text)
        return {"path": n.path, "facts": len(c.facts), "exact_ok": sum(exact_ok(f.text, nt) for f in c.facts),
                "unsupported": len(v.get("unsupported") or []), "coverage": v.get("coverage", 0),
                "missing": v.get("missing", []), "card": {"about": c.about, "facts": [f.text for f in c.facts]}}

    with ThreadPoolExecutor(8) as ex:
        res = list(ex.map(judge, [n for n in notes if n.path in got]))
    facts = sum(r["facts"] for r in res) or 1
    summary = {
        "model": a.model,
        "notes": len(notes),
        "cards": len(got),
        "facts": facts,
        "unsupported_share": round(sum(r["unsupported"] for r in res) / facts, 3),
        "exact_fidelity": round(sum(r["exact_ok"] for r in res) / facts, 3),
        "coverage_mean": round(statistics.mean(r["coverage"] for r in res), 2),
    }
    summary["pass"] = (summary["cards"] == len(notes) and summary["unsupported_share"] <= 0.05
                       and summary["exact_fidelity"] >= 0.95 and summary["coverage_mean"] >= 3.5)
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / f"{a.model}.json").write_text(json.dumps({"summary": summary, "notes": res}, indent=1))
    print(json.dumps(summary))


if __name__ == "__main__":
    main()
