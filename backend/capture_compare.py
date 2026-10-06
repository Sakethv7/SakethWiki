"""
Capture-time conflict review.

Compares an extracted clip with the closest existing wiki page and sorts the
result into a band: duplicate / overlap / conflict / distinct / unknown.
Reads the vault. Never writes to the vault or the queue.

Method: token containment only picks the target page. One Haiku call then
labels each clip claim (same / new / changed / conflicts) and fixed rules turn
the labels into a band. There is no token-overlap shortcut to "duplicate":
calibration showed it marks a clip with one changed number as a duplicate.
See docs/capture-conflict-review/.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
from datetime import datetime
from pathlib import Path
from typing import Optional

import identity
import llm_client
import vault_reader

logger = logging.getLogger(__name__)

# Starting value. Calibrate on labeled pairs (backend/calibrate_compare.py).
CANDIDATE_FLOOR = 0.35         # min containment for a page the extractor did not name
PAGE_CHAR_CAP = 6000
SAME_SUPPORT_MIN = 0.45        # share of a "same" claim's terms that its page quote must contain
FLAG_SUPPORT_MIN = 0.15        # a changed/conflicts flag needs a quote that is about the same point

VERDICTS = ("same", "new", "changed", "conflicts")
_MODEL = "claude-haiku-4-5-20251001"


def _tokens(text: str) -> set[str]:
    return {t for t in re.findall(r"[a-z0-9]+", text.lower()) if len(t) > 1}


def _numbers(text: str) -> set[str]:
    return set(re.findall(r"\d+(?:[.,]\d+)*", text or ""))


def _norm(text: str) -> str:
    return " ".join(text.split()).lower()


def _strip_frontmatter(content: str) -> str:
    return re.sub(r"\A---\n.*?\n---\n?", "", content, count=1, flags=re.DOTALL)


def find_page_path(slug: str) -> Optional[Path]:
    """Path of the page file for a slug, or None. Searches all vault folders."""
    wanted = {identity.resolve_slug(slug).lower(), identity.slugify(slug).lower()}
    for folder in vault_reader._dirs().values():
        if not folder.exists():
            continue
        for md in folder.glob("*.md"):
            if md.stem.lower() in wanted:
                return md
    return None


def page_hash(slug: str) -> str:
    path = find_page_path(slug)
    if not path:
        return ""
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return ""


def clip_claims(item: dict) -> list[str]:
    return [str(c).strip() for c in (item.get("summary") or []) if str(c).strip()]


def _clip_text(item: dict) -> str:
    return " ".join([item.get("title", ""), *clip_claims(item)])


def _containment(clip_tokens: set[str], page_tokens: set[str]) -> float:
    return len(clip_tokens & page_tokens) / len(clip_tokens) if clip_tokens else 0.0


def pick_target(item: dict) -> tuple[Optional[str], float]:
    """Return (slug, containment). The extractor's suggested page wins if it exists."""
    clip_tokens = _tokens(_clip_text(item))
    suggested = identity.resolve_slug(item.get("suggested_page", "") or "")
    best: tuple[Optional[str], float] = (None, 0.0)
    for page in vault_reader.list_concept_pages():
        text = vault_reader.read_page(page["name"]) or ""
        score = _containment(clip_tokens, _tokens(page["name"].replace("-", " ") + " " + text[:PAGE_CHAR_CAP]))
        if page["name"] == suggested:
            return page["name"], score
        if score > best[1]:
            best = (page["name"], score)
    if best[0] and best[1] >= CANDIDATE_FLOOR:
        return best
    return None, best[1]


def band_from_verdicts(verdicts: list[str]) -> str:
    if any(v in ("changed", "conflicts") for v in verdicts):
        return "conflict"
    if any(v == "new" for v in verdicts):
        return "overlap"
    if verdicts and all(v == "same" for v in verdicts):
        return "duplicate"
    return "unknown"


def _recommend(band: str, target: Optional[str]) -> Optional[str]:
    if band == "overlap":
        return "append"
    if band in ("distinct", "unknown") and target:
        return "keep_both"
    return None


def _report(band: str, target: Optional[str], thash: str, score: float, claims: list[dict],
            reason: str, error: Optional[str] = None) -> dict:
    return {
        "band": band,
        "target_page": target,
        "target_hash": thash,
        "match_score": round(score, 3),
        "claims": claims,
        "recommended": _recommend(band, target),
        "reason": reason,
        "error": error,
        "built_at": datetime.now().isoformat(timespec="seconds"),
    }


