"""Recall: one fixed-size memory block for a question, built from the encoded store.

fast:  hybrid retrieval (dense + BM25, fused) over chunks and over memory-card facts -> link-graph neighbours ->
       cross-encoder rerank -> pack under a budget, with an exact-facts section so names, numbers and paths survive.
       No model call.
synth: the fast slice at a larger budget, condensed by one Sonnet call into a short query-conditioned block."""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field

import numpy as np

from . import config, llm
from .store import Store, day_number, embed_query, passage_text, reranker

STOP = set(
    """a an the and or of to in on for with at by from is are was were be been it its this that these those what which
    who whom when where why how does do did done has have had can could should would will his her their there here
    about into over under than then so if not no yes my me i you your we our us he she they them as any all some
    one two get got use used using make made via per vs""".split()
)
TOKEN_CHARS = 2.4  # measured: Claude tokens per character of these blocks, from the dev run
FACT_LINE = re.compile(r"\d|`|/|\$|@|#\w|[A-Z]{2,}-\d")


@dataclass
class Passage:
    chunk_id: int
    path: str
    title: str
    heading: str
    text: str
    score: float = 0.0
    kind: str = "chunk"
    date: str | None = None


@dataclass
class Memory:
    question: str
    mode: str
    block: str
    notes: list[str] = field(default_factory=list)
    seconds: float = 0.0
    synth_usage: dict | None = None


@dataclass
class Params:
    budget: int = 1500
    dense_k: int = 100
    bm25_k: int = 100
    fused_k: int = 40
    graph_seed_notes: int = 5
    graph_k: int = 15
    note_k: int = 10
    per_note: int = 2
    facts_share: float = 0.15
    synth_slice: int = 3000
    synth_words: int = 150
    rerank_chars: int = 600
    fact_k: int = 20
    facts_per_note: int = 4
    recency_weight: float = 0.0
    recency_half_life: float = 30.0
    supersede_top: int = 0
    supersede_by: str = "both"


def query_terms(q: str) -> list[str]:
    words = re.findall(r"[A-Za-z0-9][A-Za-z0-9_.\-]*", q.lower())
    return [w.strip(".-") for w in words if w not in STOP and len(w.strip(".-")) > 1]


def est_tokens(text: str) -> int:
    return int(len(text) / TOKEN_CHARS) + 1


def _fts_query(terms: list[str]) -> str:
    return " OR ".join('"' + t.replace('"', "") + '"' for t in terms)


def candidates(store: Store, question: str, p: Params) -> tuple[list[int], np.ndarray, np.ndarray]:
    m, ids = store.matrix()
    qv = embed_query(question)
    sims = m @ qv
    fused: dict[int, float] = {}
    if len(ids):
        top = np.argpartition(-sims, min(p.dense_k, len(ids) - 1))[: p.dense_k]
        for rank, i in enumerate(top[np.argsort(-sims[top])]):
            fused[int(ids[i])] = fused.get(int(ids[i]), 0.0) + 1 / (60 + rank)
    terms = query_terms(question)
    if terms:
        rows = store.db.execute(
            "SELECT rowid FROM chunks_fts WHERE chunks_fts MATCH ? ORDER BY bm25(chunks_fts, 4.0, 2.0, 1.0) LIMIT ?",
            (_fts_query(terms), p.bm25_k),
        ).fetchall()
        for rank, (cid,) in enumerate(rows):
            fused[cid] = fused.get(cid, 0.0) + 1 / (60 + rank)
    order = sorted(fused, key=fused.get, reverse=True)[: p.fused_k]
    return order, sims, qv


