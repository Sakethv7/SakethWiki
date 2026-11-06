"""
Merge a queued clip into its target page. Builds a preview. Never writes.

One Haiku call rewrites the page body so each fact appears once. Fixed checks
reject a rewrite that drops content. Frontmatter never goes through the model.
See docs/bulk-resolve-walkthrough/ (change 3).
"""
from __future__ import annotations

import re
from typing import Optional

import capture_compare
import llm_client

_MODEL = "claude-haiku-4-5-20251001"
MAX_BODY_CHARS = 24000   # a longer page is not merged: the model output would be cut off
MIN_LENGTH_RATIO = 0.6   # merged body must keep at least this share of the old length


class MergeError(Exception):
    """kind: no_target | too_long | model_failed | merge_rejected"""

    def __init__(self, kind: str, reason: str):
        super().__init__(reason)
        self.kind = kind
        self.reason = reason


_PROMPT = """Merge new claims into an existing wiki page. Return the full new page body in Markdown.

EXISTING PAGE BODY:
{page}

NEW CLAIMS:
{claims}

Rules:
- State every fact from the page and from the claims once. Remove repeats.
- Keep the page's headings, tables, wikilinks, numbers and structure. Do not shorten the page.
- Update the "Current understanding" block so it covers the merged content.
- If a claim differs from or contradicts the page, keep both statements and add this line right after them:
  > **CONTRADICTION flagged** — the page says X; the new source says Y.
  Never pick a winner.
- Add no commentary. Return only the page body, starting with the first heading."""


def _numbers(text: str) -> set[str]:
    return set(re.findall(r"\d+(?:[.,]\d+)*", text or ""))


def _first_heading(text: str) -> str:
    m = re.search(r"^# .+$", text, flags=re.MULTILINE)
    return m.group(0).strip() if m else ""


def check_merged(old_body: str, merged: str, claims: list[str]) -> Optional[str]:
    """Return a reason if the merged body fails a check, else None."""
    if not merged.strip():
        return "The model returned no text."
    if len(merged) < MIN_LENGTH_RATIO * len(old_body):
        return "The merged page is much shorter than the old page. Content may have been dropped."
    old_heading = _first_heading(old_body)
    if old_heading and _first_heading(merged) != old_heading:
        return "The first heading changed."
    dropped = _numbers(old_body) - _numbers(merged)
    if len(dropped) > len(_numbers(" ".join(claims))):
        return f"{len(dropped)} numbers from the old page are missing from the merged page."
    return None


def _clean(raw: str) -> str:
    raw = raw.strip()
    if raw.startswith("```"):
        raw = re.sub(r"\A```[a-zA-Z]*\n", "", raw)
        raw = re.sub(r"\n```\Z", "", raw)
    return raw.strip()


def build_merge(item: dict) -> dict:
    """Return {merged_body, old_body, target_page, target_hash, notes}. Raises MergeError."""
    target = (item.get("conflict_report") or {}).get("target_page")
    path = capture_compare.find_page_path(target) if target else None
    if not path:
        raise MergeError("no_target", "This clip has no target page to merge into.")
    thash = capture_compare.page_hash(target)
    old_body = capture_compare._strip_frontmatter(path.read_text(encoding="utf-8")).strip()
    if len(old_body) > MAX_BODY_CHARS:
        raise MergeError("too_long", f"The page is over {MAX_BODY_CHARS} characters. Use Add to page.")
    claims = capture_compare.clip_claims(item)
    numbered = "\n".join(f"{i + 1}. {c}" for i, c in enumerate(claims))
    try:
        raw = llm_client.complete(
            task="page_merge",
            model=_MODEL,
            max_tokens=8000,
            messages=[{"role": "user", "content": _PROMPT.format(page=old_body, claims=numbered)}],
        )
    except Exception as e:
        raise MergeError("model_failed", str(e)[:200]) from e
    merged = _clean(raw)
    reason = check_merged(old_body, merged, claims)
    if reason:
        raise MergeError("merge_rejected", reason)
    return {
        "merged_body": merged,
        "old_body": old_body,
        "target_page": target,
        "target_hash": thash,
        "notes": [f"{len(old_body)} characters before, {len(merged)} after."],
    }
