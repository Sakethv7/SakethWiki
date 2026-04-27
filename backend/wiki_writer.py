"""
Writes approved queue items to the Obsidian vault with knowledge evolution.

Evolution model — each new source is classified against existing understanding:
  extends    → adds new depth/breadth, understanding grows
  refines    → corrects or clarifies a specific point
  supersedes → old info is now outdated, flagged in the page
  duplicates → already known, source logged but section not appended
  contradicts→ conflict flagged for human attention

Each concept page has a living "Current understanding" block at the top
that gets rewritten on every write. Source evidence accumulates below it.
"""
import json
import os
import re
from datetime import datetime
from pathlib import Path

# Auto-load .env from project root
_env_path = Path(__file__).parent.parent / ".env"
if _env_path.exists():
    for _line in _env_path.read_text().splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _, _v = _line.partition("=")
            _k, _v = _k.strip(), _v.strip()
            if _v:
                os.environ[_k] = _v

import llm_client

VAULT_PATH   = Path(os.environ.get("VAULT_PATH", "/Users/sakethv7/SakethVault"))
CONCEPTS_DIR = VAULT_PATH / "_wiki" / "concepts"
SOURCES_DIR  = VAULT_PATH / "_wiki" / "sources"
INDEX_PATH   = VAULT_PATH / "_wiki" / "index.md"
LOG_PATH     = VAULT_PATH / "_wiki" / "log.md"

EVOLUTION_TYPES = ("extends", "refines", "supersedes", "duplicates", "contradicts")


# ── public API ────────────────────────────────────────────────────────────────

def write_approved(item: dict) -> str:
    """
    Takes an approved queue item, evolves the concept page, and writes it.
    Returns the relative path of the file written plus evolution metadata.
    """
    vault = Path(os.environ.get("VAULT_PATH", "/Users/sakethv7/SakethVault"))
    concepts_dir = vault / "_wiki" / "concepts"
    sources_dir  = vault / "_wiki" / "sources"
    concepts_dir.mkdir(parents=True, exist_ok=True)
    sources_dir.mkdir(parents=True, exist_ok=True)

    page_name = _slug(item.get("suggested_page", "general"))
    page_path = concepts_dir / f"{page_name}.md"

    section = _format_section(item)

    if page_path.exists():
        existing = page_path.read_text(encoding="utf-8")
        evolution = _analyze_evolution(existing, item)
        evo_type = evolution.get("evolution_type", "extends")

        if evo_type == "duplicates":
            # Already know this — log it but don't clutter the page
            _append_log(item, page_path, note="duplicate — not appended")
            return str(page_path.relative_to(vault)) + " [duplicate, not written]"

        new_content = _evolve_page(existing, section, item, evolution)
    else:
        evolution = {"evolution_type": "extends", "evolution_reason": "initial entry", "updated_understanding": _distill_insight(item.get("summary", []))}
        new_content = _create_page(page_name, item, section, evolution)

    _atomic_write(page_path, new_content)

    # Auxiliary writes
    try:
        _write_source_record(item, evolution)
    except Exception:
        pass
    try:
        _update_index(concepts_dir, vault)
    except Exception:
        pass
    _append_log(item, page_path, note=evolution.get("evolution_type", ""))

    # Attach evolution info to item so callers can surface it
    item["_evolution"] = evolution
    return str(page_path.relative_to(vault))


# ── evolution engine ──────────────────────────────────────────────────────────

