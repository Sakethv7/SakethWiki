"""
Reads vault content for the /chat and /pages endpoints.
No embeddings — keyword match + Claude decides which pages to read.
"""
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Optional

_DEFAULT_VAULT = "/Users/sakethv7/SakethVault"

def _vault() -> Path:
    """Read VAULT_PATH fresh every call — never cached at import time."""
    return Path(os.environ.get("VAULT_PATH", _DEFAULT_VAULT))

# Keep these as properties for backwards compat with code that imports them directly
@property
def VAULT_PATH(): return _vault()  # noqa — module-level property not supported; use _vault()

WIKI_DIR     = property(lambda: _vault() / "_wiki")   # not usable as module attr
CONCEPTS_DIR = property(lambda: _vault() / "_wiki" / "concepts")
INDEX_PATH   = property(lambda: _vault() / "_wiki" / "index.md")

def _dirs():
    wiki = _vault() / "_wiki"
    return {
        "concepts":     wiki / "concepts",     # legacy — kept for backward compat
        "cs":           wiki / "cs",
        "humanities":   wiki / "humanities",
        "science":      wiki / "science",
        "sources":      wiki / "sources",
        "insights":     wiki / "insights",
        "open-threads": wiki / "open-threads",
        "meta":         wiki / "meta",
    }

def _concept_dirs() -> list:
    """All dirs that hold concept pages (legacy + domain subfolders)."""
    wiki = _vault() / "_wiki"
    return [
        wiki / "concepts",
        wiki / "cs",
        wiki / "humanities",
        wiki / "science",
    ]

# Keep VAULT_PATH/WIKI_DIR/CONCEPTS_DIR/INDEX_PATH usable as simple names
VAULT_PATH   = _vault()
WIKI_DIR     = VAULT_PATH / "_wiki"
CONCEPTS_DIR = WIKI_DIR / "concepts"
INDEX_PATH   = WIKI_DIR / "index.md"


def list_pages_in_folder(folder: str = "concepts") -> list[dict]:
    """Return metadata for all pages in a given folder."""
    vault = _vault()
    dirs = _dirs()
    folder_dir = dirs.get(folder)
    if folder_dir is None:
        return []
    if not folder_dir.exists():
        return []
    pages = []
    for md_file in sorted(folder_dir.glob("*.md")):
        try:
            content = md_file.read_text(encoding="utf-8")
        except OSError:
            continue
        meta = _parse_frontmatter(content)
        word_count = len(content.split())
        pages.append({
            "name": md_file.stem,
            "folder": folder,
            "file": str(md_file.relative_to(vault)),
            "title": meta.get("title", md_file.stem),
            "tags": meta.get("tags", []),
            "last_updated": meta.get("last_updated", ""),
            "entry_count": int(meta.get("entry_count", 1)),
            "word_count": word_count,
        })
    return pages


def list_concept_pages() -> list[dict]:
    """Return metadata for all concept pages across all domain folders."""
    vault = _vault()
    pages = []
    seen: set[str] = set()
    for concept_dir in _concept_dirs():
        if not concept_dir.exists():
            continue
        folder_name = concept_dir.name
        for md_file in sorted(concept_dir.glob("*.md")):
            if md_file.stem in seen:
                continue
            seen.add(md_file.stem)
            try:
                content = md_file.read_text(encoding="utf-8")
            except OSError:
                continue
            meta = _parse_frontmatter(content)
            pages.append({
                "name": md_file.stem,
                "folder": folder_name,
                "file": str(md_file.relative_to(vault)),
                "title": meta.get("title", md_file.stem),
                "tags": meta.get("tags", []),
                "last_updated": meta.get("last_updated", ""),
                "entry_count": int(meta.get("entry_count", 1)),
                "word_count": len(content.split()),
            })
    return pages


