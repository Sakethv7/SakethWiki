# Agent Review Resolution — API Contract

## No new HTTP endpoints

This change adds zero new FastAPI routes. `resolve_reviews.py` calls existing backend functions in-process (`system_loop.approve_action_candidate`, the internal function backing `POST /queue/batch-decision`). The only new interface is the script's CLI.

## `backend/resolve_reviews.py`

### Invocation

```bash
python3 backend/resolve_reviews.py [--domain system|hitl|all] [--apply] [--limit N]
```

| Flag | Default | Meaning |
|---|---|---|
| `--domain` | `all` | Restrict to one domain (`system` = Operations tab candidates, `hitl` = wiki queue) instead of both. |
| `--apply` | absent (dry-run) | Without this flag, the script only prints what it *would* do — no mutation. Matches the `DRY_RUN`-first convention already used by `normalize_vault_tags.py`. |
| `--limit` | none | Cap the number of items applied in one run, for a first cautious `--apply` pass. |

Exit code `0` if the run completed (including "0 eligible items found") and produced no unexpected errors; non-zero if the script itself failed to run (e.g. `hitl_queue.json` unreadable, backend import failure). Per-item failures (e.g. an eligible candidate's `_apply_candidate` raising) do not change the process exit code — they're reported in the printed summary, same as a human would see a failed apply in the UI.

### Output (human-readable, stdout)

```text
== System action candidates ==
  eligible (low risk):     act-3f9a1c2b — "activate_preference: prefers concise summaries"
  eligible (medium, eval passed): act-88d0e701 — "increase_chat_context_budget to 6000"
  left for human (high risk): act-102bfeee — "queue_page_review: transformers"
  3 eligible, 0 applied (dry run)

== HITL wiki queue ==
  eligible-approve (source_verdict=ingest): a1b2c3 — "Flash Attention 2 mechanics"
  eligible-approve (source_verdict=ingest): d4e5f6 — "KV cache quantization notes"
  left for human (source_verdict=source_only): 998877 — "Twitter thread on MoE routing"
  2 eligible, 0 applied (dry run)

Run again with --apply to act on the above.
```

With `--apply`, the "eligible" lines become "applied" or "failed: <reason>" lines, and the summary counts reflect actual outcomes.

### Output (machine-readable)

Not needed for the first version — a human or an invoking agent reads stdout directly. If an agent needs to parse results programmatically later (e.g. to decide what to report back to you), add a `--json` flag as a follow-up rather than building it speculatively now.

## Reused existing contracts

These are documented in full in their own docs and are unchanged by this work:

- `system_loop.list_action_candidates()`, `system_loop.approve_action_candidate()` — internal Python functions, see `backend/system_loop.py`.
- `POST /queue/batch-decision` — see [queue-bulk-review/api.md](../queue-bulk-review/api.md) for the full request/response contract. This script calls this endpoint directly over HTTP (`http://127.0.0.1:8001`, requires the backend to be running), the same way the Queue UI's bulk-approve button does — same validation, same decision function, same vault writes.

## Code review procedure interface

No API — this is an agent following the runbook in [logic_flow.md](logic_flow.md) Path C using the existing `/code-review` Skill and its documented `--fix` flag. No new contract to specify.
