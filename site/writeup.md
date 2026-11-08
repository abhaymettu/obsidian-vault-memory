# vault-memory

Claude Code sessions remember things by preloading an index file and grepping a notes folder for the rest. It works,
and on my own vault the index alone is about 10,000 tokens before a session has asked anything. I wanted to know
whether one small block of memory could give a session the same answers, so I built a memory layer over an Obsidian
vault and measured it against grep and against search, on questions checked against the notes.

## What it does

Each note is encoded once, when it changes: chunks, embeddings, a full-text index, the wikilink graph, and a short card
of dated facts that a small model writes. A question gets back about 1,500 tokens. The block has the passages that
answer it, an exact-facts section for the dates, numbers, paths and ids a model must not paraphrase, and the note every
line came from. Nothing reads the raw vault at question time, and a note saved a minute ago is already in the index.

## What it found

On the SilverBullet documentation, which anyone can download, a session that recalls instead of grepping scored 0.949
against 0.978 for a session with Grep, Glob and Read. That is four fewer answers fully correct out of 89, with 26% fewer
tokens in context. A single recall call tied keyword search there (0.781 against 0.775 for context-mode at five hits),
and won on questions that need two notes.

On my own vault, 3,614 notes and 110 held-out questions, the gap to grep is a little wider and the saving much bigger.
The recall agent scored 0.877 against 0.936, with 2,167 tokens in context against 15,147, most of which is the index
grep preloads. One recall call scored 0.841 against 0.709 for search. The cards did most of the single-call gain: 0.795
without them, 0.841 with them, at the same block size.

It beats search, and it doesn't beat grep yet.

## Where grep still wins

Most of the questions grep gets and recall misses are about current state. An older note says one thing, a newer note
says it changed, and the session that read the index first sees the change. I tried recency first. Ranking newer notes
higher, and putting the newer of two linked notes first, changed nothing beyond run-to-run variation, on the dev
questions and on the held-out ones.

Then I put each note's date in the block with one line saying the newer of two notes is current. That made it worse:
0.810 against 0.822 and 0.829 for two runs of the unchanged system. The reader started overriding right answers with
any newer note, and in a working vault a newer note that links an older one usually adds to it instead of replacing
it. The next version needs a labelled change: a card fact that says what replaced what, kept only when the newer
note's own words back it and checked the way the card facts already are.

## How it was measured

The questions come in three kinds: the answer sits in one note, needs two notes combined, or is an exact string such as
a date or a path. Every question carries the verbatim evidence it was written from, and a checker rejects any quote not
found in its note. A seeded third of each kind is held out from every tuning decision. A fixed reader answers from what
each system returns, and a judge grades the answer as correct, partial or wrong. Scores are the mean of those grades.
Two runs of the same code differ by about 0.007, so I treat smaller differences as noise.

The private vault's questions quote my notes, so they stay private. The public set, the corpus pin, every cached answer
and the scripts that rerun them are in the repo, and `sh bench/public.sh` runs the whole public benchmark from a fresh
clone.
