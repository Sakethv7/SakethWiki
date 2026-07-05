"""
Durable semantic memory index for SakethWiki.

Markdown remains the source of truth. This module builds a persistent SQLite
index over knowledge-bearing pages so retrieval can compound over time instead
of rebuilding context from raw files on every question.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Optional

import httpx

import identity
import llm_client
import vault_reader

_DEFAULT_VAULT = "/Users/sakethv7/SakethVault"
_INDEXED_FOLDERS = ("cs", "science", "humanities", "insights", "open-threads")
_CHUNK_CHAR_LIMIT = 900
_CHUNK_OVERLAP = 120


def _vault_path() -> Path:
    return Path(os.environ.get("VAULT_PATH", _DEFAULT_VAULT))


def _db_path() -> Path:
    return _vault_path() / "_wiki" / "meta" / "memory.db"


def _connect() -> sqlite3.Connection:
    db_path = _db_path()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def initialize() -> None:
    with _connect() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS pages (
                slug TEXT PRIMARY KEY,
                folder TEXT NOT NULL,
                title TEXT NOT NULL,
                tags_json TEXT NOT NULL DEFAULT '[]',
                current_understanding TEXT NOT NULL DEFAULT '',
                content_hash TEXT NOT NULL,
                source_path TEXT NOT NULL,
                indexed_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS chunks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                page_slug TEXT NOT NULL,
                chunk_index INTEGER NOT NULL,
                heading TEXT NOT NULL DEFAULT '',
                chunk_text TEXT NOT NULL,
                snippet TEXT NOT NULL,
                embedding_json TEXT,
                FOREIGN KEY(page_slug) REFERENCES pages(slug) ON DELETE CASCADE,
                UNIQUE(page_slug, chunk_index)
            );
            """
        )
        try:
            conn.execute(
                """
                CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts
                USING fts5(
                    page_slug UNINDEXED,
                    title,
                    tags,
                    heading,
                    chunk_text
                )
                """
            )
        except sqlite3.OperationalError:
            pass


def _iter_indexable_pages() -> list[dict]:
    vault = _vault_path()
    wiki_dir = vault / "_wiki"
    raw_pages: list[dict] = []
    for folder in _INDEXED_FOLDERS:
        folder_dir = wiki_dir / folder
        if not folder_dir.exists():
            continue
        for md_file in sorted(folder_dir.glob("*.md")):
            try:
                content = md_file.read_text(encoding="utf-8")
            except OSError:
                continue
            meta = vault_reader._parse_frontmatter(content)
            title = meta.get("title", md_file.stem)
            tags = meta.get("tags", [])
            if not isinstance(tags, list):
                tags = []
            raw_pages.append(
                {
                    "slug": md_file.stem,
                    "folder": folder,
                    "title": title,
                    "tags": tags,
                    "path": md_file,
                    "content": content,
                }
            )

    existing_slugs = {page["slug"] for page in raw_pages}
    pages: list[dict] = []
    for page in raw_pages:
        canonical = identity.resolve_slug(page["slug"])
        if canonical != page["slug"] and canonical in existing_slugs:
            continue
        pages.append(page)
    return pages


def _strip_frontmatter(content: str) -> str:
    if not content.startswith("---"):
        return content.strip()
    end = content.find("---", 3)
    if end == -1:
        return content.strip()
    return content[end + 3 :].strip()


def _page_current_understanding(slug: str) -> str:
    parsed = vault_reader.parse_concept_page(slug)
    if not parsed:
        return ""
    return (parsed.get("current_understanding") or "").strip()


def _chunk_text(text: str) -> list[str]:
    clean = " ".join((text or "").split())
    if not clean:
        return []
    if len(clean) <= _CHUNK_CHAR_LIMIT:
        return [clean]

    parts: list[str] = []
    start = 0
    while start < len(clean):
        end = min(len(clean), start + _CHUNK_CHAR_LIMIT)
        if end < len(clean):
            split_at = clean.rfind(". ", start, end)
            if split_at <= start + 200:
                split_at = clean.rfind(" ", start, end)
            if split_at > start:
                end = split_at + 1
        parts.append(clean[start:end].strip())
        if end >= len(clean):
            break
        start = max(end - _CHUNK_OVERLAP, start + 1)
    return [p for p in parts if p]


