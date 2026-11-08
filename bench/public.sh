#!/bin/sh
# The public benchmark from a fresh clone: fetch the SilverBullet docs at the pinned commit, index them, extract memory
# cards, score retrieval for free, and print the judged table from the cached results in bench/results-public.
#
#   sh bench/public.sh            # index + cards (about $0.40 of Haiku through `claude -p`) + free proxy + cached table
#   NO_CARDS=1 sh bench/public.sh # skip the model calls; the proxy then runs without card facts
#   sh bench/public.sh --judged   # also rerun every single-shot arm with the Sonnet reader and judge (about $2)
set -eu
cd "$(dirname "$0")/.."
export VAULT_MEMORY_HOME="${VAULT_MEMORY_HOME:-$HOME/.local/share/vault-memory-public}"
export VAULT_MEMORY_VAULT="$VAULT_MEMORY_HOME/corpus/docs"
export VAULT_MEMORY_OWNER="the SilverBullet project"
export VAULT_MEMORY_BENCH_SET=public
commit=01d6b0e76b6f728a8e466daae618ad9ea41dc0c7
corpus="$VAULT_MEMORY_HOME/corpus"
if [ "$(git -C "$corpus" rev-parse HEAD 2>/dev/null)" != "$commit" ]; then
  mkdir -p "$corpus"
  git -C "$corpus" init -q
  git -C "$corpus" remote add origin https://github.com/silverbulletmd/silverbullet 2>/dev/null || true
  git -C "$corpus" fetch -q --depth 1 origin "$commit"
  git -C "$corpus" checkout -q FETCH_HEAD
fi
uv sync -q
uv run vault-memory index
if [ -z "${NO_CARDS:-}" ]; then
  uv run vault-memory cards
fi
uv run python -m bench.proxy --split all
if [ "${1:-}" = "--judged" ]; then
  methods=dense5,vm_fast,vm_fast_cards
  store="${CONTEXT_MODE_STORE:-$HOME/.claude/plugins/cache/context-mode/context-mode/1.0.169/src/store.ts}"
  if command -v bun >/dev/null && [ -e "$store" ]; then
    mkdir -p "$VAULT_MEMORY_HOME/bench"
    [ -e "$VAULT_MEMORY_HOME/bench/ctx.db" ] || bun bench/ctx_search.ts index "$VAULT_MEMORY_VAULT" "$VAULT_MEMORY_HOME/bench/ctx.db"
    methods=ctx2,ctx5,$methods
  else
    echo "context-mode baseline skipped: needs bun and context-mode 1.0.169's src/store.ts (CONTEXT_MODE_STORE)"
  fi
  uv run python -m bench.run --split all --methods "$methods" --fresh
fi
uv run python -m bench.report --split all
