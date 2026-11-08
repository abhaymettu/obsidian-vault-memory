# Benchmark question spec

Goal: questions about a vault of Markdown notes that a Claude session would plausibly need answered mid-task, each with
a gold answer that rests on specific notes. They score memory systems (an agent with grep, context-mode search, embedding RAG and
vault-memory) on whether the answer can be recovered.

## Output

Write one JSON object per line to the file your brief names (JSONL, UTF-8). Fields:

```json
{
  "id": "mem-07",
  "type": "single | multi | verbatim",
  "question": "natural question, as a session would ask it",
  "gold": "the full correct answer in one or two sentences",
  "exact": ["strings, any one of which must appear in a correct answer (verbatim type only; empty list otherwise)"],
  "sources": ["vault-relative paths"],
  "evidence": [{"path": "same as a source", "quote": "a verbatim span copied from that file, 20 to 300 chars"}],
  "why_hard": "one line: what a retriever must do to get it (find a paraphrase, join two notes, keep a number exact)"
}
```

## Types (aim for the mix your brief gives)

- **single**: the answer is in one note, but the question does not reuse the note's own distinctive wording. Ask the way
  someone who half-remembers would ask ("what's the rule about pushing to his public repos?", not "what does
  no-push-to-public-without-confirm say?").
- **multi**: the answer needs two notes combined, and neither note states the combined fact (Memorilla's PV4 shape).
  Good shapes: a bridge (note A names the project or tool, note B holds the rule or number for it: "the tool that
  replaced numen for texting him: what is its hourly cap?"), a comparison ("which was decided first, X or Y?"), or a
  composition (a fact from A applied to a fact in B). Both notes go in `sources` and each gets an `evidence` quote.
- **verbatim**: the answer is a specific string a summary would blur: a date, a dollar figure, a count, a file path, a
  command, an id (MAC-1100, H-0042), a version. `exact` lists the accepted forms (e.g. ["2026-11-02", "Nov 2, 2026"]).

## Rules

1. Read the notes yourself. Every `evidence.quote` must be copied character for character from the file (a checker
   greps for it; whitespace runs are normalised, nothing else is). No answer from memory or inference beyond the quotes.
2. The answer must be stable and unambiguous in the vault. If two notes disagree (an older note superseded), either
   skip it or make the question name the time ("as of 2026-11-02") and cite the current note.
3. Prefer facts that matter to how sessions work or to his projects: rules, decisions, dates, numbers, paths, who/what.
   Skip trivia nobody would ask (a tweet's wording, a font size in a scratch note).
4. No credentials, tokens, passwords or personal health details in questions or answers.
5. Avoid `04-Archives/` as a source unless your brief allows it (archives duplicate live notes).
6. Spread over many different notes: no note used by more than two questions in your file.
7. Do not write questions answerable from general knowledge without the vault.
8. Before you finish, re-open each source and confirm the gold answer matches; drop any question you are unsure of.
   Run the checker on your file: `VAULT_MEMORY_VAULT=<vault> python3 bench/check_questions.py <your file>`
   (add `--frozen <existing set>` when extending a set) and fix every line it reports.

When done, reply with: the file path, the count by type, and the checker's last line.
