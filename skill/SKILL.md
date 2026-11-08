---
name: vault-memory
description: Use to recall anything from the user's Obsidian vault (the one set in ~/.config/vault-memory/config.toml), including any agent memory folder in it, before grepping. Triggers when a task needs a rule, decision, date, number, path, id, project state or reference that may be in their notes ("what's the rule on...", "when did we decide...", "where does X live", "what did the run find..."). Not for writing to the vault, and not for code search in repos.
---

# vault-memory: one call instead of grepping the vault

`vault-memory recall "<full question>"` returns one small memory block (about 1,500 tokens by default): the most
relevant passages and memory-card facts from the right notes, an `Exact facts:` section with dates, numbers, paths
and ids copied verbatim, and the note path for every line, so you can open a note when you need more.

```
vault-memory recall "what is the overnight usage floor and does it apply in the day?"
vault-memory recall "where does the deploy config live?" --budget 600
vault-memory recall "<question>" --mode synth     # one Sonnet call condenses the block (slower)
```

How to use it well:
- Ask the whole question in plain words, not keywords. One call usually answers it; if the block says nothing
  useful, rephrase once with the names you now know, then fall back to `rg` on the paths it listed.
- Trust paths and exact facts as quoted; they are copied from the notes. Card facts (lines starting with `-` under a
  note) are one model's summary of that note: read the note before acting on a rule that has consequences.
- When two notes disagree, the newer one usually wins: check the dates on the facts and open both notes. Recall does
  not yet rank by recency.
- It reads its own index, not the raw vault. The index refreshes changed notes on every call, so a note saved a
  minute ago is already in it; its memory card arrives on the next `vault-memory cards` run.

Speed: the warm server (`vault-memory serve`) answers in under a second; without it the first call loads the models
(a few seconds). The index lives in `~/.local/share/vault-memory/`, never in the vault.
