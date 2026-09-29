# Dashboard Refocus — Interface Contracts

## `GET /dashboard-stats` (changed: additive)

Existing fields are unchanged. One new top-level key is added.

```json
{
  "period_days": 30,
  "heatmap_days": 112,
  "total_events": 151,
  "total_approved": 73,
  "total_rejected": 78,
  "approval_rate": 0.4834,
  "unique_concepts": 49,
  "activity_by_date": {"2026-09-26": 4},
  "learning_velocity": {"entries_per_week": 17.03, "concepts_per_week": 11.43},
  "top_tags": [{"tag": "Systems", "count": 43}],
  "top_sources": [{"source": "link", "count": 30}],
  "new_concepts_this_week": 13,
  "concepts_touched_this_week": 20,

  "recall": {
    "pages_read": 9,
    "unique_pages_read": 7,
    "questions_asked": 24,
    "chat_questions": 24,
    "interview_questions": 0
  }
}
```

*(Numbers are illustrative.)*

| Field | Type | Meaning |
|---|---|---|
| `recall.pages_read` | int ≥ 0 | Read events in the last `period_days`. |
| `recall.unique_pages_read` | int ≥ 0 | Distinct pages among those events. Always ≤ `pages_read`. |
| `recall.questions_asked` | int ≥ 0 | `chat_questions + interview_questions`. |
| `recall.chat_questions` | int ≥ 0 | `chat_context` telemetry events in the period. |
| `recall.interview_questions` | int ≥ 0 | `interview_context` telemetry events in the period. |

**Invariants**

- `recall` is always present. Missing source files produce zeros, not nulls.
- The window is the same `period_days` as the capture metrics.
- The endpoint stays read-only.

**Errors:** unchanged. The endpoint does not raise for missing or malformed log
files.

## `_recall_stats` (new internal function)

```python
def _recall_stats(
    reads: list[dict],
    context_events: list[dict],
    now: Optional[datetime] = None,
    period_days: int = 30,
) -> dict:
    """Return the `recall` block described above."""
```

Pure: no file or network access. It sits next to `_dashboard_stats_from_traces`
and is tested the same way.

## `GET /review-due` (removed)

Deleted. Callers get `404 Not Found`. The only known caller,
`ReviewDueSection` in `App.jsx`, is removed in the same change.

## Endpoints reused without change

| Call made by the Dashboard | Fields used |
|---|---|
| `GET /review-queue?min_priority=high&limit=5` | `pages[].name`, `pages[].suggested_action`, `pages[].reasons`, `total` |
| `GET /queue` | `items.length` |
| `GET /pages?folder=open-threads` | `pages.length` |

Note on `/review-queue` `total`: `total` is the length of the returned list,
which `limit` caps. To show "61 high priority" while listing 5 rows, the UI
must either request a larger limit and slice to 5, or the endpoint needs a
separate uncapped count. **This plan requests `limit=100` and shows the first
5**, so no backend change is needed. `build_queue` already scores every page
before applying the limit, so the larger limit costs nothing extra to compute.

## Frontend component contract

```
DashboardTab({ onOpenPage, onGoCapture })
```

| Prop | Type | Use |
|---|---|---|
| `onOpenPage` | `(name?: string, folder?: string) => void` | `App`'s existing `openSavedPage`. Opens Browse at a page, or at a folder's list when `name` is omitted. |
| `onGoCapture` | `() => void` | Switches to the Capture tab. `App` passes `() => setTab("ingest")`. |

`BrowseTab` now accepts a folder-only `openTarget` (`{ folder }`). It calls its
existing `switchFolder`, which clears any open page and resets the filters.

Removed components: `ReviewDueSection` and `RecentlyRead`.

## `GET /review-queue` (changed: runs in a worker thread)

The handler changed from `async def` to plain `def`. `build_queue` reads every
page (about 1.3 s on 202 pages) with blocking file I/O. As `async def` it held
the event loop, so the 10 ms `/dashboard-stats` call waited behind it. As plain
`def`, FastAPI runs it in its threadpool. The request and response are
unchanged. Measured: the tiles now render in about 70 ms instead of about 1350 ms.

---

# Round 2 contracts

## `GET /dashboard-stats` (additive)

One new top-level key, `previous`, holding the same metrics for the previous
window of `period_days`:

```json
"previous": {
  "total_approved": 83,
  "approval_rate": 0.52,
  "pages_read": 9,
  "questions_asked": 14
}
```