_PROMPT = """You compare a new source with an existing wiki page. Label each NEW CLAIM.

EXISTING PAGE:
{page}

NEW CLAIMS:
{claims}

Labels:
- same: the page already states this claim. Different wording, synonyms or sentence order do NOT matter. The meaning and the numbers must match.
- new: the page does not cover this claim.
- changed: the page covers this exact point but the facts differ: a different value, scope, cause or detail. Never use "changed" for wording differences alone.
- conflicts: the page states the opposite, or an incompatible fact.

For same, changed and conflicts, give "page_quote": an EXACT short quote (under 200 characters) copied from the page.
For new, page_quote is null.

Return ONLY JSON: {{"claims": [{{"i": 1, "verdict": "same|new|changed|conflicts", "page_quote": "..." }}]}}
One entry per new claim, in order."""


def _diff_claims(claims: list[str], page_body: str) -> list[dict]:
    numbered = "\n".join(f"{i + 1}. {c}" for i, c in enumerate(claims))
    raw = llm_client.complete(
        task="capture_compare",
        model=_MODEL,
        max_tokens=1500,
        messages=[{"role": "user", "content": _PROMPT.format(page=page_body, claims=numbered)}],
        expect_json=True,
        required_json_keys=["claims"],
    ).strip()
    if "```" in raw:
        raw = raw.split("```")[1]
        raw = raw[4:] if raw.startswith("json") else raw
    data = json.loads(raw.strip())
    by_index = {int(e.get("i", 0)): e for e in data.get("claims", []) if isinstance(e, dict)}
    page_norm = _norm(page_body)
    out = []
    for i, claim in enumerate(claims, start=1):
        entry = by_index.get(i) or {}
        verdict = entry.get("verdict") if entry.get("verdict") in VERDICTS else "new"
        quote = entry.get("page_quote")
        quote = quote.strip() if isinstance(quote, str) and quote.strip() else None
        if quote and _norm(quote) not in page_norm:
            quote = None  # model invented a quote: do not show it
        if verdict == "same" and not quote:
            verdict = "new"  # cannot verify, so never treat as already known
        if verdict == "same" and _containment(_tokens(claim), _tokens(quote or "")) < SAME_SUPPORT_MIN:
            verdict = "new"  # the quote does not support the claim (model picked an unrelated line)
            quote = None
        if verdict in ("changed", "conflicts") and _containment(_tokens(claim), _tokens(quote or "")) < FLAG_SUPPORT_MIN:
            verdict, quote = "new", None  # quote is about something else: no evidence for the flag
        if verdict == "same" and not _numbers(claim) <= _numbers(quote):
            verdict = "changed"  # a number in the claim is not in the page text it matched
        if verdict == "new":
            quote = None
        out.append({"claim": claim, "verdict": verdict, "page_quote": quote})
    return out


def build_report(item: dict) -> dict:
    """Return a ConflictReport for an extracted item. Never raises."""
    try:
        return _build_report(item)
    except Exception as e:  # never block a capture on the comparison
        logger.warning("capture_compare failed: %s", e)
        return _report("unknown", None, "", 0.0, [], "Could not compare with existing pages.", error=str(e)[:200])


def _build_report(item: dict) -> dict:
    claims = clip_claims(item)
    if not claims:
        return _report("unknown", None, "", 0.0, [], "Clip has no claims to compare.", error="no claims")
    target, score = pick_target(item)
    if not target:
        return _report("distinct", None, "", score, [], "No close existing page. This will be a new page.")
    path = find_page_path(target)
    if not path:
        return _report("distinct", None, "", score, [], "Target page not found. This will be a new page.")
    body = _strip_frontmatter(path.read_text(encoding="utf-8"))[:PAGE_CHAR_CAP]
    thash = page_hash(target)

    try:
        verdicts = _diff_claims(claims, body)
    except Exception as e:
        logger.warning("claim diff failed: %s", e)
        return _report("unknown", target, thash, score, [], "Could not compare claims.", error=str(e)[:200])

    band = band_from_verdicts([v["verdict"] for v in verdicts])
    counts = {k: sum(1 for v in verdicts if v["verdict"] == k) for k in VERDICTS}
    reason = {
        "duplicate": f"All {len(verdicts)} claims are already on '{target}'.",
        "overlap": f"{counts['new']} new claim(s) on '{target}'. Nothing changes or conflicts.",
        "conflict": f"{counts['changed'] + counts['conflicts']} claim(s) change or conflict with '{target}'.",
    }.get(band, "Could not classify.")
    return _report(band, target, thash, score, verdicts, reason)
