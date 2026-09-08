# Lean Refactor — Logic Flow

Step-by-step execution paths for the parts this change rewrites. Read alongside
`architecture.md` (component map) and `adr.md` (why each choice).

---

## 1. `POST /chat`

### After the change

1. Reject if `message` is empty (unchanged).
2. `hits = memory_store.search(message, RAG_TOP_K, sync=False)`.
   - Runs FTS5 (and vector similarity if `EMBED_ENABLED`) against the SQLite
     index. No filesystem scan.
3. If `hits` is empty: `relevant_names = vault_reader.find_relevant_pages(message)`.
   - Keyword scan with synonym expansion. The single fallback. No LLM.
4. Build context from `hits` (or from `relevant_names` if the fallback ran),
   trimming to `RAG_CONTEXT_BUDGET`. The existing budget-accounting block
   (chunks used / dropped, chars dropped) is kept as-is.
5. `telemetry.log_context_event("chat_context", {...})` — unchanged.
6. `answer = llm_client.complete(task="chat_answer", ...)` with prompt caching on
   the system block and the context block — unchanged.
7. Attach `knowledge_card` for "what do I know about X" queries — unchanged.
8. Return `{answer, sources, pages_read, knowledge_card?}` — unchanged shape.

### Removed

- The `_semantic_page_select` branch (step "if no memory hits, ask an LLM to pick
  pages from excerpts of every page").
- The `sync=True` behavior inside `memory_store.search`.

### State / retry

Stateless per request. Conversation history is passed in by the client, unchanged.
No retry logic here; `llm_client` owns transient retries.

---

## 2. `POST /interview`

### After the change

1. Reject if `question` is empty (unchanged).
2. Parse request: `question`, `user_answer` (optional), `want_verification`
   (new, default `false`).
3. `hits = memory_store.search(question, RAG_TOP_K, sync=False)`; empty →
   `find_relevant_pages(question)`. **Same retrieval as `/chat`.**
4. Build context, trimmed to `RAG_CONTEXT_BUDGET` (was `RAG_CONTEXT_BUDGET`
   already, but via a different code block — now shares the chat helper).
5. `wiki_answer = llm_client.complete(task="chat_answer", ...)`.
6. **New — write a trace:**
   `_append_trace({event_type: "interview", ts, question, pages_read,
   want_verification, had_user_answer})`.
7. **New — write telemetry:**
   `telemetry.log_context_event("interview_context", {task: "interview",
   query, memory_hits, pages_read, context_chars_used, context_budget})`.
8. If `want_verification` is true:
   `verification = _run_verifier(question, wiki_answer)` inside
   `run_in_executor`; on exception, `{score: null, verdict: "Verification
   unavailable", gaps: []}` (unchanged behavior, now only on this branch).
9. If `user_answer` is present:
   `user_grading = _run_grader(question, wiki_answer, user_answer)`; on
   exception, the existing "unavailable" shape.
10. Return `{wiki_answer, pages_read, verification?, user_grading?}`.
    - `verification` is absent (not null) when `want_verification` is false.

### State transitions

None persisted server-side. The trace row in step 6 is append-only and is what
lets the weekly analysis and any future eval see interview activity. It is
evidence, not a mutation — it never edits a concept page.

### Failure paths

| Condition | Behavior |
|---|---|
| Retrieval empty | Fallback to `find_relevant_pages`; if that is also empty, context is `"No matching pages found yet."` and the answer says the wiki does not cover it. |
| `chat_answer` call fails after retries | 500 to the client, same as `/chat` today. |
| Verifier/grader model down | That sub-result is the "unavailable" shape; `wiki_answer` and the trace still return. |
| Trace or telemetry write fails | Logged, swallowed. Never blocks the response. |

---

## 3. `memory_store` index sync

### How the index stays fresh after the change

Two mechanisms, chosen per situation:

**Targeted single-page updates** (`memory_store.index_page` / `remove_page`) —
already the pattern in the codebase for most write endpoints; this change fills
the gaps:

| Endpoint | Call |
|---|---|
| `POST /approve` | `index_page(page_name)` (pre-existing) |
| `POST /edit-page` | `index_page(page_name)` (pre-existing) |
| `POST /consolidate` | `remove_page(source)` + `index_page(target)` (pre-existing) |
| `DELETE /page/{name}` | `remove_page(name)` (pre-existing) |
| `POST /fix-page` | `index_page(name)` (pre-existing) |
| `POST /add-link` | `index_page(from_page)` **(added)** |
| `POST /create-stub` | `index_page(slug)` **(added)** |
| `POST /quick-note` | `index_page(page.stem)` **(added)** |

**Full `sync_index()`** — for the cases a single slug cannot cover:

| Trigger | How |
|---|---|
| Backend startup | `_schedule_memory_sync()` in the `lifespan` handler — fire-and-forget task, off the request path. |
| After a vault-wide POV rewrite | `sync_index()` at the end of `_run_vault_polish` (already a background thread). |
| `POST /memory/reindex` | Existing endpoint, calls `sync_index()` directly. The escape hatch for edits made outside the app (git pull, Obsidian). |

`_schedule_memory_sync()` swallows its own errors and no-ops if there is no
running event loop (e.g. called from a sync test context).

### What a sync does (unchanged internally)

1. `initialize()` — create tables if missing.
2. Enumerate indexable pages across `cs`, `science`, `humanities`, `insights`,
   `open-threads`.
3. For each page: hash content; if the hash matches the stored one, skip; else
   re-chunk (heading-aware, 900 chars, 120 overlap), embed if enabled, replace
   the page's rows in `chunks` and `chunks_fts`.
4. Remove rows for pages no longer on disk.

### Concurrency

`sync_index` opens its own SQLite connection in WAL mode. Two syncs overlapping
(e.g. two approvals in quick succession) is safe — WAL serializes the writes —
but wasteful. Acceptable for a single-user tool; if it ever matters, guard with
an `asyncio.Lock`.

---

## 4. `ENABLE_OPS` gating

### Backend

At module load in `main.py`:

```
ENABLE_OPS = os.environ.get("ENABLE_OPS", "false").lower() in {"1","true","yes","on"}
```

Operations routes are defined inside `if ENABLE_OPS:` (or registered on a
sub-router that is only `include_router`-ed when the flag is set). Affected
routes: `/system-loop/run`, `/system-loop/actions`, `/system-actions`,
`/system-actions/{id}/approve|reject|eval`, `/trace-critic/run`, `/evals/run`,
`/inference-report`, `/reports/read`, `/operations-overview`.

Not gated (knowledge loop): `/analyze-traces`, `/system-insights`,
`/dashboard-stats`, and the weekly scheduler.

The `import system_loop` and `import eval_harness` lines move inside the flag
check so a disabled install does not even load them.

### Startup

`_weekly_analysis_scheduler` still starts. The Operations system loop, if it had
any background component, starts only under the flag.

### Frontend

`GET /health` (or a small `/config` response) includes `ops_enabled: bool`. The
tab list filters out the Operations entry when false. No dead fetches fire.

### Failure

Flag off + a client hits an Ops route → normal 404. Flag off + frontend somehow
renders the tab → its fetches 404 and the tab shows its error state. Neither is a
crash.

---

## 5. Langfuse span inside `llm_client.complete()`

### Placement

One span wraps the entire primary-plus-fallback sequence, so a span represents
"one logical task result", not one HTTP call. The transient-retry loop is inside
the span.

```
complete(task, ...):
    span = _lf_span_start(task, resolved_model)      # may return None
    try:
        ... existing primary call + contract check + fallback ...
    finally:
        _lf_span_end(span, {
            model, input_tokens, output_tokens, cost_usd,
            latency_ms, contract_ok, fallback_used, fallback_model,
            primary_attempts, fallback_attempts, error
        })
    return text
```

### Rules

- `_lf_span_start` / `_lf_span_end` catch everything internally. If the Langfuse
  SDK is not installed or not configured, they are no-ops.
- Token and cost numbers are the ones already computed for the JSONL log
  (`telemetry.estimate_*` and the `LLM_PRICE_*` table), so both sinks show the
  same figure.
- Span name = task key (e.g. `chat_answer`). Project = set once from
  `LANGFUSE_*` env. A `session_id` equal to the chat/interview request id groups
  multi-call flows (select + answer, or answer + verify + grade) — optional,
  add if it is free to thread through.
- `telemetry.log_llm_call(...)` still runs exactly as now, next to the span.

### Failure

Langfuse down, slow, or erroring → span is dropped, one `logger.warning`, user
request unaffected. There is no retry and no queue for spans; a lost span is
acceptable, a blocked user request is not.

---

## 6. Removals and their blast radius

| Removal | Who referenced it | Fix |
|---|---|---|
| `_semantic_page_select` | `/chat`, `/interview` | Both rewired to `memory_store.search` + `find_relevant_pages`. |
| `GET/POST /active-review` | Frontend uses `/review-queue`; `/active-review` had no caller | Delete the route; keep `active_review.build_queue` (still used by `/review-queue`). |
| `humanities` in `_concept_dirs()` | `list_concept_pages`, `build_graph`, `find_relevant_pages`, Browse tab filters | Drop from the list; remove the empty tab from the frontend folder list. Re-add both in one line if humanities notes start existing. |
| README "Vault Structure" block | Docs only | Rewrite to match disk (`cs/ science/ humanities/ sources/ insights/ open-threads/ lectures/`). |