def fact_candidates(store: Store, question: str, qv: np.ndarray, p: Params) -> list[int]:
    """Card facts by dense similarity and BM25, fused by reciprocal rank: the best fact_k ids."""
    if p.fact_k <= 0:
        return []
    m, ids = store.fact_matrix()
    fused: dict[int, float] = {}
    if len(ids):
        sims = m @ qv
        top = np.argpartition(-sims, min(p.dense_k, len(ids) - 1))[: p.dense_k]
        for rank, i in enumerate(top[np.argsort(-sims[top])]):
            fused[int(ids[i])] = fused.get(int(ids[i]), 0.0) + 1 / (60 + rank)
    terms = query_terms(question)
    if terms and len(ids):
        rows = store.db.execute(
            "SELECT rowid FROM card_fts WHERE card_fts MATCH ? ORDER BY bm25(card_fts, 2.0, 1.0) LIMIT ?",
            (_fts_query(terms), p.bm25_k),
        ).fetchall()
        for rank, (fid,) in enumerate(rows):
            fused[fid] = fused.get(fid, 0.0) + 1 / (60 + rank)
    return sorted(fused, key=fused.get, reverse=True)[: p.fact_k]


def load_facts(store: Store, fact_ids: list[int]) -> list[Passage]:
    if not fact_ids:
        return []
    marks = ",".join("?" * len(fact_ids))
    rows = store.db.execute(
        f"SELECT f.id, f.path, n.title, f.text, f.date FROM card_facts f JOIN notes n ON n.path=f.path WHERE f.id IN ({marks})",
        fact_ids,
    ).fetchall()
    by_id = {r[0]: Passage(-r[0], r[1], r[2], "", r[3], kind="fact", date=r[4]) for r in rows}
    return [by_id[i] for i in fact_ids if i in by_id]


def graph_neighbours(store: Store, seed_paths: list[str], sims: np.ndarray, exclude: set[int], k: int) -> list[int]:
    """Best chunk of each note linked to or from the seed notes, ranked by similarity to the question."""
    if not seed_paths or k <= 0:
        return []
    marks = ",".join("?" * len(seed_paths))
    nbrs = {
        r[0]
        for r in store.db.execute(
            f"SELECT dst FROM links WHERE src IN ({marks}) UNION SELECT src FROM links WHERE dst IN ({marks})",
            seed_paths + seed_paths,
        )
    } - set(seed_paths)
    if not nbrs:
        return []
    _, ids = store.matrix()
    pos = {int(c): i for i, c in enumerate(ids)}
    marks = ",".join("?" * len(nbrs))
    best: dict[str, tuple[float, int]] = {}
    for cid, path in store.db.execute(f"SELECT id, path FROM chunks WHERE path IN ({marks})", list(nbrs)):
        if cid in exclude or cid not in pos:
            continue
        s = float(sims[pos[cid]])
        if path not in best or s > best[path][0]:
            best[path] = (s, cid)
    return [cid for _, cid in sorted(best.values(), reverse=True)[:k]]


def note_level(store: Store, qv: np.ndarray, sims: np.ndarray, k: int) -> list[int]:
    """Best chunk of each of the k notes whose whole-note vector is closest to the question."""
    if k <= 0:
        return []
    note_m, _, row_note = store.note_matrix()
    _, ids = store.matrix()
    top_notes = set(np.argsort(-(note_m @ qv))[:k].tolist())
    best: dict[int, tuple[float, int]] = {}
    for row in np.nonzero(np.isin(row_note, list(top_notes)))[0]:
        n = int(row_note[row])
        if n not in best or sims[row] > best[n][0]:
            best[n] = (float(sims[row]), int(ids[row]))
    return [cid for _, cid in sorted(best.values(), reverse=True)]


def load_passages(store: Store, chunk_ids: list[int]) -> list[Passage]:
    if not chunk_ids:
        return []
    marks = ",".join("?" * len(chunk_ids))
    rows = store.db.execute(
        f"SELECT c.id, c.path, n.title, c.heading, c.text FROM chunks c JOIN notes n ON n.path=c.path WHERE c.id IN ({marks})",
        chunk_ids,
    ).fetchall()
    by_id = {r[0]: Passage(*r) for r in rows}
    return [by_id[i] for i in chunk_ids if i in by_id]


