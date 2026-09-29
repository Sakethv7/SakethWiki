# Lean Refactor — API & Config Contract

Only what this change adds, alters, or removes. Everything not listed keeps its
current contract.

---

## Endpoints

### `POST /interview` — changed

**Request**

| Field | Type | Default | Notes |
|---|---|---|---|
| `question` | string | — | Required, non-empty. |
| `user_answer` | string \| null | `null` | If present, grading runs. |
| `want_verification` | bool | `false` | **New.** If true, the verifier pass runs. |

**Response**

| Field | Type | Notes |
|---|---|---|
| `wiki_answer` | string | Always present. |
| `pages_read` | string[] | Slugs used for context. |
| `verification` | object | **Present only when `want_verification` was true.** Shape unchanged: `{score, verdict, gaps[]}`. `score` may be `null` if the verifier model was unavailable. |
| `user_grading` | object | Present only when `user_answer` was supplied. Shape unchanged. |

**Behavioral change:** retrieval now uses `memory_store.search`, so `pages_read`
for the same question will match `/chat`. A trace row (`event_type:
"interview"`) and a `interview_context` telemetry event are written on every
call. Neither affects the response.

---

### `POST /chat` — unchanged contract, changed internals

Request and response shapes are identical. Retrieval no longer calls an LLM to
pick pages and no longer scans the vault per request. `pages_read` may differ
from before for the same query because ranking now comes from the index, not
from an LLM.

---

### `GET /health` (or new `GET /config`) — changed

Add one field so the frontend can hide disabled surfaces:

```json
{ "status": "ok", "ops_enabled": false }
```

If a separate `/config` is cleaner, it returns at least `{ ops_enabled: bool }`.

---

### `POST /memory/reindex` — unchanged, now load-bearing

Already exists. Becomes the documented way to refresh the index after editing
vault files outside the app (git pull, Obsidian). Response unchanged:
`{indexed, unchanged, removed, pages_seen}`.

---

### `GET /memory/status` — unchanged, now surfaced

Already exists. Should get a small card in a still-visible tab (Dashboard) so the
index state (`pages_indexed`, `chunks_indexed`, `embeddings_enabled`) is
observable without curl. No contract change.

---

### `GET` / `POST /active-review` — removed

Was byte-identical to `/review-queue`. Callers use `/review-queue?days=30`.
`active_review.build_queue(...)` stays (it backs `/review-queue`).

---

### Operations routes — conditional on `ENABLE_OPS`

Registered only when `ENABLE_OPS` is truthy; otherwise they do not exist (404):

```
/system-loop/run          /system-actions
/system-loop/actions      /system-actions/{id}/approve
/trace-critic/run         /system-actions/{id}/reject
/evals/run                /system-actions/{id}/eval
/inference-report         /operations-overview
/reports/read
```

**Not** gated (knowledge loop, always on): `/analyze-traces`,
`/system-insights`, `/dashboard-stats`.

---

## Environment variables

### New

| Var | Default | Meaning |
|---|---|---|
| `ENABLE_OPS` | `false` | Register Operations routes, load `system_loop` / `eval_harness`, show the Operations tab. |
| `LANGFUSE_PUBLIC_KEY` | _(unset)_ | Langfuse project public key. If unset, all span code is a no-op. |
| `LANGFUSE_SECRET_KEY` | _(unset)_ | Langfuse project secret key. |
| `LANGFUSE_HOST` | `https://cloud.langfuse.com` | Point here at a self-hosted instance later; nothing else changes. |

### Changed defaults

| Var | Old | New | Notes |
|---|---|---|---|
| `RAG_TOP_K` | `10` | `5` | Pages considered for context. Still env-overridable. |
| `RAG_CONTEXT_BUDGET` | `6000` | `4000` | Characters of vault context injected. Still env-overridable. |

### Newly meaningful (already supported by `llm_client` task routing)

| Var | Suggested value | Notes |
|---|---|---|
| `LLM_PROVIDER_TAG_CLASSIFY` | `ollama` | Only if `OLLAMA_BASE_URL` is set; must fall back to the default provider if the local model is absent. |
| `LLM_PROVIDER_INTERVIEW_VERIFY` | default provider | Move off the assumed-local `ollama` default so verification is reliable when requested (ADR-3 follow-up). |
| `LLM_PROVIDER_INTERVIEW_GRADE` | default provider | Same. |

### Removed from `.env.example`

- `LLM_PROVIDER_CHAT_SELECT_PAGES` / `LLM_MODEL_CHAT_SELECT_PAGES` — the
  `chat_select_pages` task is deleted with `_semantic_page_select`.

---

## Internal function contracts

### `memory_store.search(query, limit=5, *, sync=False)` — default flips

| Aspect | Before | After |
|---|---|---|
| `sync` default | `True` | `False` |
| Side effects when `sync=False` | — | none; reads the index only |
| Callers passing `sync` explicitly | none | `/memory/reindex` path still forces a sync via `sync_index()` directly |

Invariant: returns `list[dict]` with `page_name, folder, title,
current_understanding, score, snippets[], headings[]`. Unchanged.

### `llm_client.complete(...)` — additive only

- Same signature, same return type (`str`).
- New behavior: opens/closes a Langfuse span around the call.
- **Invariant:** never raises because of Langfuse. A telemetry failure is logged
  and swallowed. The function still raises on an unrecoverable LLM error exactly
  as today.
- `telemetry.log_llm_call(...)` still fires on every call.

### `_append_trace(trace: dict)` — new caller

`/interview` now calls it with `event_type: "interview"`. Contract unchanged:
append one JSON line to `traces.jsonl`, best-effort, never raises into the
handler.

---

## Migration / rollback

- **Config sync:** this change edits `.env.example`, `README.md`,
  `ARCHITECTURE.md`, and `CONCEPTS.md` in the same commit per the repo's stated
  policy.
- **Rollback:** set `ENABLE_OPS=true` to restore Operations; unset the
  `LANGFUSE_*` vars to disable spans; raise `RAG_TOP_K` / `RAG_CONTEXT_BUDGET`
  via env. The only non-env rollback is `_semantic_page_select` deletion, which
  is a straight `git revert` of that hunk.
- **Data:** no schema changes to `memory.db`. No vault file format changes. No
  migration script.