def build_backlinks_index() -> dict[str, list[str]]:
    """
    Scan all concept pages for [[wikilinks]] and build a reverse index.
    Returns {page_name: [list of page_names that link TO it]}.
    """
    # First pass: collect all outbound links per page
    outbound: dict[str, list[str]] = {}
    for concept_dir in _concept_dirs():
        if not concept_dir.exists():
            continue
        for md_file in concept_dir.glob("*.md"):
            try:
                content = md_file.read_text(encoding="utf-8")
            except OSError:
                continue
            links = re.findall(r"\[\[([^\]]+)\]\]", content)
            outbound[md_file.stem] = [l.lower().replace(" ", "-") for l in links]

    # Second pass: invert to backlinks
    backlinks: dict[str, list[str]] = {}
    for source, targets in outbound.items():
        for target in targets:
            backlinks.setdefault(target, [])
            if source not in backlinks[target]:
                backlinks[target].append(source)

    return backlinks


def get_review_queue(days: int = 30) -> list[dict]:
    """
    Return concept pages not updated in more than `days` days,
    sorted by most stale first. Zero LLM — pure date math.
    """
    from datetime import date, timedelta
    cutoff = date.today() - timedelta(days=days)
    pages = list_concept_pages()
    stale = []
    for p in pages:
        last = p.get("last_updated", "")
        if not last:
            stale.append({**p, "days_since_update": 9999})
            continue
        try:
            last_date = date.fromisoformat(last)
            delta = (date.today() - last_date).days
            if delta >= days:
                stale.append({**p, "days_since_update": delta})
        except ValueError:
            stale.append({**p, "days_since_update": 9999})
    stale.sort(key=lambda x: x["days_since_update"], reverse=True)
    return stale


def build_graph() -> dict:
    """
    Build a knowledge graph: nodes are concept pages, edges are wikilinks.
    Returns {nodes: [{id, title, tags, entry_count}], edges: [{source, target}]}.
    Edges are deduplicated and bidirectional (A→B and B→A both appear if both link).
    """
    pages = list_concept_pages()
    if not pages:
        return {"nodes": [], "edges": []}

    page_set = {p["name"] for p in pages}

    nodes = []
    for p in pages:
        nodes.append({
            "id": p["name"],
            "title": p["title"],
            "tags": p["tags"],
            "entry_count": p["entry_count"],
            "last_updated": p["last_updated"],
            "domain": p["folder"],
        })

    # Collect edges from wikilinks across all concept dirs
    edge_set: set[tuple] = set()
    for concept_dir in _concept_dirs():
        if not concept_dir.exists():
            continue
        for md_file in concept_dir.glob("*.md"):
            try:
                content = md_file.read_text(encoding="utf-8")
            except OSError:
                continue
            source = md_file.stem
            links = re.findall(r"\[\[([^\]]+)\]\]", content)
            for link in links:
                target = link.lower().replace(" ", "-")
                if target in page_set and target != source:
                    edge_set.add(tuple(sorted([source, target])))

    edges = [{"source": s, "target": t} for s, t in edge_set]
    return {"nodes": nodes, "edges": edges}


def read_page(page_name: str) -> Optional[str]:
    """Return full content of a page, searching all vault folders."""
    for folder_dir in _dirs().values():
        if not folder_dir.exists():
            continue
        for md_file in folder_dir.glob("*.md"):
            if md_file.stem.lower() == page_name.lower():
                return md_file.read_text(encoding="utf-8")
    return None


