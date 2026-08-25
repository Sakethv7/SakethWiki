# Agent Review Resolution — ADRs

## ADR-1: Standalone script, not an extension of the existing auto-apply loop

**Context.** `system_loop.py` already has an `auto_apply` path (`run_system_loop(auto_apply=True)` calls `_auto_apply_preference_candidates`), but that path is narrowly scoped to preference-memory corrections — a different mechanism from the risk-tiered `action_candidates.json` queue this change targets. Two ways to add "resolve the easy ones" behavior:

1. A standalone script (`backend/resolve_reviews.py`) that something — you, a coding agent, a future cron — invokes on demand.
2. Extend the existing auto-apply loop so eligible system-action and HITL items resolve themselves as part of whatever already triggers that loop.

**Options considered.**

- *Script:* inspectable in isolation, dry-runnable, no change to any currently-running automated behavior, matches the existing `normalize_vault_tags.py` convention in this repo.
- *Extend the loop:* zero new invocation surface — everything just resolves as part of what's already running.

**Choice.** Standalone script. You asked for something "an agent or a coder" runs — that phrasing describes an invoked tool, not an ambient background behavior. A script is also strictly easier to get right the first time: you can `--dry-run` it, read the output, and decide whether the eligibility rules are actually finding the things you'd call "easy" before anything is ever applied unattended.

**Consequences.** Nothing resolves unless something runs the script. If you want this to happen automatically on a schedule later, that is a follow-up change (e.g. adding it as a step in `run_system_loop`, or a `loop`/cron entry) — deliberately not bundled into this one, so the two decisions (what counts as easy, and when it runs) can be evaluated separately.

## ADR-2: Rule-based eligibility, not an LLM judging each item

**Context.** Two ways to decide whether a given pending item is "easy" enough to resolve without a human:

1. Deterministic rules per domain, built from fields the app already computes (`risk`, `eval_status`, `source_verdict`, `pending_extraction`, `extraction_error`).
2. An LLM reads each item and decides case by case.

**Choice.** Rule-based, per your explicit preference. A rule like "risk is low, or risk is medium and eval already passed" is auditable in one sentence, costs nothing to run, and produces the same answer on every run — you can look at the code and know exactly what it will and won't touch. An LLM judge would catch more genuinely-easy edge cases the rules miss, at the cost of non-determinism and a per-run token cost for a task that is mostly "read a status field."

**Consequences.** The rules are conservative by construction — anything not explicitly matched is left for a human, including items that a person would probably also call easy. That's the intended failure direction: false negatives (leaving an easy item for you) are cheap; false positives (auto-resolving something that needed judgment) are not. If the rules turn out too conservative in practice, loosen them deliberately later rather than reaching for an LLM judge as the fix.

## ADR-3: Reuse existing signals, don't compute new confidence scores

**Context.** To decide "easy," the script needs some notion of confidence or safety per item. The app already computes several: `risk` and `eval_status` on system-action candidates, `source_verdict` on HITL items (the extraction model's own `ingest | source_only | reject` judgment), and `pending_extraction`/`extraction_error` as readiness gates.

**Choice.** The script only reads these existing fields — it does not add a new scoring pass, a new confidence field, or a second classification of any item. `source_verdict == "ingest"` already *is* the extraction step's confidence judgment; recomputing that inside the resolution script would be redundant and could disagree with the value shown to a human in the Queue UI.

**Consequences.** The quality of "easy" classification is bounded by the quality of these upstream signals. If `source_verdict` is systematically too generous with `"ingest"`, so is this script's auto-approval — that's a property of the extraction prompt, not something this script can or should compensate for. If that turns out to be a problem, the fix belongs in the extraction step, not here.

## ADR-4: Code review is a documented procedure, not a script

**Context.** Unlike the other two domains, there's no persisted findings store to select from, and `/code-review` is a Claude Code skill invoked through the Skill tool inside an agent session, not a subprocess-callable CLI.

**Choice.** Document this as a runbook step in [logic_flow.md](logic_flow.md) — "when asked to resolve review items, run `/code-review`, then `--fix` only categories on the allowlist" — rather than building automation infrastructure a plain Python script can't actually drive.

**Consequences.** This domain isn't reproducible the same way the other two are: each run depends on whatever finding categories that invocation of `/code-review` produces, and on the invoking agent correctly restricting `--fix` to the allowlist. There's no dry-run artifact to inspect ahead of time the way there is for the script. If a persisted findings store gets built later (e.g. code review output written to a tracked file), this domain could move into the script.

## Decisions (confirmed 2026-08-23)

1. **HITL auto-reject: no.** The script only auto-*approves* `source_verdict == "ingest"` items. `source_verdict == "reject"` items are left for a human — reject is destructive (the queue item is gone), while approve just writes a vault file that can be hand-edited or deleted afterward. This asymmetry is intentional: the two actions don't carry the same risk, so they don't get the same automation bar.
2. **Code-review category allowlist: confirmed as proposed.** Auto-fixable: simplification, reuse, efficiency, dead code/unused imports, comment/doc fixes. Always left for a human: correctness, security, anything the reviewer marks uncertain/PLAUSIBLE rather than CONFIRMED.
