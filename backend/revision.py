"""
Daily revision: a small, seeded set of recall questions about your own pages.

Pure selection logic (pick_daily_set, topic_of_the_day, next_due) takes all
inputs as arguments so it can be tested with fixed dates. File and LLM access
live in load_pages, read_log, append_rating and questions_for.
See docs/daily-revision/.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import random
import re
import threading
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional

import active_review
import llm_client
import vault_reader

logger = logging.getLogger(__name__)

DAILY_SIZE = 5
MIN_BODY_CHARS = 400          # stubs make poor questions
RATINGS = ("forgot", "shaky", "knew")
_DEFAULT_VAULT = "/Users/sakethv7/SakethVault"
_bank_lock = threading.Lock()


@dataclass
class PageInfo:
    slug: str
    title: str
    folder: str
    created: date
    body_chars: int
    maturity: Optional[int]
    days_since_read: Optional[int]


def _meta_dir() -> Path:
    return Path(os.environ.get("VAULT_PATH", _DEFAULT_VAULT)) / "_wiki" / "meta"


# ── selection (pure) ─────────────────────────────────────────────────────────

def _bucket(age_days: int) -> str:
    if age_days <= 7:
        return "recent"
    if age_days <= 45:
        return "weeks"
    return "months"


def _latest_ratings(log: list[dict]) -> dict[str, dict]:
    latest: dict[str, dict] = {}
    for row in log:
        latest[row["slug"]] = row
    return latest


def pick_daily_set(day: date, pages: list[PageInfo], log: list[dict], size: int = DAILY_SIZE) -> list[dict]:
    """Seeded by the date: same set all day, a new one tomorrow."""
    rng = random.Random(day.isoformat())
    eligible = sorted((p for p in pages if p.body_chars >= MIN_BODY_CHARS), key=lambda p: p.slug)
    # Only ratings from before today count; otherwise rating a card would
    # reshuffle the set you're in the middle of.
    latest = _latest_ratings([r for r in log if r.get("day", "") < day.isoformat()])

    def due(p: PageInfo) -> bool:
        row = latest.get(p.slug)
        return bool(row) and date.fromisoformat(row["next_due"]) <= day

    def rated_recently(p: PageInfo) -> bool:
        row = latest.get(p.slug)
        return bool(row) and (day - date.fromisoformat(row["day"])).days < 2

    picked: list[dict] = []
    taken: set[str] = set()

    def take(candidates: list[PageInfo], n: int, bucket: str) -> None:
        pool = [p for p in candidates if p.slug not in taken]
        for p in rng.sample(pool, min(n, len(pool))):
            picked.append({"page": p, "bucket": bucket})
            taken.add(p.slug)

    take([p for p in eligible if due(p)], 2, "due")
    by_bucket = {"recent": [], "weeks": [], "months": []}
    for p in eligible:
        if not due(p) and not rated_recently(p):
            by_bucket[_bucket((day - p.created).days)].append(p)
    take(by_bucket["recent"], 1, "recent")
    take(by_bucket["weeks"], 1, "weeks")
    take(by_bucket["months"], size - len(picked), "months")
    if len(picked) < size:
        take(eligible, size - len(picked), "any")
    return picked[:size]


def topic_of_the_day(day: date, pages: list[PageInfo], exclude: set[str]) -> Optional[dict]:
    """Weighted toward weak and long-unread pages, but never certain."""
    rng = random.Random("topic:" + day.isoformat())
    candidates = sorted((p for p in pages if p.body_chars >= MIN_BODY_CHARS and p.slug not in exclude),
                        key=lambda p: p.slug)
    if not candidates:
        return None

    def terms(p: PageInfo) -> tuple[int, int]:
        weakness = 100 - (p.maturity if p.maturity is not None else 50)
        unread = min(p.days_since_read if p.days_since_read is not None else 90, 90)
        return weakness, unread

    weights = [max(1, sum(terms(p))) for p in candidates]
    p = rng.choices(candidates, weights=weights)[0]
    weakness, unread = terms(p)
    if p.days_since_read is None:
        why = "never opened in the app"
    elif unread >= weakness:
        why = f"not read in {p.days_since_read} days"
    else:
        why = f"maturity {p.maturity}" if p.maturity is not None else "no maturity score yet"
    return {"name": p.slug, "title": p.title, "folder": p.folder, "why": why}


def next_due(rating: str, day: date, last_knew_interval: Optional[int]) -> tuple[date, Optional[int]]:
    """Returns (next due date, interval to remember for the next 'knew')."""
    if rating == "forgot":
        return day + timedelta(days=1), None
    if rating == "shaky":
        return day + timedelta(days=3), None
    interval = min((last_knew_interval or 0) * 2 or 7, 60)
    return day + timedelta(days=interval), interval


# ── files ────────────────────────────────────────────────────────────────────

def load_pages(today: date) -> list[PageInfo]:
    reads = active_review._last_read_map()
    pages = []
    for meta in vault_reader.list_concept_pages():
        slug = meta["name"]
        content = vault_reader.read_page(slug) or ""
        fm = vault_reader._parse_frontmatter(content)
        try:
            created = date.fromisoformat(str(fm.get("date", ""))[:10])
        except ValueError:
            created = datetime.fromisoformat(meta["last_saved_at"]).date() if meta.get("last_saved_at") else today
        try:
            maturity = int(fm.get("understanding_maturity")) if fm.get("understanding_maturity") not in (None, "") else None
        except (TypeError, ValueError):
            maturity = None
        body = content.split("---", 2)[-1] if content.startswith("---") else content
        last_read = reads.get(slug)
        pages.append(PageInfo(
            slug=slug, title=meta.get("title") or slug, folder=meta.get("folder", "cs"),
            created=created, body_chars=len(body.strip()), maturity=maturity,
            days_since_read=(today - last_read.date()).days if last_read else None,
        ))
    return pages


def read_log() -> list[dict]:
    path = _meta_dir() / "revision-log.jsonl"
    if not path.exists():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return rows


def append_rating(slug: str, rating: str, day: date) -> date:
    log = read_log()
    last_knew = next((r.get("interval") for r in reversed(log) if r["slug"] == slug and r["rating"] == "knew"), None)
    due, interval = next_due(rating, day, last_knew)
    row = {"ts": datetime.now().isoformat(), "day": day.isoformat(), "slug": slug,
           "rating": rating, "next_due": due.isoformat(), "interval": interval}
    path = _meta_dir() / "revision-log.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row) + "\n")
    return due


def ratings_on(day: date) -> dict[str, str]:
    return {r["slug"]: r["rating"] for r in read_log() if r.get("day") == day.isoformat()}


# ── question bank ────────────────────────────────────────────────────────────

def _bank_path() -> Path:
    return _meta_dir() / "revision-questions.json"


def _load_bank() -> dict:
    path = _bank_path()
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def cached_questions(slug: str) -> Optional[list[dict]]:
    content = vault_reader.read_page(slug) or ""
    entry = _load_bank().get(slug)
    if entry and entry.get("content_sha") == hashlib.sha256(content.encode()).hexdigest():
        return entry.get("questions") or None
    return None


def _generate(slug: str, content: str) -> list[dict]:
    prompt = f"""Write 2 or 3 revision questions about this personal wiki page, for active recall.

