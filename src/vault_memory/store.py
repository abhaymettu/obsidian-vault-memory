"""The encoded vault: notes, chunks with embeddings, a full-text index and the link graph, in one SQLite file.

Encoding happens once per note change (refresh). Recall reads only this store, never the raw vault."""
from __future__ import annotations

import os
import re
import sqlite3
import time
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import numpy as np

from . import cards as cards_mod
from . import config
from .vault import LinkResolver, iter_note_files, parse_note

_PATH_DATE = re.compile(r"20\d\d-\d\d-\d\d")


def day_number(stamp) -> int | None:
    """A YYYY-MM-DD string (or regex match of one) as a day number; None when absent or not a real date."""
    text = stamp.group(0) if isinstance(stamp, re.Match) else stamp
    try:
        return date.fromisoformat(text[:10]).toordinal() if text else None
    except ValueError:
        return None


EMBED_MODEL = "BAAI/bge-small-en-v1.5"
RERANK_MODEL = "Xenova/ms-marco-MiniLM-L-6-v2"

SCHEMA = """
CREATE TABLE IF NOT EXISTS notes(path TEXT PRIMARY KEY, title TEXT, folder TEXT, description TEXT,
  mtime REAL, size INTEGER, body TEXT, link_targets TEXT);
CREATE TABLE IF NOT EXISTS chunks(id INTEGER PRIMARY KEY, path TEXT, ord INTEGER, heading TEXT, text TEXT, emb BLOB);
CREATE INDEX IF NOT EXISTS chunks_path ON chunks(path);
CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(title, heading, text, tokenize='porter unicode61');
CREATE TABLE IF NOT EXISTS links(src TEXT, dst TEXT);
CREATE INDEX IF NOT EXISTS links_src ON links(src);
CREATE INDEX IF NOT EXISTS links_dst ON links(dst);
CREATE TABLE IF NOT EXISTS cards(path TEXT PRIMARY KEY, note_hash TEXT, model TEXT, about TEXT, note_date TEXT,
  entities TEXT, links TEXT, cost_usd REAL, extracted_at REAL);
CREATE TABLE IF NOT EXISTS card_facts(id INTEGER PRIMARY KEY, path TEXT, ord INTEGER, text TEXT, date TEXT, emb BLOB);
CREATE INDEX IF NOT EXISTS card_facts_path ON card_facts(path);
CREATE VIRTUAL TABLE IF NOT EXISTS card_fts USING fts5(title, text, tokenize='porter unicode61');
"""


def home() -> Path:
    return Path(os.environ.get("VAULT_MEMORY_HOME", Path.home() / ".local/share/vault-memory"))


def vault_root() -> Path:
    return config.vault()


_models: dict[str, object] = {}


def embedder():
    if "embed" not in _models:
        from fastembed import TextEmbedding

        _models["embed"] = TextEmbedding(EMBED_MODEL, cache_dir=str(home() / "models"))
    return _models["embed"]


def reranker():
    if "rerank" not in _models:
        from fastembed.rerank.cross_encoder import TextCrossEncoder

        _models["rerank"] = TextCrossEncoder(RERANK_MODEL, cache_dir=str(home() / "models"))
    return _models["rerank"]


def embed_passages(texts: list[str]) -> np.ndarray:
    if not texts:
        return np.zeros((0, 384), dtype=np.float32)
    return np.asarray(list(embedder().embed(texts, batch_size=64)), dtype=np.float32)


def embed_query(text: str) -> np.ndarray:
    return np.asarray(next(iter(embedder().query_embed([text]))), dtype=np.float32)


def passage_text(title: str, heading: str, text: str) -> str:
    return f"{title} | {heading}\n{text}" if heading else f"{title}\n{text}"


@dataclass
class RefreshStats:
    added: int = 0
    changed: int = 0
    removed: int = 0
    chunks_embedded: int = 0
    seconds: float = 0.0


