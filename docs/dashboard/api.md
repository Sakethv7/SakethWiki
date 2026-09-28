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
