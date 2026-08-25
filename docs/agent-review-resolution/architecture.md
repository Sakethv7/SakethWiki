# Agent Review Resolution — Architecture

## Problem

SakethWiki has three places where something sits waiting for a human decision:

1. **System action candidates** (Operations tab) — `system_loop.py` already tags each candidate `low`/`medium`/`high` risk, but nothing currently acts on that tag. A `low`-risk candidate (no eval, no approval required by its own design) and a `medium`-risk candidate that already passed its eval (`status: "eval_ready"`) both sit in the same list as `high`-risk candidates until a human opens the Operations tab and clicks through them one at a time.
2. **HITL wiki queue** (`hitl_queue.json`) — extraction candidates from Lekhni clips/notes wait for `Save to wiki` or `Skip`. The extraction step already writes its own confidence judgment onto the item (`source_verdict: ingest | source_only | reject`), but that judgment is only ever shown to a human, never acted on.
3. **Code review findings** — running `/code-review` on this repo produces findings (correctness bugs, simplification opportunities, dead code, reuse). Nothing persists them; they exist only in the reviewing agent's output for that run.

The ask: let an agent (or a person running a script) clear the obviously-easy items in each of these three places on its own, so a human only has to look at the ones that actually need judgment.

## Scope

This change adds one new script and one runbook procedure. It does not add new UI, does not change the existing Operations tab or Queue UI, does not change how candidates get their risk tag or how extraction assigns `source_verdict`, and does not add a scheduler. If auto-triggering this on a timer turns out to be wanted later, that is a separate change layered on top of this one — see [adr.md](adr.md) ADR-1.

## Components

### `backend/resolve_reviews.py` (new)

A standalone CLI script, following the same shape as the existing `backend/normalize_vault_tags.py`: a `DRY_RUN`-first script with `if __name__ == "__main__"`, importable backend modules, printed summary output. It handles two of the three domains:

- **System actions** — imports `system_loop` directly (in-process, no HTTP hop — this module has no FastAPI app and no import-time side effects) and calls the existing `list_action_candidates()` / `approve_action_candidate()` functions. It does not reimplement risk tiering or eval logic; it only decides *which already-computed candidates* are safe to wave through.
- **HITL queue** — calls the existing `POST /queue/batch-decision` endpoint over HTTP against the already-running backend (`httpx`, per this project's Python conventions), rather than importing `main.py`. `main.py` is a live FastAPI app with substantial top-level state; importing it standalone for its internal decision function would risk side effects a plain read-only script has no business triggering. Hitting the same endpoint the Queue UI's bulk-approve button hits gives identical guarantees (same validation, same decision function, same vault writes) without that risk. It does not reimplement extraction-readiness checks (`pending_extraction`, `extraction_error`) or vault-writing; it only decides *which already-extracted items* are safe to auto-decide, then lets the existing endpoint do the rest.

Both domains reuse 100% of the existing decision/apply/write code paths. This script is a *selector* that feeds already-existing, already-tested "approve this" functions — it does not introduce a second way to approve a candidate or write to the vault. See [logic_flow.md](logic_flow.md) for the exact per-domain selection rules.

### Code review runbook (documented procedure, not code)

`/code-review` is a Claude Code skill invoked through the Skill tool inside an agent session — it is not a subprocess-able CLI binary, and there is no persisted findings store to point a script at (see [adr.md](adr.md) ADR-4). This domain is handled as a documented procedure an agent follows when asked to "resolve review items": run `/code-review` scoped to this repo, then apply `--fix` only for a fixed allowlist of safe finding categories, leaving correctness findings untouched. The allowlist lives in [logic_flow.md](logic_flow.md) and needs your sign-off before it's used unattended.

## What this does NOT touch

```text
                         ┌─────────────────────────┐
                         │   resolve_reviews.py     │
                         │   (new, this change)     │
                         └───────────┬───────────────┘
                                     │ selects eligible IDs, then calls
                    ┌────────────────┼────────────────────┐
                    ▼                                      ▼
      system_loop.approve_action_candidate()   queue batch-decision path
      (existing, unchanged)                    (existing, unchanged)
                    │                                      │
                    ▼                                      ▼
      action_candidates.json (existing)         hitl_queue.json + vault (existing)
```

The risk tagging in `system_loop.py`, the eval harness, the extraction pipeline's `source_verdict` scoring, `wiki_writer.py`'s vault writes, and the Operations/Queue frontend all stay exactly as they are. This is additive: a new caller of existing decision functions, gated by a new, narrow eligibility rule.

## Complexity tier

Single script (CLI tool) plus one documentation-only procedure. Not a service, not a scheduled job, not a new API surface for two of the three domains — the HITL domain literally reuses an existing endpoint's internal logic. This is the smallest tier that does the job; see ADR-1 in [adr.md](adr.md) for why the "always-on auto-apply loop" alternative was not chosen.

## Confirmed decisions

The two open questions from the original draft (HITL auto-reject, code-review category allowlist) are resolved — see [adr.md](adr.md) "Decisions (confirmed 2026-08-23)". No open questions remain; implementation can proceed.
