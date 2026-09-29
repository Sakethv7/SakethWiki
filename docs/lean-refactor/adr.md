# Lean Refactor — Architecture Decision Records

Each ADR states the context, the options considered, the choice, and what the
choice gives up. A decision with no listed downside is not finished being
thought through.

---

## ADR-1 — Unify retrieval on the SQLite memory index; delete `_semantic_page_select`

### Context

There are three retrieval implementations. `memory_store.search()` is a real
hybrid retriever: SQLite full-text search (FTS5) with field weighting, optionally
blended with embedding similarity. `_semantic_page_select()` is an LLM call whose
prompt contains a 200-character excerpt of *every* concept page — for a
175-page vault that is roughly 8,000 input tokens per call. `find_relevant_pages()`
is a keyword scan with a hand-maintained synonym table.

Chat tries all three in sequence. Interview uses only the second and third and
never touches the index that was built for exactly this purpose.

### Options

1. **Keep all three, just make Interview call `memory_store` first.** Smallest
   diff. Leaves two redundant code paths and the expensive LLM selector in place.
2. **Make `_semantic_page_select` the primary everywhere.** It uses the LLM's
   judgment about relevance. But it costs a full-vault-sized prompt on every
   question and gets slower as the vault grows.
3. **Make `memory_store.search()` the single primary, keep `find_relevant_pages`
   as the one fallback, delete `_semantic_page_select`.**

### Choice

Option 3.

### Consequences

- **Gained:** one retrieval path for Chat and Interview; one LLM call removed from
  every Interview question and from Chat's fallback; retrieval latency stops
  growing with vault size; the index finally earns its keep.
- **Given up:** the LLM's semantic judgment in page selection. `memory_store`
  ranks by lexical + vector score, not by "which of these would a smart reader
  pick." In practice the hybrid score is good enough for a personal vault, and
  the weekly trace analysis will surface it if retrieval quality drops. If it
  turns out to matter, the mitigation is a *reranker* over the top 20 hits, not
  a return to scanning every page.
- **Given up:** the synonym table in `find_relevant_pages` still duplicates the
  alias list in `identity.py`. This ADR does not fix that; it is noted for a
  later cleanup so the fallback path does not silently diverge.

---

## ADR-2 — `memory_store.search()` no longer syncs the index on every call

### Context

`search()` defaults to `sync=True` and `/chat` calls it that way. Each sync reads
every file in five folders, parses frontmatter, and calls `parse_concept_page()`
twice per page. Content hashing avoids re-chunking unchanged pages but not the
scan itself. Every chat message pays this before retrieving anything.

### Options

1. **Leave it.** Correctness is never stale, at a fixed per-query cost that grows
   with the vault.
2. **Time-box it** — sync at most once every N seconds. Simpler, but still scans
   on the first query after any idle gap, and picks an arbitrary N.
3. **Event-driven sync.** Sync on startup, after `/approve`, after `/edit-page`,
   and on the existing manual `/memory/reindex`. Queries only read the index.

### Choice

Option 3.

### Consequences

- **Gained:** a chat or interview query does zero filesystem work for retrieval —
  it reads the SQLite index and returns. Latency becomes flat and predictable.
- **Given up:** freshness for out-of-band edits. If a page is changed by `git
  pull`, by Obsidian, or by any path that is not `/approve` or `/edit-page`, the
  index will not reflect it until the next trigger or a restart. For a
  single-user tool where nearly all writes go through the approve flow, this is a
  small and visible gap, and `POST /memory/reindex` is the one-call fix. A
  filesystem watcher (same pattern as `image_watcher`) can close it later if it
  proves annoying; it is deliberately not in this change.
- **Risk:** a sync triggered inside the `/approve` request handler adds latency
  to approval. Mitigation: run it in a background task (`asyncio.create_task`) so
  the approve response returns immediately, exactly as the weekly scheduler is
  already started.

---

## ADR-3 — Interview verification and grading become opt-in

### Context

`/interview` always makes three LLM calls: answer, verify, grade. Verify and
grade route by default to a local Ollama model (`qwen3:14b`) that most setups do
not run, so they silently return "unavailable". They are not in `CRITICAL_TASKS`
and have no Anthropic fallback.

### Options

1. **Fix the default routing** — send verify/grade to the normal cloud provider,
   or add them to `CRITICAL_TASKS` so they fall back to Anthropic. Keeps three
   calls per question.
2. **Make them opt-in** — the answer is always produced; verification runs only
   if the user asks to be graded; grading runs only if the user submitted their
   own answer (already true for grading).

### Choice

Option 2, and also allow Option 1's routing fix as a follow-up so that *when*
verification runs it is reliable.

### Consequences

- **Gained:** the default practice rep is one LLM call, not three. The
  silent-degradation-on-missing-Ollama problem leaves the common path entirely.
  Cost of the feature drops by roughly two-thirds for typical use.
- **Given up:** automatic scoring. If you want a number and a gap list you now
  click "grade me" first. This is a real ergonomic cost; it is accepted because
  the automatic score was frequently `null` anyway and few users want a verifier
  pass on every single question.
