# AGENTS.md

vault-memory is a memory layer for Claude over an Obsidian vault: encode each note once (chunks, embeddings, a
full-text index, the wikilink graph, a card of dated facts), then answer a question with one small memory block
instead of grepping. README.md has the results and the design; this file is for working on the code.

## Setup

```sh
uv sync                                  # Python 3.12, numpy + fastembed (ONNX, no torch)
export VAULT_MEMORY_VAULT=~/Notes        # or a config file, below
uv run vault-memory index                # first build reads the whole vault (about 27 min for 3,600 notes on an M4 Pro)
uv run vault-memory cards                # memory cards through `claude -p` (Haiku), a few notes per call
uv run vault-memory recall "question"    # later calls refresh only changed notes (a stat pass, ~0.08 s)
```

Settings (src/vault_memory/config.py): `VAULT_MEMORY_<NAME>` wins, then `~/.config/vault-memory/config.toml`
(`VAULT_MEMORY_CONFIG` points elsewhere). `vault` is required. Optional: `owner` (whose notes; cards resolve pronouns
to it), `claude` (the `claude -p` command or a wrapper with the same flags), and for the benchmark's agent arms
`memory_md` (an index file a session preloads), `memory_md_lines` (how much of it a session sees, default 200) and
`memory_dir` (the agent memory folder inside the vault). The index and model cache live in
`~/.local/share/vault-memory` (`VAULT_MEMORY_HOME`). Nothing is ever written inside the vault. Model-call spend goes to
the ledger at `VAULT_MEMORY_SPEND`, and a run stops past `VAULT_MEMORY_SPEND_CAP`.

Warm server: `vault-memory serve` keeps the models and vectors loaded and listens on `$VAULT_MEMORY_HOME/sock`;
`vault-memory recall` uses it when the socket exists and runs in-process otherwise. `vault-memory mcp` is a stdio MCP
server with one tool, `recall`.

## Layout

- `src/vault_memory/vault.py`: parse notes (frontmatter, title, wikilinks), chunk by heading into ~140-word windows.
- `src/vault_memory/store.py`: the SQLite store (notes, chunks with float16 embeddings, FTS5, links, cards),
  `refresh()`, and `note_dates()` (a card's stated date, else a date in the path, else the file's mtime).
- `src/vault_memory/cards.py`: memory cards. Extraction prompt, batching, parsing, the retry pass. Cards are keyed by
  the note's content hash.
- `src/vault_memory/recall.py`: retrieval (dense + BM25 fused over chunks and card facts, link neighbours, note-level
  vectors, cross-encoder rerank), optional recency ordering (`Params.recency_*`, `supersede_*`, off by default:
  measured as noise), packing under a token budget with an exact-facts section, and `synth` mode. `fact_k=0` (CLI
  `--no-cards`, `VAULT_MEMORY_CARDS=0`) searches chunks only.
- `src/vault_memory/{cli,daemon,mcp,llm}.py`: entry points; `llm.py` runs one `claude -p` call and records its cost.
- `bench/`: the benchmark. `data/public/` (89 questions on the SilverBullet docs and the corpus licence), `public.sh`
  (the whole public run from a fresh clone), `run.py` (judged runs), `proxy.py` (free deterministic retrieval score),
  `recency_sweep.py` (recency settings on one split, two folds), `card_check.py` (cards against their notes),
  `methods.py` (every system under test), `ctx_search.ts` (baseline: context-mode's own store and snippet code from
  your install, run with bun; `CONTEXT_MODE_STORE`), `report.py`, `figure.py`, `QUESTION-SPEC.md`,
  `check_questions.py` and `build_set.py` (write, check and split a question set for your own vault).
  `VAULT_MEMORY_BENCH_SET` picks the set; without a private `data/questions.jsonl` it is `public`.

## Tests and checks

```sh
uv run pytest -q                                          # index, cards and recall on a fixture vault, fake claude
sh bench/public.sh                                        # the public benchmark end to end (cards cost about $0.40)
uv run python -m bench.proxy --split all                  # retrieval support and the latency gate, no model calls
uv run python -m bench.report --split all                 # the judged table from cached results, no model calls
```

`bench.run` caches per-question results under `bench/results*/` and stops at the spend cap. Never tune on a held-out
split. During a hill-climb, never change the question set, `bench/proxy.py` or `bench/run.py`; that is a restart.
