"""Read an Obsidian vault into notes, chunks and links. Pure functions over text; no index state."""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

SKIP_DIRS = {".obsidian", ".trash", ".git", "Attachments", "Templates"}
CHUNK_WORDS = 140
WIKILINK = re.compile(r"\[\[([^\]\|#\n]+)(?:#[^\]\|\n]*)?(?:\|[^\]\n]*)?\]\]")
HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")


@dataclass(frozen=True)
class Chunk:
    ord: int
    heading: str
    text: str


@dataclass
class Note:
    path: str
    title: str
    folder: str
    description: str
    body: str
    link_targets: list[str] = field(default_factory=list)
    chunks: list[Chunk] = field(default_factory=list)


def iter_note_files(vault: Path):
    for root, dirs, files in os.walk(vault):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS and not d.startswith(".")]
        for f in files:
            if f.endswith(".md"):
                yield Path(root) / f


def split_frontmatter(text: str) -> tuple[dict[str, str], str]:
    if not text.startswith("---\n"):
        return {}, text
    end = text.find("\n---", 4)
    if end == -1:
        return {}, text
    meta = {}
    for line in text[4:end].splitlines():
        m = re.match(r"^([A-Za-z_][\w-]*):\s*(.*)$", line)
        if m:
            meta[m.group(1).lower()] = m.group(2).strip().strip("\"'")
    rest = text[end + 4 :]
    return meta, rest[rest.find("\n") + 1 :] if "\n" in rest else ""


def parse_note(vault: Path, file: Path) -> Note:
    rel = file.relative_to(vault).as_posix()
    meta, body = split_frontmatter(file.read_text(errors="replace"))
    h1 = re.search(r"^#\s+(.+)$", body, re.M)
    title = meta.get("title") or (h1.group(1).strip() if h1 else "") or file.stem
    folder = rel.rsplit("/", 1)[0] if "/" in rel else ""
    description = meta.get("description", "")
    note = Note(rel, title, folder, description, body, [m.group(1).strip() for m in WIKILINK.finditer(body)])
    note.chunks = chunk_body(body, title, description)
    return note


def chunk_body(body: str, title: str, description: str) -> list[Chunk]:
    """Split on headings, then pack lines into windows of about CHUNK_WORDS words.

    A memory note's one-line description is its own first chunk: it is the human-written summary."""
    chunks: list[Chunk] = []
    if description:
        chunks.append(Chunk(0, "description", description))
    path: list[str] = []
    buf: list[str] = []
    words = 0

    def flush():
        nonlocal buf, words
        text = "\n".join(buf).strip()
        if len(text) > 20:
            chunks.append(Chunk(len(chunks), " > ".join(path), text))
        buf, words = [], 0

    for line in body.splitlines():
        m = HEADING.match(line)
        if m:
            flush()
            level = len(m.group(1))
            path = path[: level - 1] + [m.group(2).strip()]
            continue
        n = len(line.split())
        if words and words + n > CHUNK_WORDS:
            flush()
        if n > CHUNK_WORDS * 2:
            parts = line.split()
            for i in range(0, len(parts), CHUNK_WORDS):
                buf, words = [" ".join(parts[i : i + CHUNK_WORDS])], CHUNK_WORDS
                flush()
            continue
        buf.append(line)
        words += n
    flush()
    return chunks


class LinkResolver:
    """Resolve [[wikilink]] targets the way Obsidian does: full path first, then unique basename."""

    def __init__(self, paths: list[str]):
        self.paths = set(paths)
        self.by_stem: dict[str, str] = {}
        for p in sorted(paths, key=lambda p: (p.count("/"), p)):
            self.by_stem.setdefault(Path(p).stem.lower(), p)

    def resolve(self, target: str) -> str | None:
        t = target.strip().removesuffix(".md")
        if t + ".md" in self.paths:
            return t + ".md"
        return self.by_stem.get(Path(t).name.lower())