def _analyze_evolution(existing_content: str, item: dict) -> dict:
    """
    Use Haiku to classify how new info relates to existing page understanding.
    Returns: {evolution_type, evolution_reason, updated_understanding}
    """
    # Strip frontmatter and cap body for token efficiency
    body = _strip_frontmatter(existing_content)[:2000]
    summary = "\n".join(f"- {b}" for b in item.get("summary", []))
    key_insight = _distill_insight(item.get("summary", []))
    source_title = item.get("title", "Untitled")

    # Extract current understanding block if it exists
    current_match = re.search(r"> \*\*Current understanding.*?\*\*(.*?)(?=\n\n|\n##|\Z)", body, re.DOTALL)
    current_understanding = current_match.group(0).strip() if current_match else body[:400]

    prompt = f"""You are managing a personal ML/AI knowledge wiki. A new source is being added to an existing concept page.

EXISTING PAGE (excerpt):
{current_understanding}

---
FULL PAGE BODY (for context):
{body[:1500]}

---
NEW SOURCE: "{source_title}"
Summary:
{summary}

Key insight: {key_insight}

Classify how this new information relates to what's already on the page.
Return ONLY a JSON object with exactly these fields:
{{
  "evolution_type": "<extends|refines|supersedes|duplicates|contradicts>",
  "evolution_reason": "<one crisp sentence: why/how the understanding changes, or why it's a duplicate>",
  "updated_understanding": "<1-2 sentences: the BEST current synthesis of this concept incorporating both old and new info. Write as a confident statement of understanding, not a summary of sources.>"
}}

Definitions:
- extends: genuinely new angle, depth, or sub-topic not covered before
- refines: corrects, sharpens, or replaces a specific claim in the existing understanding
- supersedes: a significant part of the existing understanding is now outdated or wrong
- duplicates: the new source covers the same ground already captured — no new understanding
- contradicts: the new source conflicts with existing content in a way that needs human review

Be strict about duplicates — if the core insight is already captured, call it a duplicate."""

    try:
        raw = llm_client.complete(
            task="evolution_classify",
            model="claude-haiku-4-5-20251001",
            max_tokens=400,
            messages=[{"role": "user", "content": prompt}],
            expect_json=True,
            required_json_keys=["evolution_type", "evolution_reason", "updated_understanding"],
        ).strip()
        # Strip markdown fences if present
        if "```" in raw:
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        result = json.loads(raw.strip())
        if result.get("evolution_type") not in EVOLUTION_TYPES:
            result["evolution_type"] = "extends"
        return result
    except Exception:
        # Fallback: always extend on error — never drop content
        return {
            "evolution_type": "extends",
            "evolution_reason": "new source added",
            "updated_understanding": _distill_insight(item.get("summary", [])),
        }


def _evolve_page(existing: str, section: str, item: dict, evolution: dict) -> str:
    """
    Update an existing page with new content and evolved understanding block.
    """
    today = datetime.now().strftime("%Y-%m-%d")
    source_url = item.get("url", "")
    evo_type    = evolution.get("evolution_type", "extends")
    evo_reason  = evolution.get("evolution_reason", "")
    new_understanding = evolution.get("updated_understanding", "")

    # Update frontmatter counters
    existing = re.sub(r"(last_updated:\s*)[\d-]+", f"\\g<1>{today}", existing)
    def _inc(m): return m.group(1) + str(int(m.group(2)) + 1)
    existing = re.sub(r"(entry_count:\s*)(\d+)", _inc, existing)
    # Bump understanding_version
    def _inc_ver(m): return m.group(1) + str(int(m.group(2)) + 1)
    if re.search(r"understanding_version:\s*\d+", existing):
        existing = re.sub(r"(understanding_version:\s*)(\d+)", _inc_ver, existing)
    else:
        # Insert it before last_updated
        existing = re.sub(r"(last_updated:)", r"understanding_version: 2\n\1", existing, count=1)
    # Record evolution type
    if re.search(r"last_evolution:", existing):
        existing = re.sub(r"last_evolution:.*", f'last_evolution: "{evo_type} · {evo_reason}"', existing)
    else:
        existing = re.sub(r"(last_updated:)", f'last_evolution: "{evo_type} · {evo_reason}"\n\\1', existing, count=1)

    # Append source URL to sources list if not already there
    if source_url and source_url not in existing:
        existing = re.sub(
            r'(sources:\s*\[)([^\]]*?)(\])',
            lambda m: m.group(1) + (m.group(2).rstrip() + ', ' if m.group(2).strip() else '') + f'"{source_url}"' + m.group(3),
            existing, count=1,
        )

    # Update or insert the Current Understanding block
    evo_badge = {"extends": "🔵", "refines": "🟡", "supersedes": "🟠", "contradicts": "🔴"}.get(evo_type, "🔵")
    understanding_block = (
        f"> **Current understanding** {evo_badge}\n"
        f"> {new_understanding}\n"
        f"> *— evolved {today} · {evo_type}: {evo_reason}*"
    )

    # Replace existing understanding block if present, otherwise insert after page heading
    if re.search(r"> \*\*Current understanding", existing):
        existing = re.sub(
            r"> \*\*Current understanding\*\*.*?(?=\n\n|\n##)",
            understanding_block,
            existing, count=1, flags=re.DOTALL,
        )
    else:
        # Insert after the # Title line
        existing = re.sub(
            r"(^# .+\n)",
            f"\\1\n{understanding_block}\n",
            existing, count=1, flags=re.MULTILINE,
        )

    # Add superseded warning inline if needed
    if evo_type == "supersedes":
        section = f"⚠️ *This source supersedes earlier understanding: {evo_reason}*\n\n" + section
    elif evo_type == "contradicts":
        section = f"⚠️ *CONTRADICTION flagged — review manually: {evo_reason}*\n\n" + section

    return existing.rstrip() + "\n\n" + section + "\n"