def _page_chunks(page: dict) -> list[dict]:
    slug = page["slug"]
    title = page["title"]
    tags = page["tags"]
    raw_body = _strip_frontmatter(page["content"])
    parsed = vault_reader.parse_concept_page(slug)

    sections: list[tuple[str, str]] = []
    current_understanding = ""
    if parsed:
        current_understanding = (parsed.get("current_understanding") or "").strip()
        overview_parts = [f"Title: {title}"]
        if tags:
            overview_parts.append("Tags: " + ", ".join(tags))
        if current_understanding:
            overview_parts.append("Current understanding: " + current_understanding)
        if parsed.get("evolution_note"):
            overview_parts.append("Evolution note: " + parsed["evolution_note"])
        sections.append(("overview", "\n".join(overview_parts)))

        for section in parsed.get("sections", []):
            section_parts = []
            section_title = section.get("title") or "source"
            if section_title:
                section_parts.append(f"Section: {section_title}")
            if section.get("key_insight"):
                section_parts.append("Key insight: " + section["key_insight"])
            if section.get("bullets"):
                section_parts.append("Bullets: " + " ".join(section["bullets"]))
            if section.get("related"):
                section_parts.append("Related: " + ", ".join(section["related"]))
            if section.get("superseded_reason"):
                section_parts.append("Superseded note: " + section["superseded_reason"])
            joined = "\n".join(p for p in section_parts if p)
            if joined:
                sections.append((section_title, joined))

    if not sections:
        heading = "page"
        sections = [(heading, f"Title: {title}\n{raw_body}")]

    chunks: list[dict] = []
    chunk_index = 0
    for heading, text in sections:
        for part in _chunk_text(text):
            chunks.append(
                {
                    "chunk_index": chunk_index,
                    "heading": heading,
                    "text": part,
                    "snippet": part[:280],
                    "current_understanding": current_understanding,
                }
            )
            chunk_index += 1
    return chunks


def _embedding_request_config() -> Optional[dict]:
    enabled = os.environ.get("EMBED_ENABLED", "").strip().lower()
    if enabled not in {"1", "true", "yes", "on"}:
        return None

    cfg = llm_client.get_embedding_config()
    provider = cfg.get("provider", "openai")
    if provider != "openai":
        return None

    api_key = cfg.get("api_key") or llm_client.get_openai_api_key()
    if not api_key:
        return None

    base_url = os.environ.get("OPENAI_BASE_URL", "").strip() or os.environ.get("OPENAI_COMPAT_BASE_URL", "").strip()
    if not base_url:
        base_url = "https://api.openai.com/v1"

    return {
        "api_key": api_key,
        "base_url": base_url.rstrip("/"),
        "model": cfg.get("model", "text-embedding-3-small"),
        "batch_size": int(cfg.get("batch_size", 64)),
    }


def _embed_texts(texts: list[str]) -> list[Optional[list[float]]]:
    request_cfg = _embedding_request_config()
    if not request_cfg or not texts:
        return [None for _ in texts]

    out: list[Optional[list[float]]] = []
    batch_size = max(1, request_cfg["batch_size"])
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {request_cfg['api_key']}",
    }
    url = f"{request_cfg['base_url']}/embeddings"

    with httpx.Client(timeout=60.0) as client:
        for i in range(0, len(texts), batch_size):
            batch = texts[i : i + batch_size]
            resp = client.post(
                url,
                headers=headers,
                json={"model": request_cfg["model"], "input": batch},
            )
            resp.raise_for_status()
            data = resp.json().get("data", [])
            vectors = {int(item["index"]): item.get("embedding") for item in data}
            for local_idx in range(len(batch)):
                out.append(vectors.get(local_idx))
    return out


def _hash_content(content: str) -> str:
    return hashlib.sha1(content.encode("utf-8")).hexdigest()


