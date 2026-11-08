"""Write-time memory cards: each note's facts, entities, dates and links, extracted once by a cheap model.

A card is keyed by the note's content hash, so only new or edited notes are re-extracted. Each fact is stored as its
own row with an embedding and a full-text entry, so recall can search facts beside chunks. A fact names its subject
(the extraction prompt forbids bare pronouns), carries its note path through the row, and its date when the note
dates it."""
from __future__ import annotations

import hashlib
import json
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field

from . import config, llm

VERSION = 1
NOTE_CHARS = 30_000
BATCH_CHARS = 14_000
BATCH_NOTES = 5
DATE = re.compile(r"^\d{4}-\d{2}(-\d{2})?$")

PROMPT = """You write memory cards for notes in a personal Obsidian vault. The vault belongs to {owner}. A search later
finds single facts by meaning or keywords, so each fact must stand on its own.

For each note below, write one card:
- "about": one sentence: what the note is (its subject and kind: a rule, decision, project state, plan, meeting, log,
  reference, summary).
- "date": the date the note is about or was written, as YYYY-MM-DD, if the note states it (frontmatter, title or text);
  otherwise null.
- "facts": the note's specific facts, one claim each, each a full sentence that names its subject (never a bare "it",
  "this", "he" or "the tool": write the name). Keep names, numbers, dates, money, paths, commands, ids, versions and
  quoted rules exactly as the note writes them. Cover rules and why they exist, decisions and who made them, dates of
  events, numbers and results, where things live, what replaced what. Give each fact "date" (YYYY-MM-DD) when the note
  dates that fact, otherwise null. Skip boilerplate, navigation and formatting. A short note may need 2 facts, a long
  one up to 25.
- "entities": people, projects, tools, products, companies and ids the note names, spelled as written.
- "links": other notes, projects or files the note points to (wikilink targets, file paths), as written.
Use only what the note says: no inference, no outside knowledge.

Reply with only a JSON array, one object per note, in the order given:
[{{"id": "n1", "about": "...", "date": null, "facts": [{{"text": "...", "date": null}}], "entities": [], "links": []}}]

{notes}"""


@dataclass
class Fact:
    text: str
    date: str | None = None


@dataclass
class Card:
    path: str
    about: str
    date: str | None
    facts: list[Fact] = field(default_factory=list)
    entities: list[str] = field(default_factory=list)
    links: list[str] = field(default_factory=list)


@dataclass
class NoteIn:
    path: str
    title: str
    text: str
    hash: str


@dataclass
class ExtractStats:
    notes: int = 0
    calls: int = 0
    failed: int = 0
    facts: int = 0
    cost_usd: float = 0.0
    seconds: float = 0.0


def note_hash(title: str, description: str, body: str) -> str:
    return hashlib.sha1(f"{VERSION}\0{title}\0{description}\0{body}".encode()).hexdigest()


def pending(store, folders: list[str] | None = None, skip: tuple[str, ...] = ()) -> list[NoteIn]:
    """Notes with no card for their current content, archives last, largest first within each group (packs better)."""
    have = dict(store.db.execute("SELECT path, note_hash FROM cards"))
    out = []
    for path, title, description, body in store.db.execute("SELECT path, title, description, body FROM notes"):
        if folders and not any(path.startswith(f) for f in folders):
            continue
        if any(path.startswith(s) for s in skip):
            continue
        h = note_hash(title, description or "", body or "")
        if have.get(path) == h:
            continue
        text = (f"description: {description}\n" if description else "") + (body or "")
        if len(text.strip()) < 40:
            continue
        out.append(NoteIn(path, title, text[:NOTE_CHARS], h))
    out.sort(key=lambda n: (n.path.startswith("04-Archives/"), -len(n.text)))
    return out


def batches(notes: list[NoteIn]) -> list[list[NoteIn]]:
    out: list[list[NoteIn]] = []
    cur: list[NoteIn] = []
    size = 0
    for n in notes:
        if cur and (size + len(n.text) > BATCH_CHARS or len(cur) >= BATCH_NOTES):
            out.append(cur)
            cur, size = [], 0
        cur.append(n)
        size += len(n.text)
    if cur:
        out.append(cur)
    return out