# ── page creation ─────────────────────────────────────────────────────────────

def _create_page(page_name: str, item: dict, section: str, evolution: dict) -> str:
    tags = item.get("tags", [])
    source_url = item.get("url", "")
    today = datetime.now().strftime("%Y-%m-%d")
    display_title = item.get("suggested_page", page_name).replace("-", " ").title()
    initial_understanding = evolution.get("updated_understanding", _distill_insight(item.get("summary", [])))

    tags_str = "[" + ", ".join(tags) + "]"
    sources_str = '["' + source_url + '"]' if source_url else "[]"

    frontmatter = f"""---
title: "{display_title}"
date: {today}
tags: {tags_str}
sources: {sources_str}
last_updated: {today}
understanding_version: 1
last_evolution: "extends · initial entry"
entry_count: 1
---

# {display_title}

> **Current understanding** 🔵
> {initial_understanding}
> *— created {today} · initial entry*

"""
    return frontmatter + section + "\n"


# ── section formatting ────────────────────────────────────────────────────────

def _format_section(item: dict) -> str:
    """Pure Python template — deterministic, no LLM."""
    source_url    = item.get("url", "")
    source_title  = item.get("title", "Untitled")
    summary_bullets = item.get("summary", [])
    wikilinks     = item.get("suggested_wikilinks", [])
    today         = datetime.now().strftime("%Y-%m-%d")

    bullets  = "\n".join(f"- {b}" for b in summary_bullets)
    normalized = [_wikilink_to_kebab(w) for w in wikilinks]
    related  = ", ".join(f"[[{w}]]" for w in normalized) if normalized else ""
    key_insight = _distill_insight(summary_bullets)

    section = f"## [{source_title}]({source_url}) · {today}\n\n{bullets}\n\n**Key insight:** {key_insight}\n"
    if related:
        section += f"\nRelated: {related}\n"

    refs = [r for r in item.get("references", []) if r and r.startswith("http")]
    if refs:
        section += "\n**Links:** " + " · ".join(f"[↗]({r})" for r in refs) + "\n"

    diagram = item.get("diagram", "").strip()
    if diagram:
        section += f"\n```mermaid\n{diagram}\n```\n"

    section += "\n---"
    return section


# ── vault index / log ─────────────────────────────────────────────────────────

