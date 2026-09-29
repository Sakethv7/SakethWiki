# Daily Revision — Decisions

## ADR-1: Replace Interview instead of adding a tab

**Context.** Interview has been used once. Chat answers questions better and
is used about 22 times a month. The one thing only Interview did was grade
your own answer, which means recalling from memory.

**Options.**

| Option | Summary |
|---|---|
| A. Keep Interview, add Revise | Two self-test tabs. |
| B. Replace Interview with Revise | One tab. `/interview`, its verifier and grader are removed. |
| C. Put revision inside Chat | No new tab. Harder to make a daily habit. |

**Choice: B.** It keeps the useful idea (recall practice), makes it proactive,
and removes about 250 lines of frontend plus the interview backend path.

**Given up.** Asking an arbitrary question and being graded on it. Chat covers
the asking. Grading is replaced by rating yourself (ADR-3).

## ADR-2: The Mac app schedules and notifies; the backend decides content

**Context.** You chose Mac notifications. Something must fire each morning and
post a notification that opens SakethWiki when clicked.

**Options.**

| Option | Summary |
|---|---|
| A. Backend timer + `osascript display notification` | No Swift changes. The notification shows as "Script Editor", and clicking it opens Script Editor, not SakethWiki. |
| B. Mac app timer + `UNUserNotificationCenter` | Real SakethWiki notifications, and a click opens the Revise tab. Needs notification permission, and depends on the app running. |
| C. A launchd job | Runs even when the app is closed. It is a second moving part to install and debug, and has the same click problem as A. |

**Choice: B.** The notification exists to get you into the Revise tab, so a
click that opens something else defeats it. The app already keeps running
after its window closes, so the timer is usually alive. If the app wasn't
running at the scheduled time, it notifies on the next launch that day.

**Given up.** Mornings when the app isn't running at all get no notification
until you open it. It also depends on ad-hoc signing being enough for
notifications, which must be checked first (architecture open question 1).
Option A stays as the fallback.

## ADR-3: Rate yourself instead of LLM grading

**Context.** Interview graded typed answers with an LLM. Revision could do the same.

**Options.**

| Option | Summary |
|---|---|
| A. Self-rating: Forgot / Shaky / Knew it | One tap. No LLM. |
| B. Type an answer, the LLM grades it | More rigorous. Slower, costs a call per question, and typing 5 answers every morning is a lot to ask. |

**Choice: A.** The habit only survives if it's fast. Recall happens in your
head either way. Seeing the real answer is what tells you whether you had it.

**Given up.** An objective score. You can be generous with yourself. If the
ratings turn out unreliable, B can be added as an optional "check me" button later.

## ADR-4: Three-rating interval table instead of SM-2

**Context.** Real spaced-repetition systems (SM-2, FSRS) track a per-item
"ease" and fit intervals over many reviews.

**Choice.** A fixed table keyed on the last rating: forgot → due in 1 day,
shaky → 3 days, knew it → double the previous interval, starting at 7 and
capped at 60.

**Given up.** Precision. SM-2 adapts better over months. With about 5 items a
day and a couple of hundred pages, the simple table is easy to reason about,
and it can be swapped for SM-2 later without changing the stored ratings.

## ADR-5: LLM-written question bank, cached per page and content hash

**Context.** You chose LLM questions. They could be generated per day, per
page, or for the whole vault at once.

**Options.**

| Option | Summary |
|---|---|
| A. Generate for the whole vault up front | About $0.10 and a few minutes. Ready immediately. Most questions may never be used. |
| B. Generate lazily when a page is first picked | About $0.002/day. The first open of a new day may wait a few seconds. |
| C. Regenerate daily | Fresh wording each day, and a recurring cost. |

**Choice: B,** with generation started in the background as soon as the daily
set is picked, so it is usually ready by the time you click the notification.
The cache key includes a hash of the page content, so edits and merges
regenerate that page's questions.

**Given up.** The very first open of a day can show "preparing questions…" for
a few seconds. Option A removes that, and remains a one-command backfill if you
want it.

## ADR-6: Seeded daily pick with age buckets

**Context.** "Shuffle every day" should not reshuffle on every app open.

**Choice.** A random generator seeded with the date string picks from three
age buckets, after pages that are due from earlier ratings. The same day gives
the same set. A new day gives a new set.

**Given up.** Pure "most-at-risk first" ordering. The buckets guarantee a mix
of old and new pages even when many recent pages are due, which is the point
of revising older knowledge.
