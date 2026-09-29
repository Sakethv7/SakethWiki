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

---

# Round 2 decisions

## ADR-6: Trends as "current minus previous window", neutral colour

**Context.** The tiles give no sense of direction, so no decision can be
checked afterwards.

**Options.**

| Option | Summary |
|---|---|
| A. Delta vs previous 30 days | One extra number per tile, same data. |
| B. Sparkline per tile | Richer, shows shape over time. |
| C. Percent change | "↑40%" |

**Choice: A.** It's the smallest change that answers "is it going up or down",
and it reuses the existing pure functions by calling them with an earlier
`now`.

**Given up.** Shape. A single delta hides a spike in the middle of the window
(the heatmap partly covers that). Option C was rejected because small counts
make percentages jumpy (2 → 4 reads is "+100%").

## ADR-7: Detect duplicates without an LLM; keep the LLM for the merge only

**Context.** You asked whether consolidation help needs an LLM such as Qwen.

**Options.**

| Option | Summary |
|---|---|
| A. Deterministic scorer, human decides | Existing `consolidation.py`, shown on the dashboard. |
| B. Deterministic shortlist, LLM judges each pair | Cheap model says duplicate / related / distinct. |
| C. Embeddings similarity | Needs `EMBED_ENABLED` and an OpenAI key, which are currently off. |

**Choice: A.** On the real vault the scorer already ranks the true duplicates
first. With eight pairs on screen, you are a faster and better judge than an
extra model call.

**Given up.** Recall on duplicates that use different words and different
names. Two pages about the same idea with no shared vocabulary won't be found.
Option C would catch those, and option B would cut the false pairs. Both stay
available as later steps if the list turns out too noisy.

## ADR-8: Show candidates below the old cut-off, and merge with force

**Context.** The finder hides pairs below 0.62. Every real duplicate on the
vault scores 0.45–0.57.

**Choice.** Request `include_weak=true` and show the top 8 by score, labelled
"possible duplicates, you decide". Merge sends `force: true`.

**Given up.** Safety margin. The old cut-off existed so that nothing
low-confidence could be merged automatically. This change keeps a human click
in front of every merge, but it does make a wrong merge one click away, and
merges had no preview. That was resolved in the same round: merges are now previewed, hash-checked and backed up (architecture open question 5). I did not change the cut-off inside
`consolidation.py`, so any automatic caller keeps the strict behavior.