def _update_index(concepts_dir: Path = None, vault: Path = None) -> None:
    if concepts_dir is None:
        concepts_dir = Path(os.environ.get("VAULT_PATH", "/Users/sakethv7/SakethVault")) / "_wiki" / "concepts"
    if vault is None:
        vault = Path(os.environ.get("VAULT_PATH", "/Users/sakethv7/SakethVault"))
    index_path = vault / "_wiki" / "index.md"
    today = datetime.now().strftime("%Y-%m-%d")
    pages = []
    for md_file in sorted(concepts_dir.glob("*.md")):
        content = md_file.read_text(encoding="utf-8")
        meta = _parse_frontmatter(content)
        tags = meta.get("tags", [])
        tags_str = ", ".join(tags) if isinstance(tags, list) else str(tags)
        entry_count  = meta.get("entry_count", 1)
        last_updated = meta.get("last_updated", today)
        evo_version  = meta.get("understanding_version", "1")
        pages.append(f"| [[{md_file.stem}]] | {tags_str} | {entry_count} | v{evo_version} | {last_updated} |")

    total_entries = sum(
        int(_parse_frontmatter(f.read_text()).get("entry_count", 1))
        for f in concepts_dir.glob("*.md")
    )

    rows = "\n".join(pages) if pages else "| — | — | — | — | — |"
    index_content = f"""# SakethWiki Index

Last updated: {today}
Total pages: {len(pages)}
Total entries: {total_entries}

## Concepts
| Page | Tags | Entries | Understanding | Last Updated |
|------|------|---------|---------------|--------------|
{rows}

## Recent Sources
"""
    log_path = vault / "_wiki" / "log.md"
    if log_path.exists():
        log_lines = log_path.read_text(encoding="utf-8").splitlines()
        recent = []
        i = len(log_lines) - 1
        while i >= 0 and len(recent) < 10:
            line = log_lines[i].strip()
            if line.startswith("Source:"):
                recent.append("- " + line[len("Source:"):].strip())
            i -= 1
        if recent:
            index_content += "\n".join(reversed(recent)) + "\n"

    _atomic_write(index_path, index_content)


def _append_log(item: dict, page_path: Path, note: str = "") -> None:
    vault = Path(os.environ.get("VAULT_PATH", "/Users/sakethv7/SakethVault"))
    log_path = vault / "_wiki" / "log.md"
    today = datetime.now().strftime("%Y-%m-%d %H:%M")
    tags = item.get("tags", [])
    tags_str = "[" + ", ".join(tags) + "]"
    source_url = item.get("url", "")
    rel_path = str(page_path.relative_to(vault))
    note_line = f"\nNote: {note}" if note else ""

    entry = f"""
## {today} · ingest
Source: {source_url}
Written to: {rel_path}
Tags: {tags_str}{note_line}
"""
    try:
        if log_path.exists():
            log_path.chmod(0o644)
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(entry)
    except Exception:
        pass