def parse_concept_page(page_name: str) -> Optional[dict]:
    """
    Parse a concept page into structured data for rich rendering.
    Returns dict with: meta, current_understanding, sections, diagrams.
    Returns None if page not found.
    """
    content = read_page(page_name)
    if not content:
        return None

    meta = _parse_frontmatter(content)
    body = content[content.find("---", 3) + 3:].strip() if content.startswith("---") else content

    # Extract current understanding block
    current_understanding = ""
    evolution_badge = "🔵"
    evolution_note = ""
    uo_match = re.search(
        r"> \*\*Current understanding\*\*\s*([🔵🟡🟠🔴⚪]?)\s*\n((?:>.*\n?)*)",
        body
    )
    if uo_match:
        evolution_badge = uo_match.group(1).strip() or "🔵"
        block_lines = [ln[1:].strip() for ln in uo_match.group(2).splitlines() if ln.startswith(">")]
        # Separate understanding text from evolution note (italic line)
        understanding_lines = [ln for ln in block_lines if not ln.startswith("*—")]
        note_lines = [ln.strip("*— ").strip() for ln in block_lines if ln.startswith("*—")]
        current_understanding = " ".join(understanding_lines).strip()
        evolution_note = note_lines[0] if note_lines else ""

    # Parse ## sections (source entries)
    sections = []
    raw_sections = re.split(r"\n(?=## )", body)
    for sec in raw_sections[1:]:  # skip header part
        lines = sec.strip().splitlines()
        if not lines:
            continue
        header = lines[0]  # ## [Title](url) · date  OR  ## Title · date

        # Check if superseded
        superseded = False
        superseded_reason = ""
        sup_match = re.search(r"\[!warning\]\s*Superseded.*?\n>(.*?)(?=\n\n|---|\Z)", sec, re.DOTALL)
        if sup_match:
            superseded = True
            superseded_reason = sup_match.group(1).replace(">", "").strip()

        # Extract title + url + date from header
        title, url, date = "", "", ""
        linked = re.match(r"##\s+\[([^\]]+)\]\(([^)]+)\)\s*·\s*(\d{4}-\d{2}-\d{2})", header)
        plain  = re.match(r"##\s+(.+?)\s*·\s*(\d{4}-\d{2}-\d{2})", header)
        if linked:
            title, url, date = linked.group(1), linked.group(2), linked.group(3)
        elif plain:
            title, date = plain.group(1), plain.group(2)

        # Extract bullets
        bullets = re.findall(r"^- (.+)$", sec, re.MULTILINE)

        # Extract key insight
        ki_match = re.search(r"\*\*Key insight:\*\*\s*(.+)", sec)
        key_insight = ki_match.group(1).strip() if ki_match else ""

        # Extract related wikilinks
        related = re.findall(r"\[\[([^\]]+)\]\]", sec)

        # Extract mermaid diagram
        diagram = ""
        diag_match = re.search(r"```mermaid\n([\s\S]+?)```", sec)
        if diag_match:
            diagram = diag_match.group(1).strip()

        if title or bullets:
            sections.append({
                "title": title,
                "url": url,
                "date": date,
                "bullets": bullets,
                "key_insight": key_insight,
                "related": related,
                "diagram": diagram,
                "superseded": superseded,
                "superseded_reason": superseded_reason,
            })

    # Collect all diagrams across the page
    all_diagrams = [s["diagram"] for s in sections if s.get("diagram")]

    # Fallback: synthesise understanding from key insights if no block yet
    if not current_understanding and sections:
        insights = [s["key_insight"] for s in sections if s.get("key_insight")]
        if insights:
            current_understanding = insights[-1]  # most recent is most evolved

    return {
        "name": page_name,
        "title": meta.get("title", page_name.replace("-", " ").title()),
        "tags": meta.get("tags", []) if isinstance(meta.get("tags"), list) else [],
        "understanding_version": int(meta.get("understanding_version", 1)),
        "understanding_maturity": int(meta.get("understanding_maturity", 0)) if meta.get("understanding_maturity") else None,
        "last_evolution": meta.get("last_evolution", ""),
        "last_updated": meta.get("last_updated", ""),
        "entry_count": int(meta.get("entry_count", len(sections))),
        "current_understanding": current_understanding,
        "evolution_badge": evolution_badge,
        "evolution_note": evolution_note,
        "sections": sections,
        "diagrams": all_diagrams,
    }


def read_index() -> str:
    try:
        index = _vault() / "_wiki" / "index.md"
        if index.exists():
            return index.read_text(encoding="utf-8")
    except Exception:
        pass
    return ""


