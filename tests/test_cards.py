import json
import os
import stat
import time
from pathlib import Path

import pytest

from vault_memory import cards
from vault_memory.recall import Params, recall
from vault_memory.store import Store

NOTES = {
    "Memory/deploy-key.md": "# Deploy key\nThe deploy key rotates on 2026-11-02. Its file is /etc/keys/deploy.pem.",
    "01-Projects/Falcon/README.md": "# Falcon\nFalcon is the bird-tracking side project. Its code lives in ~/code/falcon.",
    "03-Resources/Coffee.md": "# Coffee\nPour-over ratio is one to sixteen. Grind medium fine. Bloom for thirty seconds.",
    "03-Resources/Guitar.md": "# Guitar\nPractice barre chords ten minutes a day before songs. Restring every two months.",
}

FAKE_BATCH = r'''#!/usr/bin/env python3
import json, re, sys
prompt = sys.stdin.read()
with open(sys.argv[0] + ".calls", "a") as f:
    f.write("1\n")
notes = re.findall(r'<note id="(n\d+)" path="([^"]+)"', prompt)
if len(notes) > 1 and __import__("os").path.exists(sys.argv[0] + ".garble-batches"):
    print(json.dumps({"result": "Sorry, too long.", "total_cost_usd": 0.002, "usage": {}}))
    sys.exit(0)
cards = [{"id": i, "about": f"Card for {p}.", "date": "2026-11-02",
          "facts": [{"text": f"The note {p} has a card fact.", "date": "2026-11-02"}],
          "entities": ["Falcon"], "links": []} for i, p in notes]
print(json.dumps({"result": "```json\n" + json.dumps(cards) + "\n```", "total_cost_usd": 0.001, "usage": {}}))
'''


def write(vault: Path, rel: str, text: str) -> None:
    f = vault / rel
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(text)


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("VAULT_MEMORY_HOME", str(Path.home() / ".local/share/vault-memory"))
    vault = tmp_path / "vault"
    for rel, text in NOTES.items():
        write(vault, rel, text)
    s = Store(db_path=tmp_path / "index.sqlite", vault=vault)
    s.refresh()
    return s


@pytest.fixture
def fake_batch(tmp_path, monkeypatch):
    script = tmp_path / "fake-claude"
    script.write_text(FAKE_BATCH)
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setattr("vault_memory.llm.CLAUDE", str(script))
    monkeypatch.setenv("VAULT_MEMORY_SPEND", str(tmp_path / "spend.json"))
    return script


def card(path: str, *facts: tuple[str, str | None]) -> cards.Card:
    return cards.Card(path, "A rule note.", None, [cards.Fact(t, d) for t, d in facts], ["release lane"], [])


def current_hash(store: Store, path: str) -> str:
    title, desc, body = store.db.execute("SELECT title, description, body FROM notes WHERE path=?", (path,)).fetchone()
    return cards.note_hash(title, desc, body)


def test_card_fact_is_recalled_under_its_note_with_its_date(store):
    path = "Memory/deploy-key.md"
    store.save_card(card(path, ("Only the release lane may read the deploy key file.", "2026-10-30")), current_hash(store, path), "test", 0.0)
    m = recall(store, "who is allowed to read the deploy key?", params=Params(budget=300))
    assert m.notes[0] == path
    assert "  - Only the release lane may read the deploy key file. (2026-10-30)" in m.block.splitlines()


def test_no_cards_switch_leaves_facts_out(store):
    path = "Memory/deploy-key.md"
    store.save_card(card(path, ("Only the release lane may read the deploy key file.", None)), current_hash(store, path), "test", 0.0)
    m = recall(store, "who is allowed to read the deploy key?", params=Params(budget=300, fact_k=0))
    assert "release lane may read" not in m.block


def test_edit_drops_the_card_and_a_touch_keeps_it(store):
    path = "03-Resources/Coffee.md"
    store.save_card(card(path, ("The pour-over ratio is one to sixteen.", None)), current_hash(store, path), "test", 0.0)
    time.sleep(0.01)
    os.utime(store.vault / path)
    store.refresh()
    assert store.counts()["cards"] == 1
    time.sleep(0.01)
    write(store.vault, path, "# Coffee\nNow a one to fifteen ratio, with a forty-five second bloom.")
    store.refresh()
    assert (store.counts()["cards"], store.counts()["card_rows"]) == (0, 0)
    assert cards.pending(store, folders=["03-Resources/Coffee"])[0].path == path


def test_extract_saves_every_card_once_and_records_spend(store, fake_batch, tmp_path):
    first = cards.extract(store, workers=2, log=lambda *_: None)
    assert (first.notes, first.failed, first.facts) == (4, 0, 4)
    assert store.counts()["cards"] == 4
    rows = store.db.execute("SELECT path, ord, text, date FROM card_facts WHERE path='01-Projects/Falcon/README.md' ORDER BY ord").fetchall()
    assert rows == [
        ("01-Projects/Falcon/README.md", 0, "Card for 01-Projects/Falcon/README.md. Names: Falcon.", "2026-11-02"),
        ("01-Projects/Falcon/README.md", 1, "The note 01-Projects/Falcon/README.md has a card fact.", "2026-11-02"),
    ]
    again = cards.extract(store, workers=2, log=lambda *_: None)
    assert (again.notes, again.calls) == (0, 0)
    ledger = json.loads((tmp_path / "spend.json").read_text())
    assert ledger["by_method"] == {"cards:haiku": 0.001}


def test_missed_notes_are_retried_one_per_call(store, fake_batch):
    Path(str(fake_batch) + ".garble-batches").touch()
    st = cards.extract(store, workers=2, log=lambda *_: None)
    assert (st.notes, st.failed, st.calls) == (4, 0, 5)
    assert cards.pending(store) == []


def test_garbled_reply_leaves_notes_pending():
    batch = [cards.NoteIn("a.md", "A", "text", "h")]
    assert cards.parse("I could not do that.", batch) == {}
    assert cards.parse('[{"id": "n1", "about": "x", "facts": [{"text": "A fact about note A.", "date": "soon"}]}]', batch)["a.md"].facts == [
        cards.Fact("A fact about note A.", None)
    ]