def _write_source_record(item: dict, evolution: dict = None) -> None:
    """Write a full source record to _wiki/sources/."""
    vault = Path(os.environ.get("VAULT_PATH", "/Users/sakethv7/SakethVault"))
    sources_dir = vault / "_wiki" / "sources"
    source_url = item.get("url", "")
    today = datetime.now().strftime("%Y-%m-%d")
    slug = _slug(item.get("title", "untitled"))[:60]
    source_path = sources_dir / f"{today}-{slug}.md"

    lenses = item.get("lenses", {})
    synthesis = item.get("synthesis", "")
    open_questions = item.get("open_questions", [])

    deep_section = ""
    if lenses:
        lens_blocks = ""
        for key, lens in lenses.items():
            confidence = lens.get("confidence", "")
            conf_badge = {"high": "🟢", "medium": "🟡", "low": "🔴"}.get(confidence, "")
            lens_blocks += f"\n### {lens.get('label', key)} {conf_badge}\n{lens.get('finding', '')}\n"
        deep_section = f"\n## Deep Research\n{lens_blocks}"
        if synthesis:
            deep_section += f"\n### Synthesis\n{synthesis}\n"
        if open_questions:
            deep_section += f"\n### Open Questions\n{chr(10).join(f'- {q}' for q in open_questions)}\n"

    evo_section = ""
    if evolution:
        evo_type   = evolution.get("evolution_type", "")
        evo_reason = evolution.get("evolution_reason", "")
        evo_section = f"\n## Knowledge Evolution\n**Type:** {evo_type}\n**Reason:** {evo_reason}\n"

    content = f"""---
title: "{item.get('title', 'Untitled')}"
date: {today}
url: "{source_url}"
tags: {item.get('tags', [])}
written_to: "{item.get('suggested_page', 'general')}"
evolution_type: "{(evolution or {}).get('evolution_type', 'extends')}"
deep_research: {bool(lenses)}
---

# {item.get('title', 'Untitled')}

**URL:** {source_url}
**Date ingested:** {today}

## Summary
{chr(10).join(f"- {b}" for b in item.get('summary', []))}

## Key Concepts
{chr(10).join(f"- {c}" for c in item.get('key_concepts', []))}

## Wikilinks
{", ".join(f"[[{w}]]" for w in item.get('suggested_wikilinks', []))}{evo_section}{deep_section}
"""
    _atomic_write(source_path, content)


# ── public utilities ──────────────────────────────────────────────────────────

def fix_page_wikilinks(page_name: str) -> int:
    """Normalize all wikilinks in a concept page to kebab-case in-place."""
    vault = Path(os.environ.get("VAULT_PATH", "/Users/sakethv7/SakethVault"))
    page_path = vault / "_wiki" / "concepts" / f"{page_name}.md"
    if not page_path.exists():
        return 0
    content = page_path.read_text(encoding="utf-8")
    original = content

    def _normalize(m: re.Match) -> str:
        return f"[[{_wikilink_to_kebab(m.group(1))}]]"

    content, n = re.subn(r"\[\[([^\]]+)\]\]", _normalize, content)
    if content != original:
        _atomic_write(page_path, content)
    return n


# ── utilities ─────────────────────────────────────────────────────────────────

def _strip_frontmatter(content: str) -> str:
    if not content.startswith("---"):
        return content
    end = content.find("---", 3)
    if end == -1:
        return content
    return content[end + 3:].strip()


def _wikilink_to_kebab(text: str) -> str:
    text = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "-", text)
    text = re.sub(r"[\s_]+", "-", text)
    text = re.sub(r"[^\w-]", "", text)
    return text.lower().strip("-")


def _distill_insight(bullets: list) -> str:
    if not bullets:
        return ""
    raw = bullets[-1]
    for opener in ("I learned that ", "This shows that ", "I learned ", "This shows "):
        if raw.startswith(opener):
            raw = raw[len(opener):]
            raw = raw[0].upper() + raw[1:]
            break
    return raw


def _atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(content, encoding="utf-8")
    tmp.rename(path)
    try:
        import subprocess
        subprocess.run(["xattr", "-c", str(path)], capture_output=True)
    except Exception:
        pass


def _slug(text: str) -> str:
    text = text.lower()
    text = re.sub(r"[^\w\s-]", "", text)
    text = re.sub(r"[\s_]+", "-", text)
    text = text.strip("-")
    return text or "general"


def _parse_frontmatter(content: str) -> dict:
    meta: dict = {}
    if not content.startswith("---"):
        return meta
    end = content.find("---", 3)
    if end == -1:
        return meta
    fm = content[3:end]
    for line in fm.splitlines():
        if ":" not in line:
            continue
        key, _, val = line.partition(":")
        key = key.strip()
        val = val.strip().strip('"').strip("'")
        if val.startswith("[") and val.endswith("]"):
            val = [v.strip().strip('"').strip("'") for v in val[1:-1].split(",") if v.strip()]
        meta[key] = val
    return meta