def find_relevant_pages(query: str, max_pages: int = 5) -> list[str]:
    """
    Keyword-match the query against concept page names and content.
    Scoring: page-name match >> tag match >> content frequency.
    Also expands common synonyms so "transformers" finds "attention" pages etc.
    Returns list of page names most likely relevant.
    """
    # Synonym expansion — cross-domain term aliases
    SYNONYMS: dict = {
        # AI / ML
        "transformer": ["attention", "self-attention"],
        "transformers": ["attention", "self-attention"],
        "llm": ["language model", "gpt"],
        "llms": ["language model", "gpt"],
        "vector": ["embedding", "embeddings"],
        "vectors": ["embedding", "embeddings"],
        "rag": ["retrieval", "retrieval-augmented"],
        "fine-tuning": ["finetuning", "finetune", "lora"],
        "finetuning": ["fine-tuning", "lora"],
        "inference": ["serving", "deployment"],
        "serving": ["inference", "deployment"],
        "quantization": ["quant", "compression"],
        "quant": ["quantization", "compression"],
        "kv-cache": ["kv cache", "key-value cache", "attention cache"],
        "agent": ["agents", "agentic", "tool-use"],
        "agents": ["agent", "agentic", "tool-use"],
        "embedding": ["vector", "embeddings", "semantic search"],
        "embeddings": ["embedding", "vector"],
        # DSA
        "graph": ["graphs", "bfs", "dfs", "shortest path"],
        "graphs": ["graph", "bfs", "dfs"],
        "dp": ["dynamic programming", "memoization", "tabulation"],
        "dynamic programming": ["dp", "memoization", "recursion"],
        "tree": ["trees", "bst", "binary tree", "trie"],
        "trees": ["tree", "bst", "binary tree"],
        "heap": ["heaps", "priority queue"],
        "heaps": ["heap", "priority queue"],
        "sorting": ["sort", "quicksort", "mergesort"],
        "binary search": ["binarysearch", "bisect"],
        # Humanities
        "geopolitics": ["geopolitical", "international relations", "ir"],
        "history": ["historical", "ancient", "medieval", "modern"],
        "politics": ["political", "government", "policy"],
        # Science
        "calculus": ["derivative", "integral", "differentiation"],
        "probability": ["statistics", "bayesian", "distribution"],
        "linear algebra": ["matrix", "vectors", "eigenvalues"],
    }

    raw_terms = set(re.sub(r"[^\w\s-]", "", query.lower()).split())
    expanded_terms = set(raw_terms)
    for term in raw_terms:
        expanded_terms.update(SYNONYMS.get(term, []))

    scored: list[tuple[float, str]] = []
    seen: set[str] = set()

    for concept_dir in _concept_dirs():
        if not concept_dir.exists():
            continue
        for md_file in concept_dir.glob("*.md"):
            if md_file.stem in seen:
                continue
            seen.add(md_file.stem)
        content = md_file.read_text(encoding="utf-8")
        content_lower = content.lower()
        name_lower = md_file.stem.lower()  # e.g. "attention-residuals"
        name_words = set(name_lower.replace("-", " ").split())

        # Parse tags from frontmatter for tag-match scoring
        meta = _parse_frontmatter(content)
        tags_lower = " ".join(t.lower() for t in meta.get("tags", []))

        score: float = 0
        for term in expanded_terms:
            # Strongest signal: term matches page slug words
            if term in name_lower or term in name_words:
                score += 15
            # Strong: term matches a tag
            if term in tags_lower:
                score += 8
            # Medium: term appears in content (TF-style, diminishing returns)
            count = content_lower.count(term)
            if count > 0:
                score += min(count, 10)  # cap at 10 to avoid keyword stuffing

        if score > 0:
            scored.append((score, md_file.stem))

    scored.sort(reverse=True)
    return [name for _, name in scored[:max_pages]]


def read_pages_content(page_names: list[str]) -> dict[str, str]:
    """Return {page_name: content} for a list of page names."""
    result = {}
    for name in page_names:
        content = read_page(name)
        if content:
            result[name] = content
    return result


# ── helpers ──────────────────────────────────────────────────────────────────

def _parse_frontmatter(content: str) -> dict:
    """Extract YAML frontmatter fields as a dict (simple regex, no YAML dep)."""
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
        # Parse list fields like tags: [RAG, Agents]
        if val.startswith("[") and val.endswith("]"):
            val = [v.strip().strip('"').strip("'") for v in val[1:-1].split(",") if v.strip()]
        meta[key] = val
    return meta