def rank(store: Store, question: str, p: Params) -> list[Passage]:
    fused, sims, qv = candidates(store, question, p)
    seeds: list[str] = []
    for ps in load_passages(store, fused):
        if ps.path not in seeds:
            seeds.append(ps.path)
    extra = graph_neighbours(store, seeds[: p.graph_seed_notes], sims, set(fused), p.graph_k)
    extra += [c for c in note_level(store, qv, sims, p.note_k) if c not in set(fused + extra)]
    passages = load_passages(store, fused + extra) + load_facts(store, fact_candidates(store, question, qv, p))
    if not passages:
        return []
    docs = [passage_text(ps.title, ps.heading, ps.text)[: p.rerank_chars] for ps in passages]
    for ps, s in zip(passages, reranker().rerank(question, docs)):
        ps.score = float(s)
    return by_recency(store, sorted(passages, key=lambda ps: ps.score, reverse=True), p)


def title_key(title: str) -> str:
    """A title without its dates and numbers, so 'Plan 2026-10-01' and 'Plan 2026-11-02' compare equal."""
    return " ".join(re.findall(r"[a-z][a-z0-9]+", re.sub(r"20\d\d-\d\d-\d\d|\b\d{1,4}\b", " ", title.lower())))


def related_pairs(store: Store, titles: dict[str, str], by: str) -> set[tuple[str, str]]:
    """Pairs of distinct notes (path -> title) that link each other ("links"), share a title key ("titles"), or either."""
    paths = list(titles)
    pairs: set[tuple[str, str]] = set()
    if by in ("links", "both") and len(paths) > 1:
        marks = ",".join("?" * len(paths))
        pairs |= set(store.db.execute(f"SELECT src, dst FROM links WHERE src IN ({marks}) AND dst IN ({marks})", paths + paths))
    if by in ("titles", "both"):
        keys: dict[str, list[str]] = {}
        for path, title in titles.items():
            if k := title_key(title):
                keys.setdefault(k, []).append(path)
        pairs |= {(a, b) for group in keys.values() for a in group for b in group if a != b}
    return pairs


def by_recency(store: Store, ranked: list[Passage], p: Params) -> list[Passage]:
    """Newer memory ranks higher, in two steps on the reranked scores.

    Decay: a passage gains recency_weight when its note (for a card fact, the fact) is dated on the vault's newest day,
    halving every recency_half_life days before that. Supersede: of two related notes among the top supersede_top
    passages, the newer one's best passage moves just above the older one's, since a newer note on the same subject
    usually records what changed. Both stay in the pool; only their order changes."""
    if not ranked or (p.recency_weight <= 0 and p.supersede_top <= 0):
        return ranked
    dates = store.note_dates()
    newest = max(dates.values())
    day = {id(ps): min((day_number(ps.date) if ps.kind == "fact" else None) or dates.get(ps.path, newest), newest) for ps in ranked}
    if p.recency_weight > 0:
        for ps in ranked:
            ps.score += p.recency_weight * 0.5 ** ((newest - day[id(ps)]) / p.recency_half_life)
        ranked.sort(key=lambda ps: ps.score, reverse=True)
    if p.supersede_top > 0:
        best: dict[str, float] = {}
        for ps in ranked:
            best.setdefault(ps.path, ps.score)
        lift: dict[str, float] = {}
        for old, new in related_pairs(store, {ps.path: ps.title for ps in ranked[: p.supersede_top]}, p.supersede_by):
            if dates.get(new, 0) > dates.get(old, 0) and best[old] >= best[new]:
                lift[new] = max(lift.get(new, 0.0), best[old] - best[new] + 1e-3)
        for ps in ranked:
            ps.score += lift.get(ps.path, 0.0)
        ranked.sort(key=lambda ps: ps.score, reverse=True)
    return ranked


