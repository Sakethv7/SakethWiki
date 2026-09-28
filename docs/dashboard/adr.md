# Dashboard Refocus — Decisions

## ADR-1: Measure recall next to capture

**Context.** Every dashboard metric counts approvals. The vault has 246
approved entries and 34 page reads ever. The metric that says whether the vault
is worth keeping is the recall side, and it isn't shown.

**Options.**

| Option | Summary |
|---|---|
| A. Keep capture-only metrics | No work. Keeps hiding the gap. |
| B. Add recall tiles from existing logs | Count read events and chat/interview questions already on disk. |
| C. Add new instrumentation first | Log every page open from every entry point, then build tiles. |

**Choice: B.** The data is already on disk. `reads.jsonl` records page reads and
`context_budget_logs.jsonl` records one event per chat or interview question.
No new logging is needed to ship something useful.

**Given up.** Precision. Reads are logged only when a page is closed in Browse,
so the "pages read" tile probably undercounts. Option C would fix that but
turns a small dashboard change into a cross-cutting logging change. The
undercount is flagged in the architecture open questions.

## ADR-2: Replace `/review-due` with `/review-queue`, and delete `/review-due`

**Context.** Two endpoints answer "which pages need attention?".
`/review-due` uses two signals (last read, maturity) and is broken: it reads
the keys `page`/`timestamp`, but `/log-read` writes `concept`/`ts`, so no read
ever counts. It flagged 200 of 202 pages. `/review-queue` uses six signals,
reads both key spellings, ranks by priority, and suggests an action per page.
It flags 61 pages as high priority.

**Options.**

| Option | Summary |
|---|---|
| A. Fix the key mismatch in `/review-due` | Smallest change. Leaves two review systems. |
| B. Use `/review-queue` and delete `/review-due` | One review system. |
| C. Use `/review-queue` and keep `/review-due` | No deletion risk. Leaves broken dead code behind. |

**Choice: B.** The lean refactor already removed `/active-review` for being a
duplicate of `/review-queue`. This is the same case. Deleting also removes a
bug instead of patching it.

**Given up.** `/review-due` put "never read" pages first. `/review-queue` ranks
by structural problems (conflicts, backlinks, maturity) before staleness, so a
page you have never opened but that is structurally fine now ranks lower. Also,
any external script calling `/review-due` would break. I found no caller other
than `ReviewDueSection`.

## ADR-3: Add fields to `/dashboard-stats`, don't remove any

**Context.** The new UI stops using `top_tags`, `top_sources`,
`learning_velocity`, and `unique_concepts`.

**Options.**

| Option | Summary |
|---|---|
| A. Add `recall`, keep all existing fields | Purely additive. |
| B. Add `recall`, remove unused fields | Smaller response, tighter contract. |

**Choice: A.** An additive change can't break anything, including existing
tests of `_dashboard_stats_from_traces`. The unused fields cost microseconds.

**Given up.** The response carries fields nothing reads. They will drift
because no UI exercises them. Removing them is a separate one-line decision
later (architecture open question 3).

## ADR-4: Compute recall inside `/dashboard-stats`, not in a new endpoint

**Context.** Recall numbers could come from a new `GET /recall-stats` or be
added to the existing stats response.

**Choice:** add them to `/dashboard-stats` through a separate pure function,
`_recall_stats(reads, context_events, now, period_days)`. The Dashboard already
makes one stats call. A separate pure function keeps the logic testable without
touching the trace math.

**Given up.** `/dashboard-stats` now reads three files instead of one. Each is
a small append-only JSONL file (hundreds to about a thousand lines), so the
cost is milliseconds. If they grow large, this call becomes the first place
that needs caching.

## ADR-5: Show waiting items as links, only when non-zero

**Context.** Queue items and open threads need action, but they already have
full UIs in Capture and Browse.

**Choice:** one row of small link chips at the top, hidden when both counts are
zero. The chips navigate. They don't duplicate the lists.

**Given up.** Two extra requests (`/queue`, `/pages?folder=open-threads`) on
each Dashboard load. `/queue` is also polled by Capture, so the data may briefly
differ between tabs.
