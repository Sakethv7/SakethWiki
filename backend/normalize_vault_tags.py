#!/usr/bin/env python3
"""
One-off script: normalize tags across all vault concept pages using tag-ontology.json.

Usage:
    cd /Users/sakethv7/Sakethwiki/backend
    source venv/bin/activate
    python normalize_vault_tags.py [--dry-run]
"""
import json
import re
import sys
from pathlib import Path

VAULT_PATH = Path("/Users/sakethv7/SakethVault")
CONCEPTS_DIR = VAULT_PATH / "_wiki" / "concepts"
ONTOLOGY_PATH = VAULT_PATH / "_wiki" / "meta" / "tag-ontology.json"

DRY_RUN = "--dry-run" in sys.argv


def build_synonym_map(ontology: dict) -> dict[str, str]:
    m = {}
    for canonical, info in ontology.items():
        for syn in info.get("synonyms", []):
            m[syn.lower()] = canonical
    return m


def normalize_tags(tags: list[str], synonym_map: dict) -> tuple[list[str], dict]:
    mappings = {}
    result = []
    seen = set()
    for tag in tags:
        canonical = synonym_map.get(tag.lower(), tag)
        if canonical != tag:
            mappings[tag] = canonical
        if canonical not in seen:
            seen.add(canonical)
            result.append(canonical)
    return result, mappings


def process_file(path: Path, synonym_map: dict) -> bool:
    text = path.read_text(encoding="utf-8")
    # Extract frontmatter
    m = re.match(r"^---\n(.*?)\n---\n", text, re.DOTALL)
    if not m:
        return False

    fm_text = m.group(1)
    # Find tags line
    tags_match = re.search(r"^tags:\s*\[(.+?)\]", fm_text, re.MULTILINE)
    if not tags_match:
        return False

    raw_tags_str = tags_match.group(1)
    raw_tags = [t.strip().strip('"').strip("'") for t in raw_tags_str.split(",")]
    normalized, mappings = normalize_tags(raw_tags, synonym_map)

    if not mappings:
        return False

    new_tags_str = ", ".join(f'"{t}"' for t in normalized)
    new_fm_text = re.sub(
        r"^(tags:\s*\[).+?(\])",
        f"\\g<1>{new_tags_str}\\g<2>",
        fm_text,
        flags=re.MULTILINE,
    )
    new_text = text[: m.start(1)] + new_fm_text + text[m.end(1) :]

    print(f"  {path.name}: {mappings}")
    if not DRY_RUN:
        path.write_text(new_text, encoding="utf-8")
    return True


def main():
    if not ONTOLOGY_PATH.exists():
        print("tag-ontology.json not found — nothing to do.")
        return

    ontology = json.loads(ONTOLOGY_PATH.read_text(encoding="utf-8"))
    synonym_map = build_synonym_map(ontology)

    if not synonym_map:
        print("No synonyms defined in ontology — nothing to do.")
        return

    print(f"Synonym map ({len(synonym_map)} entries): {synonym_map}")
    print(f"{'DRY RUN — ' if DRY_RUN else ''}Scanning {CONCEPTS_DIR}...\n")

    changed = 0
    for md_file in sorted(CONCEPTS_DIR.glob("*.md")):
        if process_file(md_file, synonym_map):
            changed += 1

    print(f"\n{'Would update' if DRY_RUN else 'Updated'} {changed} file(s).")


if __name__ == "__main__":
    main()