def render_prompt(batch: list[NoteIn]) -> str:
    notes = "\n\n".join(
        f'<note id="n{i}" path="{n.path}" title="{n.title}">\n{n.text}\n</note>' for i, n in enumerate(batch, 1)
    )
    return PROMPT.format(notes=notes, owner=config.owner())


def _date(v) -> str | None:
    return v if isinstance(v, str) and DATE.match(v.strip()) else None


def _strings(v) -> list[str]:
    return [s.strip() for s in v if isinstance(s, str) and s.strip()][:40] if isinstance(v, list) else []


def parse(text: str, batch: list[NoteIn]) -> dict[str, Card]:
    """Cards by path from the model's reply; notes the reply skipped or garbled are left out (retried next run)."""
    start, end = text.find("["), text.rfind("]")
    try:
        items = json.loads(text[start : end + 1]) if start != -1 else []
    except json.JSONDecodeError:
        return {}
    by_id = {f"n{i}": n for i, n in enumerate(batch, 1)}
    out = {}
    for it in items if isinstance(items, list) else []:
        if not isinstance(it, dict) or it.get("id") not in by_id:
            continue
        facts = [
            Fact(f["text"].strip(), _date(f.get("date")))
            for f in it.get("facts") or []
            if isinstance(f, dict) and isinstance(f.get("text"), str) and len(f["text"].strip()) > 8
        ][:40]
        about = it.get("about") if isinstance(it.get("about"), str) else ""
        n = by_id[it["id"]]
        out[n.path] = Card(n.path, about.strip(), _date(it.get("date")), facts, _strings(it.get("entities")), _strings(it.get("links")))
    return out


def card_rows(card: Card) -> list[tuple[int, str, str | None]]:
    """The searchable rows of a card: ord 0 is the about line with its entities, then one row per fact."""
    head = card.about + (f" Names: {', '.join(card.entities)}." if card.entities else "")
    rows = [(0, head, card.date)] if head.strip() else []
    rows += [(i, f.text, f.date) for i, f in enumerate(card.facts, 1)]
    return rows


def extract(store, model: str = "haiku", workers: int = 8, limit: int = 0, folders: list[str] | None = None,
            skip: tuple[str, ...] = (), log=print) -> ExtractStats:
    """Extract cards for every note without a current one. Idempotent: a rerun only picks up what is still missing.

    Notes a call misses (a garbled or failed reply) are resent once, one note per call."""
    t0 = time.time()
    todo = pending(store, folders, skip)
    if limit:
        todo = todo[:limit]
    stats = ExtractStats()
    jobs = batches(todo)
    log(f"cards: {len(todo)} notes in {len(jobs)} calls, model {model}")
    missed = _run(store, jobs, model, workers, stats, t0, log)
    if missed:
        log(f"cards: retrying {len(missed)} missed notes one per call")
        missed = _run(store, [[n] for n in missed], model, workers, stats, t0, log)
    stats.failed = len(missed)
    stats.seconds = time.time() - t0
    return stats


def _run(store, jobs: list[list[NoteIn]], model: str, workers: int, stats: ExtractStats, t0: float, log) -> list[NoteIn]:
    missed: list[NoteIn] = []
    with ThreadPoolExecutor(workers) as ex:
        futs = {ex.submit(llm.ask, render_prompt(b), model, None, 600): b for b in jobs}
        for fut in as_completed(futs):
            batch = futs[fut]
            try:
                reply = fut.result()
            except Exception as e:  # a timeout or a crashed call: the notes go to the retry pass
                log(f"cards: call failed ({e!r})")
                missed += batch
                continue
            stats.calls += 1
            stats.cost_usd += reply.cost_usd
            cards = {} if reply.is_error else parse(reply.text, batch)
            size = sum(len(n.text) for n in batch)
            for n in batch:
                if n.path not in cards:
                    missed.append(n)
                    continue
                store.save_card(cards[n.path], n.hash, model, reply.cost_usd * len(n.text) / size)
                stats.notes += 1
                stats.facts += len(cards[n.path].facts)
            try:
                llm.record(f"cards:{model}", reply.cost_usd)
            except llm.SpendCap:
                ex.shutdown(wait=False, cancel_futures=True)
                raise
            if stats.calls % 25 == 0:
                log(f"cards: {stats.calls} calls, {stats.notes} notes, {stats.facts} facts, "
                    f"${stats.cost_usd:.2f}, {time.time() - t0:.0f}s")
    return missed