- **Frontend change:** a toggle in the Interview tab. CONFIRMED: persist the
  checkbox in `localStorage` (set once, stays), same pattern the codebase
  already uses for `sw_read_history` and Focus Mode onboarding. Guarded with
  `try/catch` so a storage-blocked browser falls back to off.

---

## ADR-4 — Pause the Operations subsystem behind `ENABLE_OPS`, default off

### Context

`system_loop.py` (1,076 lines), `eval_harness.py` (854 lines), the trace critic,
the action-candidate store (73 KB and growing, largely unreviewed), and the
Operations tab (~800 lines of JSX) together form a self-monitoring and
auto-remediation layer. It works. It also serves the operator, not the learner,
and it is a large fraction of the codebase.

The unreviewed 73 KB of staged action candidates is the key evidence: the
bottleneck was never generating suggested changes, it was reviewing them. More
automation upstream does not help a review queue that is already ignored.

### Options

1. **Delete it.** Smallest long-term codebase. Loses the seed for a future
   Langfuse-driven recommendation loop.
2. **Leave it running.** Status quo. Keeps the surface area and the maintenance.
3. **Gate it behind `ENABLE_OPS`, default off.** Code stays in the repo as a
   reference; nothing loads or runs unless explicitly turned on.

### Choice

Option 3.

### Consequences

- **Gained:** the running app is meaningfully smaller — fewer routes, no
  background system loop, one fewer tab, no growing candidate file. Attention
  goes to the knowledge loop.
- **Given up:** passive self-evaluation. Nothing will automatically tell you that
  retrieval coverage dropped or that a task's contract-failure rate rose. This is
  deliberate and temporary: Langfuse will provide the *seeing* part within weeks,
  from real data, and a small purpose-built loop can be rebuilt on top of that.
- **Risk:** gated code rots. Mitigation: it is a documented seed, not a
  maintained feature. If it does not come back within a quarter, delete it then —
  that is a cheap decision to defer, an expensive one to get wrong now.
- **Kept explicitly outside this gate:** `/analyze-traces` and the weekly
  scheduler. They are the knowledge feedback loop, not Operations.

---

## ADR-5 — LLM observability via Langfuse Cloud, wrapped in `llm_client.complete()`

### Context

`telemetry.py` writes JSONL and the Operations tab renders it. With Operations
paused, that reporting surface goes away, but the underlying need — see tokens,
cost, latency, failures across tasks and across projects — remains, and is not
SakethWiki-specific.

### Options

1. **Build a small observability service from scratch.** Full control, matches
   the eventual cross-project goal. Months of work to reach parity with tools
   that are free.
2. **Self-host Langfuse now.** Data stays local. Requires a Postgres + ClickHouse
   + Redis stack to stand up and maintain.
3. **Langfuse Cloud free tier now, wrap `complete()`, keep JSONL writing.**
   10-minute setup, 50k events/month free (well above this app's ~6k/month).
   Identical SDK for a later self-host move — only `LANGFUSE_HOST` changes.

### Choice

Option 3. The from-scratch build, if it happens, is the separate cross-project
project and will likely sit *on top of* a Langfuse backend rather than replace
it.

### Consequences

- **Gained:** a real observability UI across every project that points at the
  same instance, for near-zero effort, with cost/token/latency/failure views out
  of the box.
- **Given up:** local-only data. Traces go to Langfuse Cloud. For a personal
  knowledge tool this is prompts and metadata, not secrets, but it is a real
  change in where data lives. Mitigation: self-host is one env var away and is
  the expected end state once the separate project exists.
- **Given up:** nothing on the JSONL side — it keeps writing, so offline runs and
  the shelved Operations code still have their data.
- **Invariant:** a Langfuse error must never surface to the user. The span code is
  wrapped so any exception is logged and swallowed.

---

## ADR-6 — Lower default retrieval budget; route cheap tasks to local Ollama when available

### Context

`RAG_TOP_K = 10` and `RAG_CONTEXT_BUDGET = 6000` characters. Every chat turn pays
for that context as input tokens, on every turn of the conversation history.
`tag_classify` and `chat_select_pages` are frequent, low-stakes tasks running on
a cloud model.

### Options

1. **Leave the defaults.** Simplest. Known cost.
2. **Lower `TOP_K` to 5 and `BUDGET` to 4000; route the two cheap tasks to Ollama
   when `OLLAMA_BASE_URL` is set, otherwise leave them on the default provider.**

### Choice

Option 2. All four values stay environment-overridable.

### Consequences

- **Gained:** smaller chat prompts (lower input-token cost per turn), and two
  high-frequency tasks moved to a $0 local model for anyone running Ollama.
- **Given up:** on a broad question, a top-5 / 4000-char context may omit a page
  that a top-10 / 6000-char context would have included. This is measurable:
  compare `pages_read` and answer quality on a handful of real questions before
  and after. If recall suffers, raise the numbers via env without a code change.
- **Given up:** determinism of the cheap tasks if Ollama's local model is weaker.
  `chat_select_pages` is being deleted (ADR-1), so in practice only
  `tag_classify` moves. A wrong tag is a low-cost, easily-corrected error.