def index_page(slug: str, *, conn: Optional[sqlite3.Connection] = None, page: Optional[dict] = None) -> dict:
    if page is None:
        pages = {item["slug"]: item for item in _iter_indexable_pages()}
        page = pages.get(slug)
    if not page:
        remove_page(slug, conn=conn)
        return {"slug": slug, "status": "removed"}

    content_hash = _hash_content(page["content"])
    owns_conn = conn is None
    conn = conn or _connect()
    try:
        existing = conn.execute(
            "SELECT content_hash FROM pages WHERE slug = ?",
            (slug,),
        ).fetchone()
        if existing and existing["content_hash"] == content_hash:
            return {"slug": slug, "status": "unchanged"}

        current_understanding = _page_current_understanding(slug)
        chunks = _page_chunks(page)
        embeddings = _embed_texts([chunk["text"] for chunk in chunks])

        conn.execute("DELETE FROM chunks WHERE page_slug = ?", (slug,))
        try:
            conn.execute("DELETE FROM chunks_fts WHERE page_slug = ?", (slug,))
        except sqlite3.OperationalError:
            pass

        conn.execute(
            """
            INSERT INTO pages (slug, folder, title, tags_json, current_understanding, content_hash, source_path, indexed_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(slug) DO UPDATE SET
                folder = excluded.folder,
                title = excluded.title,
                tags_json = excluded.tags_json,
                current_understanding = excluded.current_understanding,
                content_hash = excluded.content_hash,
                source_path = excluded.source_path,
                indexed_at = excluded.indexed_at
            """,
            (
                slug,
                page["folder"],
                page["title"],
                json.dumps(page["tags"]),
                current_understanding,
                content_hash,
                str(page["path"]),
                datetime.now().isoformat(),
            ),
        )

        for chunk, embedding in zip(chunks, embeddings):
            cursor = conn.execute(
                """
                INSERT INTO chunks (page_slug, chunk_index, heading, chunk_text, snippet, embedding_json)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    slug,
                    chunk["chunk_index"],
                    chunk["heading"],
                    chunk["text"],
                    chunk["snippet"],
                    json.dumps(embedding) if embedding is not None else None,
                ),
            )
            chunk_id = cursor.lastrowid
            try:
                conn.execute(
                    """
                    INSERT INTO chunks_fts (rowid, page_slug, title, tags, heading, chunk_text)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        chunk_id,
                        slug,
                        page["title"],
                        " ".join(page["tags"]),
                        chunk["heading"],
                        chunk["text"],
                    ),
                )
            except sqlite3.OperationalError:
                pass

        if owns_conn:
            conn.commit()
        return {"slug": slug, "status": "indexed", "chunks": len(chunks)}
    finally:
        if owns_conn:
            conn.close()


def remove_page(slug: str, *, conn: Optional[sqlite3.Connection] = None) -> None:
    owns_conn = conn is None
    conn = conn or _connect()
    try:
        conn.execute("DELETE FROM pages WHERE slug = ?", (slug,))
        try:
            conn.execute("DELETE FROM chunks_fts WHERE page_slug = ?", (slug,))
        except sqlite3.OperationalError:
            pass
        if owns_conn:
            conn.commit()
    finally:
        if owns_conn:
            conn.close()


def sync_index() -> dict:
    initialize()
    pages = _iter_indexable_pages()
    current_slugs = {page["slug"] for page in pages}
    indexed = 0
    unchanged = 0
    removed = 0

    with _connect() as conn:
        existing_slugs = {
            row["slug"] for row in conn.execute("SELECT slug FROM pages").fetchall()
        }
        for stale_slug in sorted(existing_slugs - current_slugs):
            remove_page(stale_slug, conn=conn)
            removed += 1

        for page in pages:
            result = index_page(page["slug"], conn=conn, page=page)
            if result["status"] == "indexed":
                indexed += 1
            elif result["status"] == "unchanged":
                unchanged += 1
        conn.commit()

    return {
        "indexed": indexed,
        "unchanged": unchanged,
        "removed": removed,
        "pages_seen": len(pages),
    }


def _fts_query(query: str) -> str:
    terms = [term for term in "".join(c if c.isalnum() else " " for c in query.lower()).split() if len(term) > 1]
    if not terms:
        return ""
    return " OR ".join(dict.fromkeys(terms))


def _lexical_hits(conn: sqlite3.Connection, query: str, limit: int = 40) -> list[dict]:
    fts_query = _fts_query(query)
    hits: list[dict] = []
    if fts_query:
        try:
            rows = conn.execute(
                """
                SELECT
                    c.id,
                    c.page_slug,
                    c.heading,
                    c.snippet,
                    p.folder,
                    p.title,
                    p.current_understanding,
                    bm25(chunks_fts, 8.0, 3.0, 2.0, 1.0) AS rank
                FROM chunks_fts
                JOIN chunks c ON c.id = chunks_fts.rowid
                JOIN pages p ON p.slug = c.page_slug
                WHERE chunks_fts MATCH ?
                ORDER BY rank
                LIMIT ?
                """,
                (fts_query, limit),
            ).fetchall()
            for row in rows:
                hits.append(
                    {
                        "chunk_id": row["id"],
                        "page_slug": row["page_slug"],
                        "folder": row["folder"],
                        "title": row["title"],
                        "heading": row["heading"],
                        "snippet": row["snippet"],
                        "current_understanding": row["current_understanding"],
                        "score": 1.0 / (1.0 + max(0.0, float(row["rank"]))),
                    }
                )
            if hits:
                return hits
        except sqlite3.OperationalError:
            pass

    normalized_terms = [t for t in query.lower().split() if len(t) > 1]
    rows = conn.execute(
        """
        SELECT c.id, c.page_slug, c.heading, c.snippet, c.chunk_text, p.folder, p.title, p.current_understanding
        FROM chunks c
        JOIN pages p ON p.slug = c.page_slug
        """
    ).fetchall()
    for row in rows:
        haystack = f"{row['title']} {row['heading']} {row['chunk_text']}".lower()
        score = 0.0
        for term in normalized_terms:
            if term in haystack:
                score += 1.0 + haystack.count(term) * 0.2
        if score > 0:
            hits.append(
                {
                    "chunk_id": row["id"],
                    "page_slug": row["page_slug"],
                    "folder": row["folder"],
                    "title": row["title"],
                    "heading": row["heading"],
                    "snippet": row["snippet"],
                    "current_understanding": row["current_understanding"],
                    "score": score,
                }
            )
    hits.sort(key=lambda item: item["score"], reverse=True)
    return hits[:limit]