| Field | Type | Meaning |
|---|---|---|
| `previous.total_approved` | int ≥ 0 | Approved decisions in days 31–60 before now. |
| `previous.approval_rate` | float or null | Same definition as `approval_rate`. Null when there were no decisions. |
| `previous.pages_read` | int ≥ 0 | Read events in that window. |
| `previous.questions_asked` | int ≥ 0 | Chat + interview questions in that window. |

**Invariant:** the window is `(now − 2·period_days, now − period_days]`, the
same length as the current one, with no overlap.

Implementation note: `_dashboard_stats_from_traces` counts
`ts > now − period_days`, so it needs an upper bound as well to exclude the
current window. `_recall_stats` needs the same bound. Both get an optional
`until: Optional[datetime]` parameter (default: no upper bound). Existing calls
are unchanged.

## `GET /consolidation-candidates` (unchanged contract, faster)

Called as `?limit=8&include_weak=true`. Response shape is unchanged:
`candidates[]` with `source`, `target`, `score`, `confidence`, `reasons`.
Typical latency on 215 pages: about 1.8 s (previously it did not finish).

## `POST /consolidate` (changed: preview, then apply)

Request fields added (all optional, old callers unaffected):

| Field | Type | Meaning |
|---|---|---|
| `dry_run` | bool | Return the LLM draft; write nothing. |
| `merged` | string | Apply this previewed draft instead of calling the LLM. |
| `source_sha`, `target_sha` | string | SHA-256 of each page from the preview. Required with `merged`. |

Dry-run response: `preview`, `source`, `target`, `source_sha`, `target_sha`,
`input_chars`, `merged_chars`.

Apply response adds `backup`: the vault-relative folder holding both
originals.

Errors: `409` when a page changed after the preview. `400`, `404` as before.

## `POST /consolidation-candidates/dismiss` (new)

`{ "source", "target" }` → `{ "success": true }`. Stores the pair in
`_wiki/meta/consolidation-dismissed.json`. Order doesn't matter.

## Frontend

- New `TidyUpSection({ onMerge })` below `NextUpSection`. It fetches its own
  data on mount and shows a loading line while the scan runs.
- `ConsolidateModal` gains an optional `force` prop and is now two steps for
  every caller: Preview merge (dry run) → Apply merge. The Lint panel's merges
  get the preview too.
- `TidyUpSection` rows have a "Not a duplicate" button.
- `DashboardTab` tiles render `current − previous` under each value when
  `previous` exists.

## Round 3 additions

- `GET /consolidation-candidates` response adds `merge_max_chars` (int).
- `POST /consolidate` without `merged` returns **413** when the two pages
  together exceed `CONSOLIDATE_MAX_INPUT_CHARS` (12,000 characters). No LLM
  call is made.
- New frontend components: `ComparePairModal({ pair, mergeMax, onClose,
  onOpenPage, onMerge, onDecided })` and `ComparePane({ page, other })`.
  "Link them" calls `POST /add-link` in both directions, then dismisses the pair.

## Round 4 additions

### `GET /attention` (new, read-only, no LLM)

Query: `pairs_limit` (default 8), `orphans_limit` (default 8).

```json
{
  "contradictions": [{ "name": "ai-failure-mode-diagnostics", "folder": "cs", "reasons": ["…conflict marker(s)"] }],
  "pairs":   [{ "source", "target", "score", "confidence", "reasons" }],
  "orphans": [{ "source", "target", "score", "reasons", "folder" }],
  "counts":  { "contradictions": 1, "pairs": 8, "unlinked_total": 72 },
  "merge_max_chars": 12000
}
```

Invariants: a page in `pairs` is never also listed in `orphans`, and an
orphan's partner is never a dismissed pair. Typical latency is about 3 s on
215 pages, running in a worker thread.

### `consolidation.best_partners(slugs, exclude_pairs) -> {slug: pair}` (new)

Best-scoring other page for each slug, using `_pair_score`, skipping
dismissed pairs and `exclude_pairs`.

### Frontend

- `NeedsAttentionSection({ onMerge, onOpenPage, refreshKey })` replaces
  `NextUpSection` and `TidyUpSection`.
- `ComparePairModal` takes `kind: "duplicate" | "unlinked"`, which changes the
  heading and turns "Not a duplicate" into "Not related".
- `PageReaderModal({ name, onClose, onOpenPage })` reads a page in place.
- `DashboardTab` and `BrowseTab` take `active: boolean`. All tabs stay mounted.
- `BrowseTab`'s `openTarget` effect depends on the object, so each request
  re-fires, including reopening the same page.
- Reads under 2 s are not logged on any path (Back, page switch, leaving the
  tab, unload).
