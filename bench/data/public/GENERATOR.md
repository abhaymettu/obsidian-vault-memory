# Public question set: SilverBullet docs

Corpus: SilverBullet documentation (`docs/` at silverbulletmd/silverbullet@01d6b0e76b6f728a8e466daae618ad9ea41dc0c7),
Copyright 2022 Zef Hemel, MIT License (`CORPUS-LICENSE.md`). Questions and quoted passages are derived from it.
301 notes, 2,226 wikilinks (92% resolve). Picked over Dendron, Logseq, the D&D SRD and Obsidian's help docs for the
licence, the size and a link graph the parser can read.

```sh
git init -q corpus && cd corpus && git remote add origin https://github.com/silverbulletmd/silverbullet
git fetch -q --depth 1 origin 01d6b0e76b6f728a8e466daae618ad9ea41dc0c7 && git checkout -q FETCH_HEAD
```

Generation: written to `bench/QUESTION-SPEC.md` (three types, verbatim evidence, no general-knowledge questions) with
these corpus rules:

- The vault root is `docs/`.
- At most 8 questions take `CHANGELOG.md` as a source; prefer its version and date facts for verbatim questions.
- Do not use `Zef Hemel.md` (a page about a person) as a source.
- Link-hop (two-note) questions use real page links, not image embeds, query prefixes or links inside code blocks.
- A question must need these docs: nothing answerable from general knowledge of SilverBullet or Markdown.

Check: `VAULT_MEMORY_VAULT=corpus/docs python3 bench/check_questions.py <files>`. Split: `bench/build_set.py` rules
(per type, SHA-1 of the id, every third held out).