def _cosine_similarity(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


def _vector_hits(conn: sqlite3.Connection, query: str, limit: int = 40) -> list[dict]:
    query_embedding = _embed_texts([query])[0]
    if not query_embedding:
        return []

    rows = conn.execute(
        """
        SELECT c.id, c.page_slug, c.heading, c.snippet, c.embedding_json, p.folder, p.title, p.current_understanding
        FROM chunks c
        JOIN pages p ON p.slug = c.page_slug
        WHERE c.embedding_json IS NOT NULL
        """
    ).fetchall()

    hits: list[dict] = []
    for row in rows:
        try:
            embedding = json.loads(row["embedding_json"])
        except (TypeError, json.JSONDecodeError):
            continue
        score = _cosine_similarity(query_embedding, embedding)
        if score <= 0:
            continue
        hits.append(
            {
                "chunk_id": row["id"],
                "page_slug": row["page_slug"],
                "folder": row["folder"],
                "title": row["title"],
                "heading": row["heading"],
                "snippet": row["snippet"],
                "current_understanding": row["current_understanding"],
                "score": score,
            }
        )
    hits.sort(key=lambda item: item["score"], reverse=True)
    return hits[:limit]


def search(query: str, limit: int = 5) -> list[dict]:
    if not query or not query.strip():
        return []

    sync_index()
    query = identity.expand_query(query)
    with _connect() as conn:
        lexical = _lexical_hits(conn, query, limit=max(20, limit * 6))
        vector = _vector_hits(conn, query, limit=max(20, limit * 6))

    chunk_scores: dict[int, dict] = {}
    max_lex = max((hit["score"] for hit in lexical), default=0.0)
    max_vec = max((hit["score"] for hit in vector), default=0.0)

    for hit in lexical:
        entry = chunk_scores.setdefault(hit["chunk_id"], {**hit, "lex": 0.0, "vec": 0.0})
        entry["lex"] = max(entry["lex"], hit["score"] / max_lex if max_lex else 0.0)
    for hit in vector:
        entry = chunk_scores.setdefault(hit["chunk_id"], {**hit, "lex": 0.0, "vec": 0.0})
        entry["vec"] = max(entry["vec"], hit["score"] / max_vec if max_vec else 0.0)

    page_scores: dict[str, dict] = {}
    for entry in chunk_scores.values():
        combined = 0.65 * entry["lex"] + 0.75 * entry["vec"]
        if entry["lex"] > 0 and entry["vec"] == 0:
            combined = entry["lex"]
        if entry["vec"] > 0 and entry["lex"] == 0:
            combined = entry["vec"]
        page = page_scores.setdefault(
            entry["page_slug"],
            {
                "page_name": entry["page_slug"],
                "folder": entry["folder"],
                "title": entry["title"],
                "current_understanding": entry["current_understanding"],
                "score": 0.0,
                "snippets": [],
                "headings": [],
            },
        )
        page["score"] += combined
        if entry["snippet"] not in page["snippets"] and len(page["snippets"]) < 3:
            page["snippets"].append(entry["snippet"])
        if entry["heading"] and entry["heading"] not in page["headings"] and len(page["headings"]) < 3:
            page["headings"].append(entry["heading"])

    ranked = sorted(page_scores.values(), key=lambda item: item["score"], reverse=True)
    return ranked[:limit]


def status() -> dict:
    initialize()
    with _connect() as conn:
        page_count = conn.execute("SELECT COUNT(*) AS c FROM pages").fetchone()["c"]
        chunk_count = conn.execute("SELECT COUNT(*) AS c FROM chunks").fetchone()["c"]
    return {
        "db_path": str(_db_path()),
        "pages_indexed": int(page_count),
        "chunks_indexed": int(chunk_count),
        "embeddings_enabled": _embedding_request_config() is not None,
        "indexed_folders": list(_INDEXED_FOLDERS),
    }
