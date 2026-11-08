#!/bin/sh
# Rerun every system on the held-out test split from scratch, then print the table (about $12 API-equivalent).
# Needs: the index and cards built (vault-memory index; vault-memory cards), the warm server for the agent arms
# (vault-memory serve), bun for the context-mode baseline. VAULT_MEMORY_BENCH_SET=public runs the public set
# (single-shot arms only: the agent arms need the private vault's MEMORY.md).
set -e
cd "$(dirname "$0")/.."
uv run python -m bench.run --split test --methods ctx2,ctx5,dense5,vm_fast,vm_fast_cards --fresh
if [ -z "$VAULT_MEMORY_BENCH_SET" ]; then
  uv run python -m bench.run --split test --methods grep_agent,vm_agent,vm_agent_cards --workers 4 --fresh
fi
uv run python -m bench.report --split test
