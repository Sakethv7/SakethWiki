# Daily Revision — Interface Contracts

## `GET /revision/today`

Query: `summary` (bool, default false).

Full response:

```json
{
  "date": "2026-09-30",
  "topic": {
    "name": "kv-cache",
    "title": "KV Cache",
    "folder": "cs",
    "why": "not read in 74 days"
  },
  "items": [
    {
      "slug": "kv-cache",
      "title": "KV Cache",
      "folder": "cs",
      "bucket": "months",
      "status": "ready",
      "question": "Why does the KV-cache make decoding memory-bound?",
      "answer": "Each step re-reads every cached key and value …",
      "rating_today": null
    }
  ],
  "ready": true
}
```

| Field | Type | Meaning |
|---|---|---|
| `items[].bucket` | `"due" \| "recent" \| "weeks" \| "months" \| "any"` | Why it was picked. |
| `items[].status` | `"ready" \| "preparing" \| "unavailable"` | Whether a question exists yet. |
| `items[].question`, `answer` | string or null | Null unless `status` is `"ready"`. |
| `items[].rating_today` | `"forgot" \| "shaky" \| "knew"` or null | Already rated today, so a reload keeps progress. |
| `ready` | bool | True when no item is `"preparing"`. |

Summary response (`?summary=1`): `{ "date", "topic": {name, title, why}, "item_count" }`.

**Invariants**

- For a given `date`, the item slugs and the topic are identical on every
  call, as long as the vault and the rating log don't change.
- Never raises for an LLM failure. The failing item becomes `"unavailable"`.
- Writes only `_wiki/meta/revision-questions.json`, from the background
  thread. Never writes vault pages.

## `POST /revision/rate`

```json
{ "slug": "kv-cache", "rating": "knew" }
```

Response: `{ "success": true, "next_due": "2026-10-07" }`.

Errors: `422` for an unknown rating, `404` for an unknown page.

## Files

| File | Shape |
|---|---|
| `_wiki/meta/revision-questions.json` | `{ slug: { content_sha, generated_at, questions: [{question, answer}] } }` |
| `_wiki/meta/revision-log.jsonl` | one line per rating: `{ts, day, slug, rating, next_due}` |

## `backend/revision.py` (new module, pure where possible)

```python
def pick_daily_set(day: date, pages: list[PageInfo], log: list[dict], size: int = 5) -> list[Pick]
def topic_of_the_day(day: date, pages: list[PageInfo], exclude: set[str]) -> Topic
def next_due(rating: str, day: date, last_knew_interval: int | None) -> date
def questions_for(slug: str) -> list[dict]      # cached; LLM on miss
```

`PageInfo`, `Pick` and `Topic` are dataclasses. The first three functions take
all their inputs as arguments, so tests can pass fixed dates and pages.

## LLM task `revision_questions`

- Routed like any task: `LLM_PROVIDER_REVISION_QUESTIONS` /
  `LLM_MODEL_REVISION_QUESTIONS` override. Default: Gemini 2.5 Flash.
- `expect_json=True`, `required_json_keys=["questions"]`, `max_tokens=800`.
- Output: `{"questions": [{"question": str, "answer": str}, …]}`, 2–3 items.

## Frontend

- `ReviseTab()` replaces `InterviewTab`: one card at a time, "Show answer",
  then Forgot / Shaky / Knew it, and "Open page" (opens Browse).
- `TopicOfTheDayCard({ onOpenPage, onDeepDive })` on the Dashboard, above
  Needs attention. "Deep dive" opens the page in the reader; "Ask about it"
  switches to Chat with the input prefilled.
- `App` reads `location.hash`. `#revise` selects the Revise tab on load and
  on `hashchange`.

## Mac app (`macapp/main.swift`)

- `UNUserNotificationCenter.requestAuthorization([.alert, .sound])` on first launch.
- A 15-minute `Timer`: if the local time is ≥ the configured hour and
  `UserDefaults["lastRevisionNotify"] != today`, call
  `GET /revision/today?summary=1`, post the notification, and store today.
- `userNotificationCenter(_:didReceive:)` shows the window and loads
  `dashboardURL` with `#revise`.
