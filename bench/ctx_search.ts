// Baseline (b): context-mode's own ContentStore and snippet code (plugin v1.0.169 source) over the vault, queried the way
// ctx_search does.
// Index:  bun bench/ctx_search.ts index <vault> <db>
// Search: bun bench/ctx_search.ts serve <db>   (JSON lines on stdin: {"query": "...", "limit": 2}; one JSON line out each)
import { createInterface } from "node:readline";
import { homedir } from "node:os";

// context-mode 1.0.169's src/store.ts: the installed Claude Code plugin by default, or a checkout of that tag. Its search
// and its snippet code are imported from the user's own copy, never copied here (context-mode is under the Elastic
// License 2.0). The flag keeps server.ts from starting its MCP server on import.
const STORE = process.env.CONTEXT_MODE_STORE ?? `${homedir()}/.claude/plugins/cache/context-mode/context-mode/1.0.169/src/store.ts`;
process.env.CONTEXT_MODE_EMBEDDED_PLUGIN_TOOLS = "1";
const { ContentStore } = await import(STORE);
const { extractSnippet } = await import(STORE.replace(/store\.ts$/, "server.ts"));

const [cmd, a, b] = process.argv.slice(2);
if (cmd === "index") {
  const store = new ContentStore(b);
  const t0 = Date.now();
  const r = store.indexDirectory({
    path: a,
    source: "vault",
    extensions: [".md"],
    exclude: [".obsidian", ".trash", "Attachments", "Templates"],
    maxDepth: 12,
    maxFiles: 10000,
  });
  console.log(JSON.stringify({ ...r, seconds: (Date.now() - t0) / 1000 }));
} else if (cmd === "serve") {
  const store = new ContentStore(a);
  const rl = createInterface({ input: process.stdin });
  for await (const line of rl) {
    const { query, limit = 2 } = JSON.parse(line);
    const t0 = performance.now();
    const results = store.searchWithFallback(query, limit);
    const formatted = results
      .map((r: any) => `--- [${r.source}] ---\n### ${r.title}\n\n${extractSnippet(r.content, query, 1500, r.highlighted)}`)
      .join("\n\n");
    const text = results.length ? `## ${query}\n\n${formatted}` : `## ${query}\nNo results found.`;
    const seconds = (performance.now() - t0) / 1000;
    console.log(JSON.stringify({ text, seconds, sources: results.map((r: any) => r.source) }));
  }
}
