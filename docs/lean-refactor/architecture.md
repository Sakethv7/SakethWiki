# Lean Refactor — Architecture

## Purpose of this change

SakethWiki works, but three things drag on it: retrieval does redundant
filesystem work on every query, the Interview feature is disconnected from the
rest of the system, and a large self-monitoring subsystem ("Operations") adds
surface area without serving the core capture → understand → recall loop.

This change makes retrieval single-path and cheap, puts Interview inside the same
feedback and observability loop as Chat, pauses the Operations subsystem behind a
flag, and moves LLM observability to Langfuse. It removes code and configuration;
it does not add a new capability.

## Complexity tier

**Single-process web app.** One FastAPI backend, one React single-page frontend,
a Markdown vault on disk, and a SQLite index derived from that vault. That tier
is correct for a personal tool used by one person, and this change keeps it —
in fact it moves *toward* the low end of that tier by shelving the Operations
loop. No queue broker, no worker pool, no service split is introduced or implied.

The one new external dependency is Langfuse Cloud, reached over HTTPS from the
backend. It is a sink for telemetry only. If it is unreachable the app must keep
working unchanged, so it is not on any critical path.

## Components touched

The term **task** below means a named LLM job — `chat_answer`, `ingest_extract`,
`interview_verify`, and so on. Every task routes through one function,
`llm_client.complete()`, which is why observability can be added in one place.

| Component | File | What changes |
|---|---|---|
| LLM client | `backend/llm_client.py` | Wrap each provider call in a Langfuse span. Never let a telemetry failure raise into the caller. |
| Memory index | `backend/memory_store.py` | `search()` stops rebuilding the index on every call. Index sync becomes event-driven. |
| Chat endpoint | `backend/main.py` `/chat` | Collapse the three-way retrieval fallback to one primary path plus one fallback. |
| Interview endpoint | `backend/main.py` `/interview` | Use `memory_store.search()` for retrieval. Write a trace and a telemetry event. Make verify/grade opt-in. |
| Route registration | `backend/main.py` | Operations routes register only when `ENABLE_OPS` is true. |
| Frontend tabs | `frontend/src/App.jsx` | Operations tab hidden when the backend reports Ops disabled. Interview gets a "grade me" toggle. |
| Config | `.env.example`, tunable constants | New env vars for Langfuse and `ENABLE_OPS`. Lower default retrieval budget. |
| Dead code | several | Remove `/active-review` (duplicate of `/review-queue`), drop the empty `humanities` folder from the concept-dir list, fix the stale README vault-structure section. |

## What stays exactly as it is

- **The Markdown vault is still the single source of truth.** Nothing in this
  change writes vault files differently.
- **The weekly self-learning loop stays on.** `_weekly_analysis_scheduler`,
  `/analyze-traces`, and `system-insights.md` are the *knowledge* feedback loop
  (approve → trace → weekly analysis → better extraction prompt). That is
  on-mission and is **not** part of "Operations". It keeps running regardless of
  `ENABLE_OPS`.
- **`telemetry.py` keeps writing its JSONL files.** Langfuse is added next to it,
  not in place of it.
- **The transient-retry logic** added in `llm_client` recently is untouched; the
  Langfuse span wraps the whole retry sequence so one span = one logical task
  attempt.

## Out of scope (explicitly)

- Splitting `main.py` / `App.jsx` into modules — separate task, later.
- Replacing `test_e2e.py`'s live LLM calls with mocks — separate task, later.
- The standalone cross-project observability layer — its own project, its own
  design docs.
- The future "read Langfuse, post 1–3 weekly recommendations" loop — not built
  until there are several weeks of real Langfuse data to design it from.

## Data flow — retrieval, before and after

Before, `/chat` and `/interview` each did their own thing, and `/chat` tried
three retrieval strategies in sequence.

```
BEFORE

/chat question
  └─ memory_store.search(sync=True)          ← re-reads every vault file first
       └─ if no hits: _semantic_page_select  ← LLM reads excerpts of ALL pages
            └─ if still none: find_relevant_pages  ← keyword scan of all files
  └─ build context → chat_answer LLM call

/interview question
  └─ _semantic_page_select                   ← LLM reads excerpts of ALL pages
       └─ if none: find_relevant_pages
  └─ build context → chat_answer LLM call
  └─ interview_verify LLM call               ← always
  └─ interview_grade LLM call                ← always (needs local Ollama)
```

```
AFTER

/chat question
  └─ memory_store.search(sync=False)         ← reads the SQLite index only
       └─ if no hits: find_relevant_pages    ← one keyword fallback, kept
  └─ build context → chat_answer LLM call

/interview question
  └─ memory_store.search(sync=False)         ← same path as chat
       └─ if no hits: find_relevant_pages
  └─ build context → chat_answer LLM call
  └─ trace + telemetry written               ← new: enters the feedback loop
  └─ interview_verify LLM call               ← only if the user asked to be graded
  └─ interview_grade LLM call                ← only if the user submitted an answer

index sync (memory_store.sync_index) now runs on:
  • backend startup
  • after POST /approve            (a page may have been written)
  • after POST /edit-page          (a page body changed)
  • POST /memory/reindex           (manual escape hatch, already exists)
```

The `_semantic_page_select` function is deleted. `find_relevant_pages` is kept as
the single fallback because it needs no index and no network — it is the safety
net when the SQLite index is cold or empty.

## Data flow — observability

```
any LLM task
  └─ llm_client.complete(task=...)
       ├─ telemetry.log_llm_call(...)      ← unchanged: appends to llm_call_logs.jsonl
       └─ langfuse span                    ← new: task name, model, tokens, cost,
                                             latency, contract_ok, fallback_used,
                                             attempts. Errors here are swallowed.
```

One span per `complete()` call. The span name is the task key. Cost and token
numbers come from the same `telemetry.estimate_*` helpers already used for the
JSONL logs, so the two sinks agree.

## Failure behavior

| Failure | Result |
|---|---|
| Langfuse unreachable or errors | Span dropped, warning logged, user request completes normally. |
| SQLite index cold / empty | `search()` returns nothing, `/chat` and `/interview` fall back to `find_relevant_pages`. |
| A page saved outside the app (git pull, Obsidian) | Not searchable until the next sync trigger or a `POST /memory/reindex`. This is the accepted cost of dropping per-query sync — see ADR-2. |
| `ENABLE_OPS=false` and something calls an Ops route | 404, same as any unregistered route. |
| Interview verify/grade requested but the routed model is down | That call returns an "unavailable" result as today, but it is no longer on the default path, so the common case is unaffected. |

## Resolved

- **`humanities` folder** — CONFIRMED: remove it from `_concept_dirs()` and from
  the frontend folder list. Re-add both in one line if humanities notes start
  existing.
- **`RAG_TOP_K` / `RAG_CONTEXT_BUDGET`** — CONFIRMED: change the defaults
  (`10 → 5`, `6000 → 4000`). Both stay env-overridable; measure `pages_read` and
  answer quality on real questions, revert via env if recall drops.

## Open questions

1. **Ollama routing for cheap tasks** — the plan routes `tag_classify` to local
   Ollama *when configured*. If Ollama is absent the task must fall back to the
   default provider, not fail. Confirm that `llm_client` already degrades this
   way, or add the guard during implementation.
2. **Langfuse cost fields** — Langfuse can compute cost itself from a model price
   table, or take the number we send. Plan: send our own number so it matches the
   JSONL and honors the `LLM_PRICE_*` overrides.