class Store:
    def __init__(self, db_path: Path | None = None, vault: Path | None = None):
        self.vault = vault or vault_root()
        self.db_path = db_path or home() / "index.sqlite"
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.db_path, check_same_thread=False, timeout=30)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(SCHEMA)
        self._matrix: np.ndarray | None = None
        self._ids: np.ndarray | None = None
        self._facts: tuple[np.ndarray, np.ndarray] | None = None
        self._facts_sig: tuple | None = None
        self._dates: dict[str, int] | None = None

    def refresh(self) -> RefreshStats:
        """Bring the store in line with the vault: re-encode notes whose mtime or size changed. Idempotent."""
        t0 = time.time()
        stats = RefreshStats()
        on_disk = {}
        for f in iter_note_files(self.vault):
            st = f.stat()
            on_disk[f.relative_to(self.vault).as_posix()] = (f, st.st_mtime, st.st_size)
        known = {p: (m, s) for p, m, s in self.db.execute("SELECT path, mtime, size FROM notes")}
        gone = [p for p in known if p not in on_disk]
        todo = [p for p, (_, m, s) in on_disk.items() if known.get(p) != (m, s)]
        if not gone and not todo:
            self._check_facts()
            stats.seconds = time.time() - t0
            return stats
        with self.db:
            for p in gone + todo:
                self._delete(p)
            stats.removed = len(gone)
            self._drop_cards(gone)
            pending = []
            for p in todo:
                f, m, s = on_disk[p]
                note = parse_note(self.vault, f)
                if self._card_hash(p) not in (None, cards_mod.note_hash(note.title, note.description, note.body)):
                    self._drop_cards([p])
                stats.changed += p in known
                stats.added += p not in known
                self.db.execute(
                    "INSERT INTO notes VALUES (?,?,?,?,?,?,?,?)",
                    (p, note.title, note.folder, note.description, m, s, note.body, "\n".join(note.link_targets)),
                )
                for c in note.chunks:
                    pending.append((p, note.title, c))
            for start in range(0, len(pending), 512):
                batch = pending[start : start + 512]
                vecs = embed_passages([passage_text(t, c.heading, c.text) for _, t, c in batch])
                for (p, t, c), v in zip(batch, vecs):
                    cur = self.db.execute(
                        "INSERT INTO chunks(path, ord, heading, text, emb) VALUES (?,?,?,?,?)",
                        (p, c.ord, c.heading, c.text, v.astype(np.float16).tobytes()),
                    )
                    self.db.execute(
                        "INSERT INTO chunks_fts(rowid, title, heading, text) VALUES (?,?,?,?)",
                        (cur.lastrowid, t, c.heading, c.text),
                    )
                stats.chunks_embedded += len(batch)
            self._rebuild_links()
        self._matrix = None
        self._dates = None
        self._check_facts()
        stats.seconds = time.time() - t0
        return stats

    def _delete(self, path: str) -> None:
        ids = [r[0] for r in self.db.execute("SELECT id FROM chunks WHERE path=?", (path,))]
        self.db.executemany("DELETE FROM chunks_fts WHERE rowid=?", [(i,) for i in ids])
        self.db.execute("DELETE FROM chunks WHERE path=?", (path,))
        self.db.execute("DELETE FROM notes WHERE path=?", (path,))

    def _card_hash(self, path: str) -> str | None:
        row = self.db.execute("SELECT note_hash FROM cards WHERE path=?", (path,)).fetchone()
        return row[0] if row else None

    def _drop_cards(self, paths: list[str]) -> None:
        for p in paths:
            ids = [r[0] for r in self.db.execute("SELECT id FROM card_facts WHERE path=?", (p,))]
            self.db.executemany("DELETE FROM card_fts WHERE rowid=?", [(i,) for i in ids])
            self.db.execute("DELETE FROM card_facts WHERE path=?", (p,))
            self.db.execute("DELETE FROM cards WHERE path=?", (p,))

    def save_card(self, card, note_hash: str, model: str, cost_usd: float) -> None:
        """Replace a note's card and its searchable rows (one embedding and one full-text entry per row)."""
        title = (self.db.execute("SELECT title FROM notes WHERE path=?", (card.path,)).fetchone() or [""])[0]
        rows = cards_mod.card_rows(card)
        vecs = embed_passages([f"{title} | {text}" for _, text, _ in rows])
        with self.db:
            self._drop_cards([card.path])
            self.db.execute(
                "INSERT INTO cards VALUES (?,?,?,?,?,?,?,?,?)",
                (card.path, note_hash, model, card.about, card.date, "\n".join(card.entities), "\n".join(card.links),
                 cost_usd, time.time()),
            )
            for (ord_, text, date), v in zip(rows, vecs):
                cur = self.db.execute(
                    "INSERT INTO card_facts(path, ord, text, date, emb) VALUES (?,?,?,?,?)",
                    (card.path, ord_, text, date, v.astype(np.float16).tobytes()),
                )
                self.db.execute("INSERT INTO card_fts(rowid, title, text) VALUES (?,?,?)", (cur.lastrowid, title, text))

    def _check_facts(self) -> None:
        """Drop the cached fact matrix when another process (a card extraction run) changed the card rows."""
        sig = self.db.execute("SELECT count(*), max(id) FROM card_facts").fetchone()
        if sig != self._facts_sig:
            self._facts, self._facts_sig, self._dates = None, sig, None

    def note_dates(self) -> dict[str, int]:
        """Each note's date as a day number: the date its card says it was written or is about, else a date in its
        path, else its file's modified time. Sync tools rewrite modified times in bulk, so a stated date wins."""
        if self._dates is None:
            rows = self.db.execute("SELECT n.path, n.mtime, c.note_date FROM notes n LEFT JOIN cards c ON c.path=n.path")
            today = date.today().toordinal()
            self._dates = {p: min(today, day_number(stated) or day_number(_PATH_DATE.search(p)) or date.fromtimestamp(m).toordinal()) for p, m, stated in rows}
        return self._dates

    def fact_matrix(self) -> tuple[np.ndarray, np.ndarray]:
        if self._facts is None:
            rows = self.db.execute("SELECT id, emb FROM card_facts ORDER BY id").fetchall()
            ids = np.array([r[0] for r in rows], dtype=np.int64)
            m = np.frombuffer(b"".join(r[1] for r in rows), dtype=np.float16).reshape(len(rows), -1) if rows else np.zeros((0, 384), np.float16)
            self._facts = (m.astype(np.float32), ids)
            self._facts_sig = self.db.execute("SELECT count(*), max(id) FROM card_facts").fetchone()
        return self._facts

    def _rebuild_links(self) -> None:
        rows = self.db.execute("SELECT path, link_targets FROM notes").fetchall()
        resolver = LinkResolver([p for p, _ in rows])
        edges = set()
        for src, targets in rows:
            for t in filter(None, (targets or "").split("\n")):
                dst = resolver.resolve(t)
                if dst and dst != src:
                    edges.add((src, dst))
        self.db.execute("DELETE FROM links")
        self.db.executemany("INSERT INTO links VALUES (?,?)", sorted(edges))

    def matrix(self) -> tuple[np.ndarray, np.ndarray]:
        if self._matrix is None:
            rows = self.db.execute("SELECT id, emb FROM chunks ORDER BY id").fetchall()
            self._ids = np.array([r[0] for r in rows], dtype=np.int64)
            m = np.frombuffer(b"".join(r[1] for r in rows), dtype=np.float16).reshape(len(rows), -1)
            self._matrix = m.astype(np.float32)
        return self._matrix, self._ids

    def note_matrix(self) -> tuple[np.ndarray, list[str], np.ndarray]:
        """One vector per note (normalised mean of its chunk vectors), the note paths, and each chunk row's note index."""
        if getattr(self, "_notes", None) is None or self._matrix is None:
            m, ids = self.matrix()
            path_of = dict(self.db.execute("SELECT id, path FROM chunks"))
            paths = sorted(set(path_of.values()))
            index = {p: i for i, p in enumerate(paths)}
            row_note = np.array([index[path_of[int(c)]] for c in ids], dtype=np.int64)
            sums = np.zeros((len(paths), m.shape[1]), dtype=np.float32)
            np.add.at(sums, row_note, m)
            sums /= np.linalg.norm(sums, axis=1, keepdims=True) + 1e-9
            self._notes = (sums, paths, row_note)
        return self._notes

    def counts(self) -> dict[str, int]:
        q = lambda sql: self.db.execute(sql).fetchone()[0]
        return {
            "notes": q("SELECT count(*) FROM notes"),
            "chunks": q("SELECT count(*) FROM chunks"),
            "links": q("SELECT count(*) FROM links"),
            "cards": q("SELECT count(*) FROM cards"),
            "card_rows": q("SELECT count(*) FROM card_facts"),
        }
