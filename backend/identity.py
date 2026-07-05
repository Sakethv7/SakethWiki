"""
Canonical concept identity helpers.

Markdown pages stay as the source of truth. This module only answers:
"when a user or model says X, which page slug should the system treat as X?"
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Iterable

_DEFAULT_VAULT = "/Users/sakethv7/SakethVault"
_CONCEPT_FOLDERS = ("cs", "science", "humanities", "insights", "open-threads")

_BUILTIN_ALIASES: dict[str, list[str]] = {
    "rag": [
        "retrieval augmented generation",
        "retrieval-augmented generation",
        "retrieval-augmented-generation",
    ],
    "kv-cache": [
        "kv cache",
        "key value cache",
        "key-value cache",
        "attention cache",
    ],
    "llm": ["large language model", "large-language-model", "language model"],
    "embeddings": ["embedding", "semantic embeddings", "vector embeddings"],
    "agents": ["agent", "agentic", "tool use", "tool-use"],
}


def _vault() -> Path:
    return Path(os.environ.get("VAULT_PATH", _DEFAULT_VAULT))


def slugify(text: str) -> str:
    text = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "-", str(text or ""))
    text = text.lower()
    text = re.sub(r"[^\w\s-]", "", text)
    text = re.sub(r"[\s_]+", "-", text)
    return text.strip("-") or "general"


def _aliases_path() -> Path:
    return _vault() / "_wiki" / "meta" / "aliases.json"


def _load_alias_file() -> dict[str, list[str]]:
    path = _aliases_path()
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}

    if isinstance(data, dict) and isinstance(data.get("canonical"), dict):
        data = data["canonical"]

    aliases: dict[str, list[str]] = {}
    if not isinstance(data, dict):
        return aliases
    for canonical, values in data.items():
        if isinstance(values, str):
            values = [values]
        if not isinstance(values, list):
            continue
        aliases[slugify(canonical)] = [str(v) for v in values if str(v).strip()]
    return aliases


def _parse_frontmatter(content: str) -> dict:
    meta: dict = {}
    if not content.startswith("---"):
        return meta
    end = content.find("---", 3)
    if end == -1:
        return meta
    for line in content[3:end].splitlines():
        if ":" not in line:
            continue
        key, _, val = line.partition(":")
        key = key.strip()
        val = val.strip().strip('"').strip("'")
        if val.startswith("[") and val.endswith("]"):
            val = [v.strip().strip('"').strip("'") for v in val[1:-1].split(",") if v.strip()]
        meta[key] = val
    return meta


def _iter_page_aliases() -> Iterable[tuple[str, list[str]]]:
    wiki = _vault() / "_wiki"
    for folder in _CONCEPT_FOLDERS:
        folder_dir = wiki / folder
        if not folder_dir.exists():
            continue
        for md_file in folder_dir.glob("*.md"):
            try:
                meta = _parse_frontmatter(md_file.read_text(encoding="utf-8"))
            except OSError:
                continue
            aliases = meta.get("aliases", [])
            if isinstance(aliases, str):
                aliases = [aliases]
            if aliases:
                yield md_file.stem, [str(alias) for alias in aliases]


def alias_map() -> dict[str, str]:
    """
    Return {alias_slug: canonical_slug}. Canonical slugs map to themselves.
    Optional vault aliases override built-ins when they define the same alias.
    """
    mapping: dict[str, str] = {}

    def add(canonical: str, aliases: list[str]) -> None:
        canonical_slug = slugify(canonical)
        mapping.setdefault(canonical_slug, canonical_slug)
        for alias in aliases:
            mapping[slugify(alias)] = canonical_slug

    for canonical, aliases in _BUILTIN_ALIASES.items():
        add(canonical, aliases)
    for canonical, aliases in _load_alias_file().items():
        add(canonical, aliases)
    for canonical, aliases in _iter_page_aliases():
        add(canonical, aliases)
    return mapping


def resolve_slug(text: str) -> str:
    slug = slugify(text)
    return alias_map().get(slug, slug)


def aliases_for(canonical: str) -> list[str]:
    canonical_slug = resolve_slug(canonical)
    values = [
        alias
        for alias, target in alias_map().items()
        if target == canonical_slug and alias != canonical_slug
    ]
    return sorted(dict.fromkeys(values))


def expand_query(query: str) -> str:
    """
    Add canonical names and known aliases to a lexical query. This keeps the
    SQLite index local while making alias phrases searchable.
    """
    if not query or not query.strip():
        return query
    query_slugs = {slugify(term) for term in re.findall(r"[\w-]+(?:\s+[\w-]+)?", query.lower())}
    additions: list[str] = []
    mapping = alias_map()
    for alias, canonical in mapping.items():
        if alias in query_slugs or alias.replace("-", " ") in query.lower():
            additions.extend([canonical, canonical.replace("-", " ")])
            additions.extend(a.replace("-", " ") for a in aliases_for(canonical))
    if not additions:
        resolved = resolve_slug(query)
        if resolved != slugify(query):
            additions.append(resolved.replace("-", " "))
    return " ".join([query, *dict.fromkeys(additions)])


def duplicate_candidates(existing_slugs: Iterable[str] | None = None) -> list[dict]:
    """
    Report pages whose slug resolves to another canonical slug that also exists.
    These are redirect/merge candidates, not automatic merge instructions.
    """
    if existing_slugs is None:
        wiki = _vault() / "_wiki"
        existing_slugs = []
        for folder in _CONCEPT_FOLDERS:
            folder_dir = wiki / folder
            if folder_dir.exists():
                existing_slugs = [*existing_slugs, *(p.stem for p in folder_dir.glob("*.md"))]
    existing = {slugify(slug) for slug in existing_slugs}
    issues = []
    for slug in sorted(existing):
        canonical = resolve_slug(slug)
        if canonical != slug and canonical in existing:
            issues.append(
                {
                    "alias": slug,
                    "canonical": canonical,
                    "reason": f"'{slug}' resolves to canonical page '{canonical}'",
                }
            )
    return issues
