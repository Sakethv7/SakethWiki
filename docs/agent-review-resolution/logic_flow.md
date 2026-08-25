# Agent Review Resolution — Logic Flow

## Overview

```text
resolve_reviews.py [--apply] [--domain system|hitl|all]
        │
        ├── domain: system ────────────────────────────────────────┐
        │     list_action_candidates(status=None)                  │
        │     for each candidate:                                  │
        │       skip if status in {applied, rejected, apply_failed}│
        │       skip if requires_approval (risk == "high")         │
        │       eligible if risk == "low"                          │
        │       eligible if risk == "medium" and status=="eval_ready"│
        │       otherwise: leave for human                          │
        │     if --apply: approve_action_candidate(id) each        │
        │     else: print what would be approved                   │
        │                                                            │
        ├── domain: hitl ──────────────────────────────────────────┐
        │     queue_manager.get_all()                               │
        │     for each item:                                        │
        │       skip if pending_extraction or extraction_error      │
        │       eligible-approve if source_verdict == "ingest"       │
        │       eligible-reject  if source_verdict == "reject"       │  (pending ADR open question)
        │       otherwise (source_only, missing verdict): leave      │
        │     if --apply: batch-decide eligible IDs, approve/reject  │
        │     else: print what would be approved/rejected            │
        │                                                            │
        └── print summary: N eligible / N applied / N left for you  ┘

Code review: not part of this script. Separate procedure below.
```

## Path A: System action candidates

1. Load all candidates via the existing `system_loop.list_action_candidates()` — no new read path.
2. Filter out anything already in a terminal state (`applied`, `rejected`, `apply_failed`) — nothing to do there.
3. Filter out anything with `requires_approval` true (i.e. `risk == "high"`) — always left for a human, no exceptions, regardless of any other field.
4. Of what remains, an item is eligible when:
   - `risk == "low"` (by construction these already have `requires_approval == False` and `requires_eval == False` — the app itself decided no gate was needed, but currently nothing acts on that), or
   - `risk == "medium"` and `status == "eval_ready"` (a human or a previous script run already triggered `/system-actions/{id}/eval` and it passed).
5. For each eligible candidate, call `system_loop.approve_action_candidate(candidate_id)` — the exact function the Operations tab's Approve button calls. This function itself re-checks `requires_eval`/`eval_status` and runs the eval if it's somehow missing, so there's no way for this script to apply something that hasn't actually passed its gate.
6. A medium-risk candidate with `status` still `candidate` (no eval run yet) is **not** eligible in this version — the script does not trigger a fresh eval on your behalf, it only acts on evals that already passed. Triggering evals automatically is a reasonable follow-up but is a different decision (spending eval-harness runs autonomously) from resolving already-cleared items.

## Path B: HITL wiki queue

1. Load all queue items via `queue_manager.get_all()`.
2. Filter out anything not ready: `pending_extraction` truthy or `extraction_error` truthy — matches the exact same gate `POST /queue/batch-decision` already enforces, so nothing this script calls "eligible" could fail that endpoint's own readiness check.
3. Of what remains:
   - `source_verdict == "ingest"` → eligible to auto-approve.
   - `source_verdict == "reject"` → **not** eligible; left for a human (confirmed in [adr.md](adr.md) — reject is destructive, approve is not, so they don't get the same automation bar).
   - `source_verdict == "source_only"`, missing, or anything else → leave for human. `source_only` means the extraction step itself flagged this as "useful as provenance/context, not durable enough to ingest as-is" — a judgment call.
4. For eligible IDs, call `POST /queue/batch-decision` on the running backend (`http://127.0.0.1:8001`) with `approved: true` — the same call the Queue UI's bulk-approve button makes. The script never calls it with `approved: false`.
5. Batch cap: reuse the existing 1–100 IDs-per-call validation already implemented for `/queue/batch-decision`; if more than 100 items are eligible in one run, process in batches of 100.

## Path C: Code review (procedure, not script)

This path is followed by an agent (e.g. a Claude Code session) when asked to resolve review items — it is not something `resolve_reviews.py` executes.

1. Run `/code-review` (default level, or a level the requester specifies) scoped to the SakethWiki repo's current diff or a named target.
2. From the findings returned, only act on categories in the allowlist (confirmed in [adr.md](adr.md)): simplification, reuse, efficiency, dead-code/unused-import, comment/doc fixes.
3. For allowlisted findings, apply `/code-review --fix` (or manually apply the equivalent edit) as normal — same as any other `--fix` invocation, nothing custom.
4. Correctness, security, and any finding marked uncertain rather than confirmed are left unfixed and reported to the requester, the same way `/code-review` normally surfaces them — this procedure narrows what gets auto-applied, it does not change how findings are reported.
5. If `--fix` is not being used in a given invocation, this step is a no-op — the procedure only applies when auto-fixing is actually happening.

## Failure behavior

- If `system_loop.approve_action_candidate()` raises (e.g. `_apply_candidate` fails), the resulting `apply_failed` status is left as-is — this script does not retry or suppress that failure, it's the same failure path a human clicking Approve would hit.
- If a HITL batch-decision call returns a per-item failure (e.g. `not_found` because another tab already acted on it), that item is reported as failed in the script's summary and not retried.
- The script never partially mutates its own selection logic mid-run: eligibility is computed once at the start from a single snapshot of each domain's state, then applied. An item that changes state during the run (e.g. approved by a human in the UI while the script is running) is handled by the existing per-item failure paths above, not by re-checking eligibility.

## Verification plan after approval

- Run `--dry-run` (the default) against the current live queues and inspect the printed eligible list before ever passing `--apply`.
- Confirm the printed "eligible" set for system actions matches manual inspection of `GET /system-actions` for a few sample candidates of each risk tier.
- Confirm the printed "eligible" set for HITL items matches manual inspection of a few sample `source_verdict` values in `hitl_queue.json`.
- Run `--apply` once against a small number of items (or a disposable test queue, matching the verification approach already used for queue-bulk-review) and confirm the Operations tab / Queue UI reflect the same outcome a manual click would have produced.
- Confirm nothing this script does is missing from telemetry (`action_candidates.json` history, existing trace logs) — i.e., an auto-resolved item is indistinguishable in the audit trail from a human-resolved one, except that a human didn't click it.
