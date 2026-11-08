import os
import time
from pathlib import Path

import pytest

from vault_memory.recall import Params, est_tokens, recall
from vault_memory.store import Store

FILLER = [
    ("03-Resources/Coffee.md", "# Coffee\nPour-over ratio is one to sixteen. Grind medium fine. Bloom for thirty seconds."),
    ("03-Resources/Running.md", "# Running\nEasy runs stay conversational. Long run on Sunday along the lake path."),
    ("00-Inbox/Groceries.md", "# Groceries\nOat milk, rice, lentils, spinach, lemons and olive oil for the week."),
    ("03-Resources/Guitar.md", "# Guitar\nPractice barre chords ten minutes a day before songs. Restring every two months."),
]


def write(vault: Path, rel: str, text: str) -> None:
    f = vault / rel
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(text)


@pytest.fixture
def vault(tmp_path, monkeypatch):
    v = tmp_path / "vault"
    monkeypatch.setenv("VAULT_MEMORY_HOME", str(Path.home() / ".local/share/vault-memory"))
    for rel, text in FILLER:
        write(v, rel, text)
    write(v, "Memory/deploy-key.md",
          "---\nname: deploy-key\ndescription: the deploy key rotates on 2026-11-02 and lives at /etc/keys/deploy.pem\n---\n"
          "The deploy key rotates on 2026-11-02. Its file is /etc/keys/deploy.pem and only the release lane reads it.")
    write(v, "01-Projects/Falcon/README.md",
          "# Falcon\nFalcon is the bird-tracking side project. Its code lives in ~/code/falcon. Rules: [[Falcon push rule]].")
    write(v, "Memory/Falcon push rule.md",
          "# Falcon push rule\nEvery push needs his explicit yes, because the repo deploys a public site.")
    return v


def make_store(vault: Path, tmp_path: Path) -> Store:
    return Store(db_path=tmp_path / "index.sqlite", vault=vault)


def test_refresh_is_incremental_and_idempotent(vault, tmp_path):
    store = make_store(vault, tmp_path)
    first = store.refresh()
    assert (first.added, first.changed, first.removed) == (7, 0, 0)
    again = store.refresh()
    assert (again.added, again.changed, again.removed, again.chunks_embedded) == (0, 0, 0, 0)

    time.sleep(0.01)
    write(vault, "03-Resources/Coffee.md", "# Coffee\nNow a one to fifteen ratio.")
    os.remove(vault / "00-Inbox/Groceries.md")
    third = store.refresh()
    assert (third.added, third.changed, third.removed) == (0, 1, 1)
    assert store.counts()["notes"] == 6
    body = store.db.execute("SELECT body FROM notes WHERE path='03-Resources/Coffee.md'").fetchone()[0]
    assert "one to fifteen" in body


def test_wikilinks_resolve_by_basename(vault, tmp_path):
    store = make_store(vault, tmp_path)
    store.refresh()
    edges = store.db.execute("SELECT src, dst FROM links").fetchall()
    assert edges == [("01-Projects/Falcon/README.md", "Memory/Falcon push rule.md")]


def test_recall_keeps_exact_strings_and_provenance(vault, tmp_path):
    store = make_store(vault, tmp_path)
    m = recall(store, "when does the deploy key get rotated, and where is the file?", params=Params(budget=200))
    assert m.notes[0] == "Memory/deploy-key.md"
    assert "2026-11-02" in m.block
    assert "/etc/keys/deploy.pem" in m.block


def test_recall_joins_linked_notes(vault, tmp_path):
    store = make_store(vault, tmp_path)
    m = recall(store, "what must happen before pushing the project in ~/code/falcon?", params=Params(budget=300))
    assert "01-Projects/Falcon/README.md" in m.notes
    assert "Memory/Falcon push rule.md" in m.notes
    assert "explicit yes" in m.block


def test_block_stays_within_budget(vault, tmp_path):
    store = make_store(vault, tmp_path)
    m = recall(store, "guitar practice and running plan", params=Params(budget=60))
    assert est_tokens(m.block) <= 60 * 1.15