Rules:
- Test understanding (why, when, trade-offs, how parts connect), not recall of exact wording.
- Every answer must come only from the page. At most 3 sentences each.
- No questions about dates, sources, or the page itself.

Return JSON only: {{"questions": [{{"question": "...", "answer": "..."}}]}}

Page [[{slug}]]:
{content[:12000]}"""
    raw = llm_client.complete(
        task="revision_questions",
        model=None,
        # Gemini 2.5 Flash spends part of max_tokens on hidden reasoning; at
        # 800 the visible JSON came back cut off (29-152 tokens).
        max_tokens=3000,
        messages=[{"role": "user", "content": prompt}],
        expect_json=True,
        required_json_keys=["questions"],
    ).strip()
    if raw.startswith("```"):
        raw = raw.split("```")[1].removeprefix("json").strip()
    questions = [q for q in json.loads(raw).get("questions", [])
                 if isinstance(q, dict) and q.get("question") and q.get("answer")]
    if not questions:
        raise ValueError("no usable questions returned")
    return questions[:3]


def questions_for(slug: str) -> list[dict]:
    """Cached per page and content hash; one LLM call on a miss."""
    cached = cached_questions(slug)
    if cached:
        return cached
    content = vault_reader.read_page(slug) or ""
    questions = _generate(slug, content)
    with _bank_lock:
        bank = _load_bank()
        bank[slug] = {"content_sha": hashlib.sha256(content.encode()).hexdigest(),
                      "generated_at": datetime.now().isoformat(timespec="seconds"),
                      "questions": questions}
        path = _bank_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(bank, indent=2), encoding="utf-8")
        tmp.replace(path)
    return questions


_filling: set[str] = set()
_failed_at: dict[str, datetime] = {}
RETRY_AFTER = timedelta(minutes=10)   # the UI polls every 3s; don't retry a failing LLM call that often


def _recently_failed(slug: str) -> bool:
    failed = _failed_at.get(slug)
    return bool(failed) and datetime.now() - failed < RETRY_AFTER


def fill_bank_in_background(slugs: list[str]) -> None:
    """Generate missing questions without blocking the request."""
    todo = [s for s in slugs if s not in _filling and not _recently_failed(s) and not cached_questions(s)]
    if not todo:
        return
    _filling.update(todo)

    def run() -> None:
        for slug in todo:
            try:
                questions_for(slug)
                _failed_at.pop(slug, None)
            except Exception as exc:
                _failed_at[slug] = datetime.now()
                logger.warning("revision question generation failed for %s: %s", slug, exc)
            finally:
                _filling.discard(slug)

    threading.Thread(target=run, daemon=True).start()


def is_filling(slug: str) -> bool:
    return slug in _filling


# ── the day's view ───────────────────────────────────────────────────────────

def today_view(day: date, summary: bool = False) -> dict:
    pages = load_pages(day)
    picks = pick_daily_set(day, pages, read_log())
    topic = topic_of_the_day(day, pages, exclude={p["page"].slug for p in picks})
    if summary:
        return {"date": day.isoformat(), "topic": topic, "item_count": len(picks)}

    rated = ratings_on(day)
    items = []
    for pick in picks:
        p: PageInfo = pick["page"]
        questions = cached_questions(p.slug)
        item = {"slug": p.slug, "title": p.title, "folder": p.folder, "bucket": pick["bucket"],
                "rating_today": rated.get(p.slug), "question": None, "answer": None}
        if questions:
            q = questions[random.Random(f"q:{day.isoformat()}:{p.slug}").randrange(len(questions))]
            item.update(status="ready", question=q["question"], answer=q["answer"])
        else:
            item["status"] = "preparing"
        items.append(item)

    missing = [i["slug"] for i in items if i["status"] == "preparing"]
    fill_bank_in_background(missing)
    for item in items:
        # Not generating and still missing means the last attempt failed.
        if item["status"] == "preparing" and not is_filling(item["slug"]) and not cached_questions(item["slug"]):
            item["status"] = "unavailable"
    return {"date": day.isoformat(), "topic": topic, "items": items,
            "ready": all(i["status"] != "preparing" for i in items)}
