"""
Conservative duplicate-page consolidation helpers.

This module proposes merge candidates. It does not mutate files.
"""
from __future__ import annotations

import json
import os
import re
from difflib import SequenceMatcher
from functools import lru_cache
from pathlib import Path

import identity
import vault_reader

_DEFAULT_VAULT = "/Users/sakethv7/SakethVault"


def _vault() -> Path:
    return Path(os.environ.get("VAULT_PATH", _DEFAULT_VAULT))


def _dismissed_path() -> Path:
    return _vault() / "_wiki" / "meta" / "consolidation-dismissed.json"


def _pair_key(a: str, b: str) -> tuple[str, str]:
    return tuple(sorted((identity.slugify(a), identity.slugify(b))))


def load_dismissed() -> set[tuple[str, str]]:
    """Pairs you marked "not a duplicate", order-independent."""
    path = _dismissed_path()
    if not path.exists():
        return set()
    try:
        return {_pair_key(a, b) for a, b in json.loads(path.read_text(encoding="utf-8"))}
    except (OSError, ValueError, TypeError):
        return set()


def dismiss_pair(source: str, target: str) -> None:
    pairs = load_dismissed() | {_pair_key(source, target)}
    path = _dismissed_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(sorted(pairs), indent=2), encoding="utf-8")


def _tokens(text: str) -> set[str]:
    return {t for t in re.findall(r"[a-z0-9]+", text.lower()) if len(t) > 1}


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


@lru_cache(maxsize=None)
def _page_text(slug: str) -> str:
    # Cached per run: find_candidates scores every pair, so without this each
    # page was parsed ~2N times. Entry points clear it so edits are never stale.
    parsed = vault_reader.parse_concept_page(slug) or {}
    parts = [
        slug.replace("-", " "),
        parsed.get("title", ""),
        parsed.get("current_understanding", ""),
        " ".join(parsed.get("tags", [])),
    ]
    return " ".join(p for p in parts if p)


@lru_cache(maxsize=None)
def _canonical(slug: str) -> str:
    # identity.resolve_slug rebuilds the alias map from every page on each
    # call, so resolve each slug once per run (cleared with _page_text).
    return identity.resolve_slug(slug)


def _pair_score(source: str, target: str) -> dict:
    source_text = _page_text(source)
    target_text = _page_text(target)
    slug_similarity = SequenceMatcher(None, source, target).ratio()
    token_overlap = _jaccard(_tokens(source_text), _tokens(target_text))
    canonical_source = _canonical(source)
    canonical_target = _canonical(target)
    alias_match = canonical_source == canonical_target and source != target

    score = max(slug_similarity * 0.55 + token_overlap * 0.45, 0.0)
    if alias_match:
        score = max(score, 0.98)

    reasons = []
    if alias_match:
        reasons.append("identity alias resolves both slugs to one canonical concept")
    if slug_similarity >= 0.75:
        reasons.append(f"similar slugs ({slug_similarity:.2f})")
    if token_overlap >= 0.45:
        reasons.append(f"overlapping concept text ({token_overlap:.2f})")

    confidence = "low"
    safe_auto = False
    if alias_match:
        confidence = "high"
        safe_auto = True
    elif score >= 0.82 and token_overlap >= 0.45:
        confidence = "high"
        safe_auto = True
    elif score >= 0.62:
        confidence = "medium"

    return {
        "source": source,
        "target": canonical_target,
        "score": round(score, 3),
        "confidence": confidence,
        "safe_auto": safe_auto,
        "reasons": reasons or ["weak overlap only"],
        "alias_match": alias_match,
    }


def find_candidates(limit: int = 50, include_weak: bool = False) -> list[dict]:
    _page_text.cache_clear()
    _canonical.cache_clear()
    pages = vault_reader.list_concept_pages()
    slugs = sorted({p["name"] for p in pages})
    candidates: list[dict] = []

    # Identity aliases first: highest signal and cheap.
    for issue in identity.duplicate_candidates(slugs):
        candidates.append(
            {
                "source": issue["alias"],
                "target": issue["canonical"],
                "score": 0.99,
                "confidence": "high",
                "safe_auto": True,
                "reasons": [issue["reason"]],
                "alias_match": True,
            }
        )

    dismissed = load_dismissed()
    candidates = [c for c in candidates if _pair_key(c["source"], c["target"]) not in dismissed]
    seen = {(c["source"], c["target"]) for c in candidates}
    for i, source in enumerate(slugs):
        for target in slugs[i + 1 :]:
            pair = _pair_score(source, target)
            key = (pair["source"], pair["target"])
            if key in seen or source == pair["target"] or _pair_key(source, pair["target"]) in dismissed:
                continue
            if pair["confidence"] == "low" and not include_weak:
                continue
            candidates.append(pair)
            seen.add(key)

    candidates.sort(key=lambda item: (item["safe_auto"], item["score"]), reverse=True)
    return candidates[: max(1, min(limit, 200))]


def validate_pair(source: str, target: str) -> dict:
    _page_text.cache_clear()
    _canonical.cache_clear()
    return _pair_score(identity.slugify(source), identity.resolve_slug(target))