def fact_lines(store: Store, paths: list[str], terms: list[str], shown: str, limit_chars: int) -> list[tuple[str, str]]:
    """Lines from the chosen notes that carry an exact string (number, date, path, id, command) and touch the question."""
    stems = [t[:5] for t in terms]
    out, used = [], 0
    for path in paths:
        body = store.db.execute("SELECT body FROM notes WHERE path=?", (path,)).fetchone()
        for line in (body[0] if body else "").splitlines():
            line = line.strip(" -*\t")
            low = line.lower()
            if len(line) < 12 or line in shown or not FACT_LINE.search(line):
                continue
            hits = sum(1 for s in stems if s in low)
            if hits < max(1, min(2, len(stems) // 3)):
                continue
            line = line[:300]
            if used + len(line) > limit_chars:
                return out
            out.append((path, line))
            used += len(line)
    return out


def render_passage(ps: Passage) -> str:
    if ps.kind == "fact":
        return f"  - {ps.text.strip()}" + (f" ({ps.date})" if ps.date and ps.date not in ps.text else "")
    head = ps.heading.split(" > ")[-1] if ps.heading and ps.heading != "description" else ""
    return "  " + (f"{head}: " if head else "") + re.sub(r"\s*\n\s*", " / ", ps.text.strip())


def pack(store: Store, question: str, ranked: list[Passage], p: Params) -> tuple[str, list[str]]:
    """Fill the budget (in rendered characters) with the best passages, grouped by note, then exact-fact lines."""
    budget_chars = int(p.budget * TOKEN_CHARS)
    header = f"Memory for: {question}"
    room = budget_chars - len(header) - 1 - int(budget_chars * p.facts_share)
    per_note: dict[str, list[str]] = {}
    titles: dict[str, str] = {}
    used: dict[tuple[str, str], int] = {}
    for ps in ranked:
        group = per_note.get(ps.path, [])
        if used.get((ps.path, ps.kind), 0) >= (p.facts_per_note if ps.kind == "fact" else p.per_note):
            continue
        line = render_passage(ps)
        cost = len(line) + 1 + (0 if group else len(f"[00] {ps.path} ({ps.title})") + 1)
        if cost > room:
            if room - (cost - len(line)) > 160:
                line = line[: room - (cost - len(line)) - 4] + " ..."
                cost = room
            else:
                continue
        per_note.setdefault(ps.path, []).append(line)
        used[(ps.path, ps.kind)] = used.get((ps.path, ps.kind), 0) + 1
        titles[ps.path] = ps.title
        room -= cost
        if room < 160:
            break
    notes = list(per_note)
    lines = [header]
    for n, path in enumerate(notes, 1):
        lines.append(f"[{n}] {path} ({titles[path]})")
        lines.extend(per_note[path])
    shown = "\n".join(lines)
    left = budget_chars - len(shown) - len("\nExact facts:")
    facts = fact_lines(store, notes[:4], query_terms(question), shown, left - 8 * len(notes[:4]))
    if facts:
        lines.append("Exact facts:")
        lines.extend(f"- [{notes.index(path) + 1}] {line}" for path, line in facts)
    return "\n".join(lines), notes


SYNTH_PROMPT = """You are the memory layer for a Claude session working for {owner}. Below are excerpts from their Obsidian vault,
ranked for the question. Write the memory block the session needs to answer it.

Rules:
- At most {words} words. Facts only, no preamble.
- Copy names, numbers, dates, paths, commands and ids exactly as the excerpts write them.
- Combine facts across notes when the question needs it, and say which note each fact comes from as [n].
- If the excerpts do not answer the question, write the closest facts they do hold and then "Not in memory: <what is missing>".

Question: {question}

Excerpts:
{slice}
"""


def recall(store: Store, question: str, mode: str = "fast", params: Params | None = None, refresh: bool = True) -> Memory:
    p = params or Params()
    t0 = time.time()
    if refresh:
        store.refresh()
    ranked = rank(store, question, p)
    if mode == "fast":
        block, notes = pack(store, question, ranked, p)
        return Memory(question, mode, block, notes, time.time() - t0)
    if mode == "synth":
        wide = Params(**{**p.__dict__, "budget": p.synth_slice})
        slice_block, notes = pack(store, question, ranked, wide)
        out = llm.ask(SYNTH_PROMPT.format(owner=config.owner(), words=p.synth_words, question=question, slice=slice_block))
        sources = "\n".join(f"[{n}] {path}" for n, path in enumerate(notes, 1))
        block = f"Memory for: {question}\n{out.text.strip()}\nSources:\n{sources}"
        return Memory(question, mode, block, notes, time.time() - t0, out.usage_summary())
    raise ValueError(f"unknown mode {mode!r}; trained mode is a proposal, see RESULT.md")
