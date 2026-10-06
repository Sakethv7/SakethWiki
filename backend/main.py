"""
SakethWiki FastAPI backend.

POST /ingest      — fetch URL / accept text/image, extract via routed LLM, stage to queue
GET  /queue       — list pending HITL items
POST /approve/{id} — approve or reject a queued item
POST /chat        — RAG chat over the SQLite memory index (keyword-scan fallback)
GET  /revision/today, POST /revision/rate — daily recall practice
GET  /pages       — list all concept pages with metadata
GET  /page/{name} — full content of a concept page
"""
import base64
import hashlib
import json
import logging
import os
import random
import re as _re
import shutil
import time
import uuid
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional

logger = logging.getLogger("sakethwiki")

# Auto-load .env from project root (one level up from backend/)
_env_path = Path(__file__).parent.parent / ".env"
if _env_path.exists():
    for _line in _env_path.read_text().splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _, _v = _line.partition("=")
            _k, _v = _k.strip(), _v.strip()
            if _v:  # force-set if .env has a non-empty value (override empty env vars)
                os.environ[_k] = _v

import httpx
from bs4 import BeautifulSoup
from fastapi import APIRouter, FastAPI, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

import active_review
import revision
import consolidation
import identity
import llm_client
import memory_store
import preference_memory
import queue_manager
import system_loop
import tag_classifier
import telemetry
import vault_reader
import wiki_writer
import eval_harness

# ── app setup ────────────────────────────────────────────────────────────────

import asyncio
from contextlib import asynccontextmanager

@asynccontextmanager
async def lifespan(app):
    """Start background tasks on app startup."""
    memory_store.initialize()
    _schedule_memory_sync()  # refresh the index once, off the request path
    asyncio.create_task(_weekly_analysis_scheduler())
    # Start folder watcher for screenshot inbox
    import image_watcher
    image_watcher.start()
    yield


def _schedule_memory_sync() -> None:
    """Refresh the SQLite memory index in the background.

    Retrieval (`memory_store.search`) no longer syncs on every query. Instead we
    sync on write events — startup, /approve, /edit-page — and via the manual
    POST /memory/reindex. Fire-and-forget so it never delays a response.
    """
    async def _run():
        try:
            loop = asyncio.get_event_loop()
            result = await loop.run_in_executor(None, memory_store.sync_index)
            logger.info(
                "memory index synced: indexed=%s unchanged=%s removed=%s",
                result.get("indexed"), result.get("unchanged"), result.get("removed"),
            )
        except Exception as e:
            logger.warning("background memory sync failed: %s", e)

    try:
        asyncio.get_event_loop().create_task(_run())
    except RuntimeError:
        # No running loop (e.g. called from a sync context in tests) — skip.
        pass

app = FastAPI(title="SakethWiki API", version="1.0.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],   # LAN access needed for mobile upload page
    allow_methods=["GET", "POST", "DELETE"],
    allow_headers=["Content-Type"],
)

# ── tunable constants ─────────────────────────────────────────────────────────

URL_SCRAPE_CHAR_LIMIT   = 5000   # max chars kept from fetched URL body
URL_SCRAPE_LINK_LIMIT   = 20     # max external links scraped per page
RAG_TOP_K               = int(os.environ.get("RAG_TOP_K", 5))          # top-k pages for chat context
RAG_CONTEXT_BUDGET      = int(os.environ.get("RAG_CONTEXT_BUDGET", 4000))  # chars of vault context injected into chat
SELF_LEARN_TRACE_WINDOW = 100    # last N traces sent to Sonnet for weekly analysis
LINT_CACHE_TTL_SECONDS  = 86400  # 24 h — lint report cache validity
WEEKLY_ANALYSIS_INTERVAL_SECONDS = 3600  # scheduler checks every hour
ANALYSIS_RETRY_BACKOFF_SECONDS = 86400   # after an attempt (pass or fail), wait a day before retrying
# The merge draft is capped at max_tokens=3000 (~12K chars). Larger combined
# inputs would come back cut off, so /consolidate refuses to draft them.
CONSOLIDATE_MAX_INPUT_CHARS = 12000
SOURCE_VERDICTS = {"ingest", "source_only", "reject"}
KNOWLEDGE_SHAPES = {"taxonomy", "mechanism", "architecture", "argument", "case_study", "none"}
CHAT_NOTE_TYPES = {"correction", "contradiction", "example", "nuance"}

# Operations subsystem (system loop, eval harness, trace critic, action
# candidates, the Operations tab). Paused by default — set ENABLE_OPS=true to
# register its routes and show the tab. The knowledge feedback loop
# (/analyze-traces, weekly scheduler, system-insights) runs regardless.
ENABLE_OPS = os.environ.get("ENABLE_OPS", "false").strip().lower() in {"1", "true", "yes", "on"}

# ─────────────────────────────────────────────────────────────────────────────

def _chat_context_budget() -> int:
    try:
        settings = system_loop.load_runtime_settings().get("settings", {})
        value = int(settings.get("chat_context_budget") or 0)
        if value >= 1000:
            return value
    except Exception:
        pass
    try:
        return int(os.environ.get("RAG_CONTEXT_BUDGET", RAG_CONTEXT_BUDGET))
    except ValueError:
        return RAG_CONTEXT_BUDGET


def _ingest_source_budget(default_budget: int) -> int:
    try:
        settings = system_loop.load_runtime_settings().get("settings", {})
        value = int(settings.get("ingest_source_budget") or 0)
        if 4000 <= value <= 80000:
            return max(default_budget, value)
    except Exception:
        pass
    return default_budget


VALID_TAGS = [
    # AI / ML
    "RAG", "Agents", "Serving", "MLOps", "LLM", "Inference",
    "VectorDB", "Attention", "KVCache", "Quantization",
    "FineTuning", "Embeddings", "Agentic",
    # DSA
    "Graphs", "Trees", "DynamicProgramming", "Heaps", "BinarySearch",
    "Sorting", "Arrays", "LinkedLists", "HashMaps", "Recursion",
    # System Design
    "Databases", "Caching", "LoadBalancing", "APIs", "Queues",
    "Microservices", "Networking", "Distributed",
    # Tech / Engineering
    "Engineering", "Systems", "DevTools", "Product", "Security",
    # Humanities
    "History", "Geopolitics", "Politics", "Geography", "Philosophy",
    "Culture", "IR",
    # Science
    "Physics", "Math", "Chemistry", "Biology", "Statistics",
    # Finance / Business
    "Finance", "Investing", "Business", "Startups", "Economics",
    # Meta
    "Productivity", "Learning", "Health", "Mental-models", "Career",
]


# ── request/response models ──────────────────────────────────────────────────

class IngestRequest(BaseModel):
    url: Optional[str] = None
    text: Optional[str] = None
    image_base64: Optional[str] = None        # legacy single image
    images: Optional[list] = None             # [{data: b64, mediaType: "image/png"}, ...]
    user_notes: Optional[str] = None          # user's own understanding (shapes extraction)
    source_type: Optional[str] = None         # "lecture" | "url" | "text" | "screenshot"
    force: bool = False


class MarkdownIngestRequest(BaseModel):
    markdown: str
    source_url: Optional[str] = None
    clip_title: Optional[str] = None
    force: bool = False


class InboxProcessRequest(BaseModel):
    limit: int = 20
    force: bool = False


class RegenerateRequest(BaseModel):
    mode: str = "full"  # full | diagram


class DiagramRegenerationRequest(BaseModel):
    expected_revision: int
    idempotency_key: str
    intent: str = "faithful_source"
    feedback: str = ""


class DiagramSelectionRequest(BaseModel):
    expected_revision: int
    candidate_id: str


class ApproveRequest(BaseModel):
    approved: bool
    redirect_note: Optional[str] = None
    # Optional human edits — overrides the extracted values before vault write
    edits: Optional[dict] = None
    open_thread: bool = False  # if True, add deep-dive tag to the concept page


class BatchDecisionRequest(BaseModel):
    item_ids: list[str]
    approved: bool


class OpenThreadRequest(BaseModel):
    title: str
    notes: str = ""  # free-form "what I want to learn"
    tags: list = []


class ChatRequest(BaseModel):
    message: str
    history: list = []


class ChatNoteRequest(BaseModel):
    note_type: str
    note: str
    question: str = ""
    answer_excerpt: str = ""
    pages_read: list = []
    sources: list = []


class PreferenceReviewRequest(BaseModel):
    kind: str
    key: str
    value: str = ""
    status: str


class SaveAnswerRequest(BaseModel):
    question: str
    answer: str
    sources: list = []
    pages_read: list = []


class RejectSystemActionRequest(BaseModel):
    reason: str = ""


class ReportPathRequest(BaseModel):
    path: str


class LintRequest(BaseModel):
    save: bool = False  # write lint report to _wiki/insights/ if True
    force_refresh: bool = False  # if True, bypass cache and re-run Sonnet scan


class ApplyLinkFixesRequest(BaseModel):
    dry_run: bool = False


class ConsolidateRequest(BaseModel):
    source: str   # page to merge FROM (will be deleted after)
    target: str   # page to merge INTO (will be updated)
    force: bool = False  # allow manual override for non-high-confidence pairs
    dry_run: bool = False  # draft only: return the merged text, write nothing
    # Apply a previewed draft instead of calling the LLM again. The hashes must
    # match the pages' current content, so an edit after the preview is caught.
    merged: Optional[str] = None
    source_sha: Optional[str] = None
    target_sha: Optional[str] = None


class DismissPairRequest(BaseModel):
    source: str
    target: str


# ── /ingest ──────────────────────────────────────────────────────────────────

def _vault_path() -> Path:
    return Path(os.environ.get("VAULT_PATH", "/Users/sakethv7/SakethVault"))


def _wiki_path() -> Path:
    return _vault_path() / "_wiki"


def _inbox_path() -> Path:
    return _wiki_path() / "inbox"


def _processed_inbox_path() -> Path:
    return _inbox_path() / "processed"


def _inbox_source_dirs() -> list[Path]:
    """
    Default scan paths for raw markdown clips.
    Override with CLIP_INBOX_DIRS (comma-separated absolute or relative paths).
    """
    custom = os.environ.get("CLIP_INBOX_DIRS", "").strip()
    if custom:
        dirs = []
        for raw in custom.split(","):
            raw = raw.strip()
            if not raw:
                continue
            p = Path(raw).expanduser()
            if not p.is_absolute():
                p = _vault_path() / p
            dirs.append(p)
        return dirs

    return [
        _inbox_path(),                 # new canonical path
        _vault_path() / "Clippings",   # Obsidian Web Clipper default
        _vault_path() / "clippings",   # lowercase variant
        _wiki_path() / "Clippings",
    ]


def _processed_clips_index_path() -> Path:
    return _wiki_path() / "meta" / "processed_clips.jsonl"


def _extract_markdown_title(markdown: str) -> str:
    # Prefer frontmatter title, then first markdown heading
    fm = vault_reader._parse_frontmatter(markdown)
    title = (fm.get("title") or "").strip() if isinstance(fm, dict) else ""
    if title:
        return title
    for line in markdown.splitlines():
        s = line.strip()
        if s.startswith("# "):
            return s[2:].strip()
    return "Web clip"


def _extract_markdown_mermaid(markdown: str) -> str:
    m = _re.search(r"```mermaid\s*([\s\S]*?)```", markdown, flags=_re.IGNORECASE)
    return (m.group(1).strip() if m else "")


def _extract_markdown_image_links(markdown: str) -> list[str]:
    links = []
    # Markdown image syntax ![alt](url)
    for m in _re.finditer(r"!\[[^\]]*\]\((https?://[^)\s]+)\)", markdown):
        links.append(m.group(1))
    # HTML image tags
    for m in _re.finditer(r'<img[^>]+src=["\'](https?://[^"\']+)["\']', markdown, flags=_re.IGNORECASE):
        links.append(m.group(1))
    # Preserve order, unique
    seen = set()
    out = []
    for u in links:
        if u in seen:
            continue
        seen.add(u)
        out.append(u)
    return out[:10]


def _clip_signature(markdown: str, source_url: str = "", clip_title: str = "") -> str:
    body = " ".join(markdown.split())[:2000]
    seed = f"{source_url.strip()}|{clip_title.strip()}|{body}"
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()


def _is_clip_processed(sig: str) -> bool:
    # Check queue first
    for item in queue_manager.get_all():
        if item.get("clip_signature") == sig:
            return True
    # Then check historical index
    idx = _processed_clips_index_path()
    if not idx.exists():
        return False
    for line in idx.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            rec = json.loads(line)
        except Exception:
            continue
        if rec.get("clip_signature") == sig and _ledger_page_exists(rec):
            return True
    return False


def _ledger_page_exists(rec: dict) -> bool:
    """A ledger entry blocks re-processing only while its page still exists."""
    written = (rec.get("file_written") or "").split(" [")[0].strip()
    if not written:
        return True  # no page recorded (e.g. skipped duplicate): keep the block
    path = Path(written)
    return (path if path.is_absolute() else _vault_path() / path).exists()


def _record_processed_clip(item: dict, file_written: str) -> None:
    sig = item.get("clip_signature")
    if not sig:
        return
    idx = _processed_clips_index_path()
    idx.parent.mkdir(parents=True, exist_ok=True)
    rec = {
        "ts": datetime.now().isoformat(),
        "clip_signature": sig,
        "title": item.get("title", ""),
        "source_url": item.get("url", ""),
        "inbox_file": item.get("inbox_file", ""),
        "file_written": file_written,
    }
    with open(idx, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")

    inbox_file = item.get("inbox_file")
    if inbox_file:
        src = Path(inbox_file)
        if src.exists():
            _processed_inbox_path().mkdir(parents=True, exist_ok=True)
            dst = _processed_inbox_path() / src.name
            if dst.exists():
                stem = src.stem
                dst = _processed_inbox_path() / f"{stem}-{int(datetime.now().timestamp())}.md"
            shutil.move(str(src), str(dst))


def _stage_markdown_clip(markdown: str, source_url: str = "", clip_title: str = "", inbox_file: str = "", force: bool = False) -> dict:
    text = markdown.strip()
    if not text:
        raise HTTPException(400, "Markdown is empty")
    sig = _clip_signature(text, source_url, clip_title)
    if not force and _is_clip_processed(sig):
        raise HTTPException(409, "Clip already processed")

    title_hint = clip_title.strip() if clip_title else _extract_markdown_title(text)
    existing_pages = [p["name"] for p in vault_reader.list_concept_pages()]
    extraction = _extract_with_sonnet(text, [], source_url or f"clip://{title_hint}", existing_pages)
    # Preserve raw markdown visuals deterministically when present
    md_diagram = _extract_markdown_mermaid(text)
    if md_diagram and not extraction.get("diagram"):
        extraction["diagram"] = md_diagram
    md_images = _extract_markdown_image_links(text)
    if md_images:
        refs = extraction.get("references", []) or []
        extraction["references"] = list(dict.fromkeys(refs + md_images))[:10]
    item_id = str(uuid.uuid4())
    item = {
        "id": item_id,
        "url": source_url or "",
        "title": extraction["title"] or title_hint,
        "key_concepts": extraction.get("key_concepts", []),
        "summary": extraction["summary"],
        "suggested_page": extraction["suggested_page"],
        "suggested_wikilinks": extraction.get("suggested_wikilinks", []),
        "tags": extraction.get("tags", []),
        "references": extraction.get("references", []),
        "diagram": extraction.get("diagram", ""),
        **_curation_fields(extraction),
        "source_type": "clip_markdown",
        "clip_signature": sig,
        "raw_markdown": text,
        "source_evidence": {"text": text, "images": []},
        "revision": 0,
        "diagram_revisions": [],
        "inbox_file": inbox_file,
        "staged_at": datetime.now().isoformat(),
        "status": "pending",
    }
    queue_manager.enqueue(item)
    return item


DIAGRAM_INTENTS = {"faithful_source", "explain_mechanism", "compare_alternatives"}


def _diagram_evidence(item: dict) -> tuple[str, list]:
    """Return only capture-time evidence; never re-fetch a drifting URL."""
    evidence = item.get("source_evidence") if isinstance(item.get("source_evidence"), dict) else {}
    text = str(evidence.get("text") or item.get("raw_markdown") or "").strip()
    images = evidence.get("images") if isinstance(evidence.get("images"), list) else []
    return text, images


def _regeneration_plan(item: dict, evidence: str, intent: str) -> dict:
    """Reuse the curation decision, allowing an explicit no-diagram outcome."""
    current = _normalize_diagram_plan(item.get("diagram_plan"))
    if not current.get("needed"):
        return {"needed": False, "type": "none", "reason": "Source has no approved visual structure."}
    shape = str(item.get("knowledge_shape") or "none").lower()
    types = {
        "taxonomy": "hierarchy", "mechanism": "flowchart", "architecture": "architecture",
        "argument": "comparison", "case_study": "flowchart",
    }
    plan_type = types.get(shape, current.get("type", "flowchart"))
    if intent == "compare_alternatives" and shape in {"argument", "taxonomy"}:
        plan_type = "comparison"
    return {
        "needed": True,
        "type": plan_type,
        "reason": f"Regenerated from captured {shape if shape != 'none' else 'source'} structure.",
    }


def _diagram_node_labels(diagram: str) -> list[str]:
    return [label.strip() for label in _re.findall(r"\[([^\]]+)\]", diagram or "") if label.strip()]


def _validate_diagram_candidate(diagram: str, plan: dict, evidence: str, key_concepts: list) -> dict:
    """Cheap deterministic gate; browser Mermaid remains the final renderer."""
    if not plan.get("needed"):
        return {"syntax_valid": True, "shape_match": True, "grounded_labels": True, "selectable": True}
    clean = _normalize_mermaid(diagram or "").strip()
    if not _re.match(r"^(?:flowchart|graph)\s+(?:TB|TD|BT|RL|LR)\b", clean, flags=_re.I):
        return {"syntax_valid": False, "shape_match": False, "grounded_labels": False, "selectable": False,
                "reason": "Diagram must start with a Mermaid flowchart direction."}
    if len(_re.findall(r"(?:-->|---|==>)", clean)) < 2 or len(_diagram_node_labels(clean)) < 3:
        return {"syntax_valid": False, "shape_match": False, "grounded_labels": False, "selectable": False,
                "reason": "Diagram needs at least three labeled nodes and two relationships."}
    if plan.get("type") == "hierarchy" and not _re.search(r"flowchart\s+(?:TB|TD)\b", clean, flags=_re.I):
        return {"syntax_valid": True, "shape_match": False, "grounded_labels": False, "selectable": False,
                "reason": "A hierarchy must use a top-down layout."}
    evidence_tokens = set(_re.findall(r"[a-z0-9]+", (evidence + " " + " ".join(map(str, key_concepts or []))).lower()))
    unsupported = []
    for label in _diagram_node_labels(clean):
        tokens = [t for t in _re.findall(r"[a-z0-9]+", label.lower()) if len(t) > 2]
        if tokens and not any(t in evidence_tokens for t in tokens):
            unsupported.append(label)
    if unsupported:
        return {"syntax_valid": True, "shape_match": True, "grounded_labels": False, "selectable": False,
                "reason": f"Unsupported labels: {', '.join(unsupported[:3])}."}
    return {"syntax_valid": True, "shape_match": True, "grounded_labels": True, "selectable": True}


def _generate_evidence_diagram(item: dict, evidence: str, images: list, plan: dict, intent: str, feedback: str) -> str:
    if not plan.get("needed"):
        return ""
    if images:
        # Vision generation uses the original labels and directions where available.
        return _vision_diagram(images, item.get("title", "Concept"), item.get("summary", []))
    prompt = f"""Generate a Mermaid diagram strictly from the captured source below.

Title: {item.get('title', 'Concept')}
Required structure: {plan.get('type')}
Reviewer intent: {intent}
Reviewer feedback (non-authoritative): {feedback or 'None'}

Rules:
- Return NONE if the source does not support this structure.
- Use only component names, concepts, and relationships supported by the source.
- Use flowchart TD for hierarchy; flowchart LR otherwise.
- 3-10 nodes, short labels, at least two arrows.
- Return raw Mermaid only: no fences or explanation.

Captured source:
{evidence[:10000]}"""
    raw = llm_client.complete(task="diagram", model=None, max_tokens=600,
                              messages=[{"role": "user", "content": prompt}]).strip()
    raw = _re.sub(r"^```(?:mermaid)?\s*", "", raw)
    raw = _re.sub(r"\s*```$", "", raw).strip()
    return "" if raw == "NONE" else _normalize_mermaid(raw)


def _regen_item(item: dict, mode: str = "full") -> dict:
    """Regenerate queue item extraction from raw markdown or source URL."""
    updated = dict(item)
    existing_pages = [p["name"] for p in vault_reader.list_concept_pages()]

    if mode == "diagram":
        raise HTTPException(410, "Diagram-only regeneration moved to versioned candidates.")

    raw_md = updated.get("raw_markdown", "")
    src_url = updated.get("url", "")
    if raw_md:
        extraction = _extract_with_sonnet(raw_md, [], src_url or "clip://regenerate", existing_pages)
        md_diagram = _extract_markdown_mermaid(raw_md)
        if md_diagram and not extraction.get("diagram"):
            extraction["diagram"] = md_diagram
        md_images = _extract_markdown_image_links(raw_md)
        if md_images:
            refs = extraction.get("references", []) or []
            extraction["references"] = list(dict.fromkeys(refs + md_images))[:10]
    elif src_url:
        raw = _fetch_url(src_url)
        extraction = _extract_with_sonnet(raw, [], src_url, existing_pages)
    else:
        raise HTTPException(400, "Cannot regenerate: item has neither raw_markdown nor url")

    for k in [
        "title", "key_concepts", "summary", "suggested_page", "suggested_wikilinks",
        "tags", "references", "diagram", "source_verdict", "educational_core",
        "discarded_context", "knowledge_shape", "diagram_plan",
    ]:
        updated[k] = extraction.get(k, updated.get(k))
    updated["regenerated_at"] = datetime.now().isoformat()
    updated["regenerated_mode"] = "full"
    return updated

def _is_ios_shortcut(request: Request) -> bool:
    """Detect iOS Shortcuts / Share Sheet callers by User-Agent."""
    ua = request.headers.get("user-agent", "").lower()
    return any(s in ua for s in ("shortcuts", "cfnetwork", "darwin", "ios"))


@app.post("/ingest")
async def ingest(req: IngestRequest, request: Request):
    request_started = time.perf_counter()
    stage_ms: dict[str, float] = {}

    def _mark_stage(name: str, started: float) -> None:
        stage_ms[name] = round((time.perf_counter() - started) * 1000, 1)

    if not req.url and not req.text and not req.image_base64 and not req.images:
        raise HTTPException(400, "Provide url, text, image_base64, or images")

    source_url = req.url or ""

    # Deduplication: reject if URL is already pending in queue or written to vault
    if source_url and not req.force:
        duplicate = _find_duplicate(source_url)
        if duplicate:
            raise HTTPException(409, f"Already ingested: {duplicate}")

    # iOS Shortcuts / Share Sheet: queue instantly and extract in background
    # so the request returns in <100ms and never times out on the device
    if _is_ios_shortcut(request) and source_url and not req.text and not req.image_base64:
        item_id = str(uuid.uuid4())
        item = {
            "id": item_id,
            "url": source_url,
            "title": source_url,
            "key_concepts": [],
            "summary": ["Extracting in background…"],
            "suggested_page": "unprocessed",
            "suggested_wikilinks": [],
            "tags": [],
            "diagram": "",
            "pending_extraction": True,
            "queued_at": datetime.now().isoformat(),
        }
        queue_manager.enqueue(item)
        asyncio.create_task(_background_extract(item_id, source_url))
        return {"id": item_id, "queued": True, "diff_preview": {
            "title": source_url, "summary": ["Saved — extracting in background"],
            "suggested_page": "unprocessed", "suggested_wikilinks": [], "tags": [],
            "key_concepts": [], "references": [], "diagram": "",
        }}

    raw_text = ""
    loop = asyncio.get_running_loop()

    # Step 1: fetch URL with httpx + parse with BeautifulSoup (zero LLM)
    if req.url:
        started = time.perf_counter()
        raw_text = await loop.run_in_executor(None, _fetch_url, req.url)
        _mark_stage("fetch_url", started)

    if req.text:
        raw_text = (raw_text + "\n\n" + req.text).strip()

    # Normalise images: merge legacy single image + new images list
    all_images = list(req.images or [])
    if req.image_base64 and not all_images:
        all_images = [{"data": req.image_base64, "mediaType": "image/png"}]

    # Step 2: extract — image payloads skip slicing (already atomic)
    started = time.perf_counter()
    existing_pages = [p["name"] for p in vault_reader.list_concept_pages()]
    _mark_stage("list_existing_pages", started)
    source_type = req.source_type or ("lecture" if all_images else ("url" if req.url else "text"))

    if all_images:
        def _image_pipeline():
            pipeline_stage_ms: dict[str, float] = {}

            def _pipeline_mark(name: str, started: float) -> None:
                pipeline_stage_ms[name] = round((time.perf_counter() - started) * 1000, 1)

            started = time.perf_counter()
            uncertainties = _extract_image_uncertainties(all_images, req.user_notes or "")
            _pipeline_mark("image_uncertainty_extract", started)
            started = time.perf_counter()
            search_context = _tavily_search(uncertainties) if uncertainties else ""
            _pipeline_mark("image_gap_search", started)
            enriched_text = (raw_text + "\n\n" + search_context).strip() if search_context else raw_text
            started = time.perf_counter()
            extraction = _extract_with_sonnet(
                enriched_text, all_images, source_url, existing_pages, user_notes=req.user_notes
            )
            _pipeline_mark("vision_extract", started)
            return {
                "extraction": extraction,
                "stage_ms": pipeline_stage_ms,
                "uncertainty_count": len(uncertainties or []),
                "search_context_chars": len(search_context or ""),
            }
        slices = [{"title": "", "text": raw_text, "concept_hint": ""}]
        started = time.perf_counter()
        image_result = await loop.run_in_executor(None, _image_pipeline)
        _mark_stage("image_pipeline_total", started)
        stage_ms.update(image_result.get("stage_ms", {}))
        extractions = [image_result["extraction"]]
        image_uncertainty_count = image_result.get("uncertainty_count", 0)
        image_search_context_chars = image_result.get("search_context_chars", 0)
    else:
        # Slice step: decide if content splits into multiple conceptual notes
        started = time.perf_counter()
        slices = await loop.run_in_executor(None, _slice_content, raw_text, source_url, existing_pages)
        _mark_stage("slice_content", started)

        # Extract each slice in parallel
        from concurrent.futures import ThreadPoolExecutor
        def _extract_slice(sl: dict):
            ex = _extract_with_sonnet(sl["text"], [], source_url, existing_pages)
            # If slicer gave us a chapter title, prefer it over the LLM's extracted title
            if sl.get("title"):
                ex["title"] = sl["title"]
            # If slicer identified a matching vault page, bias suggested_page toward it
            if sl.get("concept_hint") and not ex.get("suggested_page"):
                ex["suggested_page"] = sl["concept_hint"]
            return ex
        started = time.perf_counter()
        with ThreadPoolExecutor(max_workers=min(len(slices), 4)) as pool:
            extractions = list(pool.map(_extract_slice, slices))
        _mark_stage("text_extract_slices", started)
        image_uncertainty_count = 0
        image_search_context_chars = 0

    # Step 3: stage all slices to queue
    started = time.perf_counter()
    items = []
    for extraction in extractions:
        item_id = str(uuid.uuid4())
        item = {
            "id": item_id,
            "url": source_url,
            "source_type": source_type,
            "user_notes": req.user_notes or "",
            "title": extraction["title"],
            "key_concepts": extraction["key_concepts"],
            "summary": extraction["summary"],
            "suggested_page": extraction["suggested_page"],
            "suggested_wikilinks": extraction["suggested_wikilinks"],
            "tags": extraction["tags"],
            "references": extraction.get("references", []),
            "diagram": extraction.get("diagram", ""),
            **_curation_fields(extraction),
            "source_evidence": {"text": raw_text, "images": all_images},
            "revision": 0,
            "diagram_revisions": [],
            "staged_at": datetime.now().isoformat(),
            "status": "pending",
        }
        queue_manager.enqueue(item)
        items.append(item)
    _mark_stage("queue_stage", started)

    first = items[0]
    total_ms = round((time.perf_counter() - request_started) * 1000, 1)
    latency = {
        "total_ms": total_ms,
        "stage_ms": stage_ms,
        "source_type": source_type,
        "image_count": len(all_images),
        "slice_count": len(items),
        "source_chars": len(raw_text or ""),
        "image_uncertainty_count": image_uncertainty_count,
        "image_search_context_chars": image_search_context_chars,
    }
    telemetry.log_context_event("ingest_latency", latency)
    return {
        "id": first["id"],
        "sliced": len(items) > 1,
        "slice_count": len(items),
        "slice_titles": [it["title"] for it in items[1:]],
        "latency": latency,
        "diff_preview": {
            "title": first["title"],
            "summary": first["summary"],
            "suggested_page": first["suggested_page"],
            "suggested_wikilinks": first["suggested_wikilinks"],
            "tags": first["tags"],
            "key_concepts": first["key_concepts"],
            "references": first["references"],
            "diagram": first["diagram"],
            **_curation_fields(first),
            "lenses": first.get("lenses", {}),
            "synthesis": first.get("synthesis", ""),
            "open_questions": first.get("open_questions", []),
        },
    }


@app.post("/ingest-markdown")
async def ingest_markdown(req: MarkdownIngestRequest):
    loop = asyncio.get_running_loop()
    item = await loop.run_in_executor(
        None,
        lambda: _stage_markdown_clip(
            markdown=req.markdown,
            source_url=req.source_url or "",
            clip_title=req.clip_title or "",
            force=req.force,
        ),
    )
    return {
        "id": item["id"],
        "diff_preview": {
            "title": item["title"],
            "summary": item["summary"],
            "suggested_page": item["suggested_page"],
            "suggested_wikilinks": item["suggested_wikilinks"],
            "tags": item["tags"],
            "key_concepts": item["key_concepts"],
            "references": item["references"],
            "diagram": item["diagram"],
            **_curation_fields(item),
        },
    }


@app.post("/inbox/process")
async def process_inbox(req: InboxProcessRequest):
    inbox = _inbox_path()
    inbox.mkdir(parents=True, exist_ok=True)
    _processed_inbox_path().mkdir(parents=True, exist_ok=True)

    unique_files = []
    seen = set()
    for src_dir in _inbox_source_dirs():
        if not src_dir.exists() or not src_dir.is_dir():
            continue
        for p in sorted(src_dir.glob("*.md")):
            if not p.is_file():
                continue
            try:
                key = str(p.resolve())
            except Exception:
                key = str(p.absolute())
            if key in seen:
                continue
            seen.add(key)
            unique_files.append(p)

    files = unique_files[: max(1, min(req.limit, 200))]

    def _process_files(files, force):
        queued, skipped, errors = [], [], []
        for md_path in files:
            try:
                markdown = md_path.read_text(encoding="utf-8")
                item = _stage_markdown_clip(
                    markdown=markdown,
                    source_url="",
                    clip_title=md_path.stem.replace("-", " ").strip(),
                    inbox_file=str(md_path),
                    force=force,
                )
                queued.append({"file": md_path.name, "id": item["id"], "suggested_page": item["suggested_page"]})
            except HTTPException as e:
                if e.status_code == 409:
                    skipped.append({"file": md_path.name, "reason": "already processed"})
                    try:
                        dst = _processed_inbox_path() / md_path.name
                        if dst.exists():
                            dst = _processed_inbox_path() / f"{md_path.stem}-{int(datetime.now().timestamp())}.md"
                        shutil.move(str(md_path), str(dst))
                    except Exception:
                        pass
                else:
                    errors.append({"file": md_path.name, "error": str(e.detail)})
            except Exception as e:
                errors.append({"file": md_path.name, "error": str(e)})
        return queued, skipped, errors

    loop = asyncio.get_running_loop()
    queued, skipped, errors = await loop.run_in_executor(None, lambda: _process_files(files, req.force))

    return {
        "success": True,
        "inbox_dir": str(inbox),
        "scan_dirs": [str(p) for p in _inbox_source_dirs()],
        "queued_count": len(queued),
        "queued": queued,
        "skipped": skipped,
        "errors": errors,
    }


def _is_tweet_url(url: str) -> bool:
    """Detect Twitter/X tweet URLs."""
    import re
    return bool(re.match(r"https?://(www\.)?(twitter\.com|x\.com)/\w+/status/\d+", url))


def _fetch_tweet(url: str) -> str:
    """
    Fetch tweet via fxtwitter community API (api.fxtwitter.com).
    Free, no API key, no cost, no LLM. Works for all public tweets.
    Extracts: text, author, media descriptions, quoted tweets.
    """
    import re
    # Extract /username/status/id from twitter.com or x.com URLs
    m = re.search(r"(?:twitter\.com|x\.com)/(\w+)/status/(\d+)", url)
    if not m:
        raise HTTPException(400, f"Could not parse tweet URL: {url}")
    username, tweet_id = m.group(1), m.group(2)

    try:
        resp = httpx.get(
            f"https://api.fxtwitter.com/{username}/status/{tweet_id}",
            headers={"User-Agent": "SakethWiki/1.0 (personal knowledge base)"},
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        raise HTTPException(502, f"Could not fetch tweet via fxtwitter: {e}")

    if data.get("code") != 200 or not data.get("tweet"):
        raise HTTPException(404, f"Tweet not found or private (id={tweet_id})")

    tweet = data["tweet"]
    author = tweet.get("author", {})
    text = tweet.get("text", "")
    raw_text_obj = tweet.get("raw_text", {})
    created_at = tweet.get("created_at", "")

    # X Article — fxtwitter returns full content in tweet.article.content.blocks
    if tweet.get("article"):
        article = tweet["article"]
        article_title = article.get("title", "")
        blocks = article.get("content", {}).get("blocks", [])
        parts = []
        if article_title:
            parts.append(f"# {article_title}")
        for block in blocks:
            block_text = block.get("text", "").strip()
            if not block_text:
                continue
            btype = block.get("type", "unstyled")
            if btype in ("header-one",):
                parts.append(f"# {block_text}")
            elif btype in ("header-two",):
                parts.append(f"## {block_text}")
            elif btype in ("header-three",):
                parts.append(f"### {block_text}")
            else:
                parts.append(block_text)
        text = "\n\n".join(parts)

    # If text is still empty, it may be a pure media post or external link
    if not text.strip():
        raw_content = raw_text_obj.get("text", "") if isinstance(raw_text_obj, dict) else ""
        tco_match = __import__("re").search(r"https://t\.co/\S+", raw_content)
        if tco_match:
            tco_url = tco_match.group(0)
            try:
                redir = httpx.head(tco_url, follow_redirects=True, timeout=8)
                final_url = str(redir.url)
                if "x.com" not in final_url and "twitter.com" not in final_url:
                    # External URL — fetch it normally
                    text = f"[Linked article fetched from {final_url}]\n\n" + _fetch_url(final_url)
                else:
                    text = f"[Media-only tweet. Linked content: {final_url}]"
            except Exception:
                text = f"[Tweet contained a link that could not be followed: {raw_content}]"
        else:
            text = "[Media-only tweet with no text content]"

    # Pull in any quoted tweet text for extra context
    quote_text = ""
    if tweet.get("quote"):
        q = tweet["quote"]
        qa = q.get("author", {})
        quote_text = f'\n\nQuoted tweet by @{qa.get("screen_name", "")}:\n{q.get("text", "")}'

    # Describe any media (images/videos) — useful context for the LLM
    media_text = ""
    media = tweet.get("media", {})
    if media:
        photos = media.get("photos", [])
        videos = media.get("videos", [])
        if photos:
            media_text += f"\n\n[{len(photos)} image(s) attached]"
        if videos:
            media_text += f"\n\n[{len(videos)} video(s) attached]"

    # Extract external URL links from tweet facets (type="url" = external link)
    raw_text_obj2 = tweet.get("raw_text", {})
    facets = raw_text_obj2.get("facets", []) if isinstance(raw_text_obj2, dict) else []
    ext_links = []
    for facet in facets:
        if facet.get("type") == "url":
            real_url = facet.get("replacement", "")
            if real_url and not any(d in real_url for d in ("x.com", "twitter.com", "t.co")):
                ext_links.append(real_url)
    # Also check twitter_card for a linked URL
    card = tweet.get("twitter_card", {})
    if isinstance(card, dict) and card.get("url"):
        card_url = card["url"]
        if not any(d in card_url for d in ("x.com", "twitter.com", "t.co")):
            if card_url not in ext_links:
                ext_links.append(card_url)

    links_text = ""
    if ext_links:
        links_text = "\n\nReferenced links:\n" + "\n".join(f"- {u}" for u in ext_links)

    return (
        f"Tweet by @{author.get('screen_name', username)} ({author.get('name', '')})\n"
        f"Posted: {created_at}\n"
        f"URL: {url}\n\n"
        f"{text}"
        f"{quote_text}"
        f"{media_text}"
        f"{links_text}"
    )


def _fetch_url(url: str) -> str:
    """Fetch URL and extract readable text via BeautifulSoup. Zero LLM."""
    # Route tweet URLs through oEmbed instead of direct fetch
    if _is_tweet_url(url):
        return _fetch_tweet(url)

    try:
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            )
        }
        resp = httpx.get(url, headers=headers, follow_redirects=True, timeout=15)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")

        # Remove boilerplate
        for tag in soup(["script", "style", "nav", "footer", "header", "aside"]):
            tag.decompose()

        # Prefer <article> or <main>, fall back to <body>
        main = soup.find("article") or soup.find("main") or soup.body
        if main:
            text = main.get_text(separator="\n", strip=True)
        else:
            text = soup.get_text(separator="\n", strip=True)

        # Extract notable external links from the page
        from urllib.parse import urlparse as _urlparse
        source_domain = _urlparse(url).netloc
        seen_ext = set()
        ext_links = []
        for a in (soup.find_all("a", href=True) if soup else []):
            href = a["href"].strip()
            if not href.startswith("http"):
                continue
            link_domain = _urlparse(href).netloc
            if link_domain == source_domain or href in seen_ext:
                continue
            seen_ext.add(href)
            ext_links.append(href)
            if len(ext_links) >= URL_SCRAPE_LINK_LIMIT:  # cap — let Sonnet pick the useful ones
                break

        links_block = ""
        if ext_links:
            links_block = "\n\nReferenced links:\n" + "\n".join(f"- {u}" for u in ext_links)

        return text[:URL_SCRAPE_CHAR_LIMIT] + links_block
    except Exception as e:
        raise HTTPException(502, f"Failed to fetch URL: {e}")


def _load_wiki_standards(section: str = "") -> str:
    """Read wiki-standards.md from the vault.

    If `section` is provided (e.g. "For Ingest", "For Health Check"),
    returns only the content of that ## section.
    Otherwise returns the full body (no frontmatter).
    """
    vault_path_obj = Path(os.environ.get("VAULT_PATH", "/Users/sakethv7/SakethVault"))
    standards_path = vault_path_obj / "_wiki" / "meta" / "wiki-standards.md"
    if not standards_path.exists():
        return ""
    body = _strip_frontmatter(standards_path.read_text(encoding="utf-8")).strip()
    if not section:
        return body
    # Extract the matching ## section up to the next ## or end of file
    import re as _re
    pattern = rf"##\s+{_re.escape(section)}\s*\n([\s\S]*?)(?=\n##\s|\Z)"
    m = _re.search(pattern, body)
    return m.group(1).strip() if m else body


def _top_matching_pages(query_text: str, all_pages: list, max_pages: int = 10) -> list:
    """Return up to max_pages page slugs most relevant to query_text by keyword overlap.
    Prevents the existing_pages hint from growing unbounded as the vault scales.
    """
    if not all_pages or not query_text:
        return all_pages[:max_pages]
    query_words = set(query_text.lower().replace("-", " ").split())
    scored = []
    for slug in all_pages:
        slug_words = set(slug.replace("-", " ").split())
        score = len(query_words & slug_words)
        scored.append((score, slug))
    scored.sort(key=lambda x: (-x[0], x[1]))
    # Always include exact matches first, then fill to max_pages
    top = [s for _, s in scored[:max_pages]]
    return top

def _normalize_slug(text: str) -> str:
    return identity.resolve_slug(text)


def _filter_suggested_wikilinks(
    suggested: list,
    title: str,
    summary: list,
    key_concepts: list,
    existing_pages: list,
    max_links: int = 6,
) -> list[str]:
    if not suggested:
        return []

    scope_text = " ".join([title or ""] + [str(x) for x in (summary or [])] + [str(x) for x in (key_concepts or [])]).lower()
    scope_tokens = set(_re.findall(r"[a-z0-9]+", scope_text))
    if not scope_tokens:
        return []

    existing_set = set(existing_pages or [])
    out = []
    seen = set()

    for raw in suggested:
        slug = _normalize_slug(str(raw))
        if not slug or slug in seen:
            continue
        seen.add(slug)

        slug_tokens = set(t for t in slug.split("-") if t)
        overlap = len(slug_tokens & scope_tokens)
        token_count = len(slug_tokens)

        # Stronger acceptance for non-existing pages to avoid hallucinated side quests.
        if slug in existing_set:
            keep = overlap >= 1
        else:
            keep = overlap >= 2 if token_count >= 2 else overlap >= 1

        if keep:
            out.append(slug)
        if len(out) >= max_links:
            break

    return out



def _tavily_search(queries: list[str]) -> str:
    """Run up to 3 Tavily searches in parallel. Returns a context block string."""
    api_key = os.environ.get("TAVILY_API_KEY", "")
    if not api_key or not queries:
        return ""
    try:
        from tavily import TavilyClient
        from concurrent.futures import ThreadPoolExecutor, as_completed
        client = TavilyClient(api_key=api_key)

        def _search(q: str) -> list[dict]:
            try:
                resp = client.search(q, max_results=2, search_depth="basic")
                return resp.get("results", [])
            except Exception:
                return []

        results = []
        with ThreadPoolExecutor(max_workers=len(queries)) as pool:
            for res in pool.map(_search, queries[:3]):
                results.extend(res)

        if not results:
            return ""
        lines = [f"- [{r.get('title','')}]: {r.get('content','')[:200]}" for r in results]
        return "Web search context (use to fill gaps in the screenshot):\n" + "\n".join(lines)
    except Exception as e:
        logger.warning(f"Tavily search failed: {e}")
        return ""


def _extract_image_uncertainties(images: list, user_notes: str) -> list[str]:
    """Quick Haiku pass: extract 3 uncertain concepts that need web lookup."""
    user_block = f"\nStudent's current understanding: \"{user_notes}\"" if user_notes else ""
    content = []
    for img in images[:2]:
        content.append({"type": "image", "source": {"type": "base64",
            "media_type": img.get("mediaType", "image/png"), "data": img["data"]}})
    content.append({"type": "text", "text":
        f"Look at these lecture screenshot(s).{user_block}\n\n"
        "List up to 3 specific technical concepts or terms that are mentioned but not fully explained "
        "and would benefit from a quick web lookup to give more context. "
        "Return ONLY a JSON array of short query strings, e.g. [\"KV cache in transformers\", \"softmax temperature\"]. "
        "Return [] if everything is self-contained."})
    try:
        raw = llm_client.complete(
            task="ingest_image_uncertainties",
            model=None,  # vision model selected by provider routing
            max_tokens=200,
            messages=[{"role": "user", "content": content}],
        ).strip()
        raw = raw[raw.find("["):raw.rfind("]") + 1]
        return json.loads(raw) if raw else []
    except Exception:
        return []


def _string_list(value, limit: int = 8) -> list[str]:
    if not isinstance(value, list):
        return []
    out: list[str] = []
    for item in value:
        s = str(item).strip()
        if s:
            out.append(s[:280])
        if len(out) >= limit:
            break
    return out


def _normalize_diagram_plan(value) -> dict:
    if not isinstance(value, dict):
        return {"needed": False, "type": "none", "reason": "No diagram plan returned."}
    needed = bool(value.get("needed"))
    plan_type = str(value.get("type") or "none").strip().lower().replace(" ", "-")[:60] or "none"
    reason = str(value.get("reason") or "").strip()[:280]
    return {
        "needed": needed,
        "type": plan_type,
        "reason": reason or ("Source has explicit structure." if needed else "No durable visual structure."),
    }


def _normalize_mermaid(diagram: str) -> str:
    """Clean up the three things Haiku reliably gets wrong in Mermaid output."""
    if not diagram or not diagram.strip():
        return diagram
    text = diagram

    # `graph TD` -> `flowchart TD`. Mermaid treats them the same; flowchart is
    # the documented form and the one the extraction prompt asks for.
    text = _re.sub(
        r"^(\s*)graph(\s+(?:TB|TD|BT|RL|LR))?(\s*)$",
        lambda m: f"{m.group(1)}flowchart{m.group(2) or ''}{m.group(3)}",
        text,
        count=1,
        flags=_re.M,
    )

    # Uniform edge label: Haiku emits `-->|staging|` on every arrow, which
    # conveys nothing. When every edge carries the same label, strip them all.
    pipe_labels = _re.findall(r"(?:-{2,3}>|={2,3}>)\s*\|([^|]*)\|", text)
    edge_count = len(_re.findall(r"-{2,3}>|={2,3}>", text))
    distinct = {lbl.strip() for lbl in pipe_labels if lbl.strip()}
    if len(pipe_labels) >= 2 and len(pipe_labels) == edge_count and len(distinct) == 1:
        text = _re.sub(r"((?:-{2,3}>|={2,3}>))\s*\|[^|]*\|", r"\1", text)

    # `:::accent` used but `classDef accent` never defined -> add the class the
    # theme expects so the node actually renders highlighted.
    if _re.search(r":::\s*accent\b", text) and not _re.search(r"classDef\s+accent\b", text):
        text = text.rstrip() + "\n    classDef accent fill:#c4573a,color:#fff\n"

    return text


def _normalize_extraction_contract(data: dict, depth: str) -> dict:
    """Backfill and constrain the curation contract before queueing or writing."""
    verdict = str(data.get("source_verdict") or "ingest").strip().lower()
    if verdict not in SOURCE_VERDICTS:
        verdict = "ingest"
    shape = str(data.get("knowledge_shape") or "none").strip().lower()
    if shape not in KNOWLEDGE_SHAPES:
        shape = "none"
    data["source_verdict"] = verdict
    data["educational_core"] = _string_list(data.get("educational_core"), limit=8)
    data["discarded_context"] = _string_list(data.get("discarded_context"), limit=8)
    data["knowledge_shape"] = shape
    data["diagram_plan"] = _normalize_diagram_plan(data.get("diagram_plan"))

    if verdict == "reject":
        data["diagram"] = ""
        data["summary"] = data.get("summary") or data["discarded_context"][:3]
        return data

    if not data["educational_core"]:
        data["educational_core"] = _string_list(data.get("summary"), limit=5)

    # Short content can still be a useful note, but should not invent diagrams.
    if depth == "short" and not data["diagram_plan"].get("needed"):
        data["knowledge_shape"] = "none"

    if data.get("diagram"):
        data["diagram"] = _normalize_mermaid(data["diagram"])
    return data


def _diagram_plan_wants_diagram(data: dict) -> bool:
    plan = data.get("diagram_plan") if isinstance(data, dict) else {}
    return bool(isinstance(plan, dict) and plan.get("needed"))


def _curation_fields(data: dict) -> dict:
    return {
        "source_verdict": data.get("source_verdict", "ingest"),
        "educational_core": data.get("educational_core", []),
        "discarded_context": data.get("discarded_context", []),
        "knowledge_shape": data.get("knowledge_shape", "none"),
        "diagram_plan": data.get("diagram_plan", {}),
    }


def _extract_with_sonnet(text: str, images: Optional[list], source_url: str,
                         existing_pages: Optional[list] = None,
                         user_notes: Optional[str] = None) -> dict:
    """Extract structured metadata from content for the wiki.

    Model selection:
    - Long-form (>4000 chars) or images → Sonnet (complex reasoning needed)
    - Short/medium text → Haiku (~8x cheaper, quality fine for structured extraction)
    """
    tags_list = ", ".join(VALID_TAGS)
    existing_pages = existing_pages or []

    # Trim existing_pages to top-10 most relevant — avoids context bloat at scale
    relevant_pages = _top_matching_pages(
        (text or "") + " " + source_url, existing_pages, max_pages=RAG_TOP_K
    )
    pages_hint = (
        "Existing concept pages (prefer mapping to these over creating new ones):\n"
        + ", ".join(relevant_pages)
        if relevant_pages else "No pages yet."
    )

    wiki_standards = _load_wiki_standards("For Ingest")
    standards_block = f"\nCuration standards to follow:\n{wiki_standards}\n" if wiki_standards else ""

    # Inject learned hints from system-insights.md (self-improvement loop)
    hints = _load_extraction_hints()
    hints_block = f"\nLearned extraction hints (from past corrections — follow these):\n{hints}\n" if hints else ""
    preference_hints = preference_memory.prompt_hints()
    preference_block = f"\nUser preference memory (durable correction patterns — follow these):\n{preference_hints}\n" if preference_hints else ""

    user_content: list = []

    # Add all images (multiple supported)
    for img in (images or []):
        user_content.append({
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": img.get("mediaType", "image/png"),
                "data": img["data"],
            },
        })

    # Adapt depth to content length: short tweet/note vs long article/paper
    content_len = len(text) if text else 0
    has_images = bool(images)

    if has_images:
        depth = "long-form"
        model = None  # routes to vision model for active provider (gemini-2.5-flash or claude-sonnet)
        bullet_rule = "EXACTLY 5 bullets — the 5 most important, distinct, specific insights. Each 1-2 sentences. Write as neutral, precise technical notes (no first-person, no 'I learned', no opinion). State facts, mechanisms, and implications directly — like a Lilian Weng blog post. No filler, no repetition."
        diagram_rule = (
            "The image(s) likely show a diagram, architecture, or visual structure — reproduce it faithfully in Mermaid. "
            "Mirror the actual layout: if it shows nodes with labels (e.g. GPU 0..N, Parameter Server, Worker nodes), "
            "use those exact labels. If it shows a pipeline with arrows, reproduce the arrows. "
            "flowchart LR (or TD if the image is top-down). Up to 12 nodes if needed to be accurate. "
            "Short labels — 2-5 words max per node. No quotes, colons, or special chars in labels. "
            "Add 'classDef accent fill:#c4573a,color:#fff,font-weight:bold' and apply :::accent to 1-3 key nodes (entry point, critical component, or final output). "
            "If the image has no structural diagram (e.g. pure text slide, photo), return \"\"."
        )
        content_budget = 10000
        max_out = 1800
    elif content_len > 8000:
        depth = "long-form"
        model = "claude-haiku-4-5-20251001"   # Haiku handles long text fine; Sonnet only needed for images
        bullet_rule = "EXACTLY 5 bullets — the 5 most important, distinct, specific insights. Each 1-2 sentences. Write as neutral, precise technical notes (no first-person, no 'I learned', no opinion). State facts, mechanisms, and implications directly. No filler, no repetition."
        diagram_rule = (
            "ONLY include a diagram if the content has a clear visual structure worth showing "
            "(e.g. architecture, pipeline, hierarchy, decision flow). "
            "If it's a simple concept, a person's opinion, or a news item — return empty string \"\". "
            "If you do draw one: flowchart LR, 5-8 nodes max, short labels (2-4 words each). "
            "Add 'classDef accent fill:#c4573a,color:#fff,font-weight:bold' and apply :::accent to 1-2 key nodes (entry or critical output)."
        )
        content_budget = 10000
        max_out = 1800
    elif content_len > 1500:
        depth = "medium"
        model = "claude-haiku-4-5-20251001"  # structured extraction, no deep reasoning
        bullet_rule = "3-4 bullets — each a distinct, specific insight. Neutral, precise technical prose (no first-person, no 'I learned'). State facts and mechanisms directly. No filler."
        diagram_rule = (
            "ONLY include a diagram if the content has a clear structure worth visualising. "
            "Most medium articles do NOT need one — return \"\" if unsure. "
            "If you do: flowchart LR, 4-6 nodes, short labels. Do not mix runtime flow with taxonomy in one diagram. "
            "Add 'classDef accent fill:#c4573a,color:#fff,font-weight:bold' and apply :::accent to 1-2 key nodes."
        )
        content_budget = 6000
        max_out = 1600
    else:
        depth = "short"
        model = "claude-haiku-4-5-20251001"  # short structured extraction
        bullet_rule = "2-3 bullets — each a sharp, distinct insight. Neutral, precise technical prose (no first-person, no 'I learned')."
        diagram_rule = 'Return "" — short content rarely benefits from a diagram.'
        content_budget = 3000
        max_out = 1400  # Structured JSON needs headroom even for short captures.

    content_budget = _ingest_source_budget(content_budget)
    source_chars_total = len(text or "")
    source_chars_used = min(source_chars_total, content_budget)
    telemetry.log_context_event(
        "ingest_context",
        {
            "task": "ingest_extract",
            "source_url": source_url,
            "depth": depth,
            "has_images": has_images,
            "source_chars_total": source_chars_total,
            "source_chars_used": source_chars_used,
            "source_chars_dropped": max(0, source_chars_total - source_chars_used),
            "source_coverage_ratio": round(source_chars_used / source_chars_total, 4) if source_chars_total else 1.0,
            "content_budget": content_budget,
            "existing_pages_total": len(existing_pages),
            "existing_pages_used": len(relevant_pages),
        },
    )

    user_notes_block = (
        f"\nStudent's own understanding (build on this, correct gaps, don't repeat what they already know):\n"
        f"\"{user_notes}\"\n"
        if user_notes else ""
    )

    user_content.append({
        "type": "text",
        "text": f"""Extract structured metadata from this content for a personal knowledge wiki.
This wiki covers anything educational: AI/ML, DSA, system design, engineering, humanities (history, geopolitics, politics, geography, philosophy), science (physics, math, chemistry), finance, and personal development.

Source URL: {source_url}
Content depth: {depth} ({content_len} chars)
{standards_block}{hints_block}{preference_block}
{pages_hint}
{user_notes_block}
Content:
{text[:content_budget]}

Respond with a JSON object (no markdown fences) with exactly these fields:
{{
  "title": "concise title of the source",
  "source_verdict": "ingest | source_only | reject",
  "educational_core": ["durable idea worth saving", "..."],
  "discarded_context": ["event/social/context detail intentionally not saved", "..."],
  "knowledge_shape": "taxonomy | mechanism | architecture | argument | case_study | none",
  "key_concepts": ["concept1", "concept2"],
  "summary": ["bullet 1", "bullet 2", ...],
  "suggested_page": "slug-for-concept-page (e.g. rag, kv-cache, compound-interest)",
  "suggested_wikilinks": ["related-concept-1", "related-concept-2"],
  "tags": ["Tag1", "Tag2"],
  "references": ["https://...", "https://..."],
  "diagram_plan": {{"needed": true, "type": "flowchart | hierarchy | comparison | loop-stack | none", "reason": "why this visual helps or why no visual should be created"}},
  "diagram": "mermaid diagram as a single JSON string with \\n for newlines"
}}

Rules:
- source_verdict:
  * ingest = source contains durable educational signal for the wiki.
  * source_only = source is mainly event/news/social context; keep links or provenance but write only the transferable educational core.
  * reject = no durable educational value for this wiki. Use reject for recipes, shopping, celebrity gossip, announcements without transferable concepts, or pure hype.
- educational_core: 1-3 reusable concepts, mechanisms, distinctions, failure modes, or implementation lessons. Do not include conference chronology, who-posted-what timelines, sponsor copy, social proof, or namedropping unless it defines the concept.
- discarded_context: 0-2 specific details intentionally excluded from durable notes, especially event chronology, hype context, or non-educational background.
- knowledge_shape: choose the dominant structure before writing bullets. Use taxonomy for categories, mechanism for cause/effect or procedure, architecture for components/layers, argument for claims/tradeoffs, case_study for one concrete example, none for weak structure.
- summary: {bullet_rule}
- suggested_page: use an existing slug if one fits; otherwise create a lowercase-hyphenated slug
- suggested_wikilinks: 0-3 related concepts as kebab-case slugs — prefer existing page slugs listed above
- tags: pick 1-3 from this exact list only: {tags_list}
- key_concepts: 3-5 specific terms or ideas from the content
- references: pick the most valuable external links mentioned in the content (YouTube videos, GitHub repos, papers, key articles). Empty list [] if none. Max 3 links.
- diagram_plan: decide whether a diagram is actually useful before drawing. Prefer no diagram over a generic hub-and-spoke summary. Keep `reason` under 12 words.
- diagram: {diagram_rule} Escape all newlines as \\n in the JSON string. No special chars in node labels.""",
    })

    try:
        raw = llm_client.complete(
            task="ingest_extract",
            model=model,
            max_tokens=max_out,
            messages=[{"role": "user", "content": user_content}],
            expect_json=True,
            required_json_keys=[
                "title",
                "source_verdict",
                "educational_core",
                "discarded_context",
                "knowledge_shape",
                "key_concepts",
                "summary",
                "suggested_page",
                "suggested_wikilinks",
                "tags",
                "references",
                "diagram_plan",
                "diagram",
            ],
        ).strip()
    except Exception as e:
        raise HTTPException(500, f"LLM extraction failed: {e}")
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
        raw = raw.strip()

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        raise HTTPException(500, f"LLM returned invalid JSON: {e}\nRaw: {raw[:300]}")

    data = _normalize_extraction_contract(data, depth=depth)
    telemetry.log_context_event(
        "ingest_curation",
        {
            "task": "ingest_extract",
            "source_url": source_url,
            "depth": depth,
            "source_verdict": data.get("source_verdict"),
            "knowledge_shape": data.get("knowledge_shape"),
            "educational_core_count": len(data.get("educational_core") or []),
            "discarded_context_count": len(data.get("discarded_context") or []),
            "diagram_planned": bool((data.get("diagram_plan") or {}).get("needed")),
            "diagram_plan_type": (data.get("diagram_plan") or {}).get("type", "none"),
        },
    )
    if data.get("source_verdict") == "reject":
        reason = "; ".join(data.get("discarded_context") or data.get("summary") or [])
        raise HTTPException(400, f"Content rejected: {reason or 'no durable educational value'}")

    # Enforce wikilink relevance and kebab-case normalization.
    data["suggested_wikilinks"] = _filter_suggested_wikilinks(
        suggested=data.get("suggested_wikilinks", []),
        title=data.get("title", ""),
        summary=data.get("summary", []),
        key_concepts=data.get("key_concepts", []),
        existing_pages=existing_pages,
        max_links=6,
    )

    # Validate tags against allowed list
    data["tags"] = [t for t in data.get("tags", []) if t in VALID_TAGS]
    data["suggested_page"] = preference_memory.preferred_page(data.get("suggested_page", "general"))
    data["tags"] = [
        tag for tag in preference_memory.preferred_tags(data["tags"])
        if tag in VALID_TAGS
    ]
    # Sanitize diagram: remove \n inside node labels — Mermaid doesn't support them
    if data.get("diagram"):
        import re as _re
        data["diagram"] = _re.sub(
            r'\[([^\]]*)\]',
            lambda m: '[' + m.group(1).replace('\\n', ' ').replace('\n', ' ') + ']',
            data["diagram"]
        )

    should_diagram = _diagram_plan_wants_diagram(data) or _should_generate_diagram(
        source_text=text,
        summary=data.get("summary", []),
        key_concepts=data.get("key_concepts", []),
        depth=depth,
    )

    if data.get("knowledge_shape") == "none" and not _diagram_plan_wants_diagram(data):
        should_diagram = False

    # Keep diagrams only when content has clear structural signal.
    if not should_diagram:
        data["diagram"] = ""
    elif _diagram_quality_low(data.get("diagram", "")):
        # When images were provided, try a dedicated vision-to-Mermaid call before
        # falling back to the generic text-based template — it can reproduce the
        # actual architecture shown in the screenshot.
        if has_images:
            vision_diag = _vision_diagram(
                images=images,
                title=data.get("title", "Concept"),
                summary=data.get("summary", []),
            )
            data["diagram"] = vision_diag or _fallback_diagram_from_summary(
                title=data.get("title", "Concept"),
                summary=data.get("summary", []),
                key_concepts=data.get("key_concepts", []),
            )
        else:
            data["diagram"] = _fallback_diagram_from_summary(
                title=data.get("title", "Concept"),
                summary=data.get("summary", []),
                key_concepts=data.get("key_concepts", []),
            )

    return data


def _slice_content(text: str, source_url: str, existing_pages: list) -> list[dict]:
    """
    Decide whether long content should become multiple vault notes.
    Returns a list of {title, text, concept_hint} dicts — one per chapter.
    Returns a single-item list (no split) if the content is short or unified.

    Approach:
    1. Number every paragraph so the LLM can group by index — avoids fragile text matching.
    2. Ask Haiku to assign paragraphs to chapters based on conceptual coherence,
       chronological flow, and overlap with existing vault pages.
    3. Reconstruct chapter texts from the paragraph groups.
    """
    paragraphs = [p.strip() for p in _re.split(r"\n\n+", text) if p.strip()]
    no_split = [{"title": "", "text": text, "concept_hint": "", "split_mode": "none"}]

    # Don't slice short content or content with fewer than 4 paragraphs
    if len(text) < 2500 or len(paragraphs) < 4:
        return no_split

    def _structural_fallback(mode: str) -> list[dict]:
        """Split only at explicit headings or conservative paragraph size bounds."""
        heading_matches = list(_re.finditer(r"(?m)^#{1,6}\s+(.+?)\s*$", text))
        sections = []
        if len(heading_matches) >= 2:
            for i, match in enumerate(heading_matches):
                start = match.end()
                end = heading_matches[i + 1].start() if i + 1 < len(heading_matches) else len(text)
                body = text[start:end].strip()
                if body:
                    sections.append({"title": match.group(1).strip(), "text": body, "concept_hint": ""})
        if len(sections) < 2:
            sections = []
            current, current_len = [], 0
            for paragraph in paragraphs:
                if current and current_len + len(paragraph) > 4500:
                    sections.append({"title": "", "text": "\n\n".join(current), "concept_hint": ""})
                    current, current_len = [], 0
                current.append(paragraph)
                current_len += len(paragraph)
            if current:
                sections.append({"title": "", "text": "\n\n".join(current), "concept_hint": ""})
        if len(sections) < 2:
            telemetry.log_context_event("topic_split", {"mode": "none", "reason": "no_reliable_structure"})
            return no_split
        if len(sections) > 5:
            overflow = sections[4:]
            sections = sections[:4] + [{
                "title": sections[4].get("title", ""),
                "text": "\n\n".join(item["text"] for item in overflow),
                "concept_hint": "",
            }]
        for section in sections:
            section["split_mode"] = mode
        telemetry.log_context_event("topic_split", {"mode": mode, "count": len(sections)})
        return sections[:5]

    # Explicit headings are stronger evidence than an LLM guess and avoid an
    # unnecessary planner call for well-structured Markdown/HTML captures.
    heading_fallback = _structural_fallback("headings") if _re.search(r"(?m)^#{1,6}\s+", text) else None
    if heading_fallback and len(heading_fallback) > 1:
        return heading_fallback

    # Cap what we send to the LLM — beyond ~8000 chars the paragraph list itself
    # gives enough signal for boundary detection without sending the full body.
    numbered = "\n\n".join(f"[{i}] {p[:400]}" for i, p in enumerate(paragraphs[:60]))
    pages_hint = ", ".join(existing_pages[:30]) if existing_pages else "none yet"

    prompt = f"""You are organizing a long article/thread/video into coherent wiki notes.

Existing vault pages (route overlapping content to these instead of creating new ones):
{pages_hint}

Read the numbered paragraphs below and decide how to split them into 2–5 independent notes.
Each note should cover ONE distinct concept and make sense read alone.

Rules:
- Only split if there are genuinely distinct concepts. If it is all one topic, return 1 chapter.
- Preserve logical / chronological order — do not reorder paragraphs.
- If a chapter overlaps an existing vault page, set concept_hint to that page slug.
- Every paragraph index must appear in exactly one chapter.

Return ONLY a JSON array, no markdown fences:
[
  {{"title": "Short chapter title", "paragraphs": [0, 1, 2], "concept_hint": "existing-page-slug-or-empty"}},
  ...
]

Paragraphs:
{numbered}"""

    try:
        raw = llm_client.complete(
            task="slice_content",
            model=None,
            max_tokens=1000,
            messages=[{"role": "user", "content": prompt}],
            expect_json=True,
        ).strip()
        start = raw.find("[")
        end   = raw.rfind("]") + 1
        chapters = json.loads(raw[start:end]) if start >= 0 else None
    except Exception:
        return _structural_fallback("paragraph_chunks")

    if not chapters or len(chapters) <= 1:
        telemetry.log_context_event("topic_split", {"mode": "none", "reason": "unified_or_no_plan"})
        return no_split

    # A syntactically valid array is not enough: coverage must be exact so no
    # source paragraph disappears or is duplicated across child wikis.
    if not isinstance(chapters, list) or len(chapters) > 5:
        return _structural_fallback("paragraph_chunks")
    seen = []
    for chapter in chapters:
        if not isinstance(chapter, dict):
            return _structural_fallback("paragraph_chunks")
        indices = chapter.get("paragraphs")
        if not isinstance(indices, list) or not indices:
            return _structural_fallback("paragraph_chunks")
        if any(not isinstance(index, int) or index < 0 or index >= len(paragraphs) for index in indices):
            return _structural_fallback("paragraph_chunks")
        seen.extend(indices)
    if sorted(seen) != list(range(len(paragraphs))) or len(set(seen)) != len(seen):
        return _structural_fallback("paragraph_chunks")

    # Reconstruct full chapter texts (use original paragraphs, not the truncated preview)
    result = []
    for ch in chapters:
        indices = [i for i in (ch.get("paragraphs") or []) if isinstance(i, int) and i < len(paragraphs)]
        if not indices:
            continue
        chapter_text = "\n\n".join(paragraphs[i] for i in sorted(indices))
        if chapter_text.strip():
            result.append({
                "title": (ch.get("title") or "").strip(),
                "text":  chapter_text,
                "concept_hint": (ch.get("concept_hint") or "").strip(),
                "split_mode": "llm",
            })

    if len(result) > 5:
        overflow = result[4:]
        result = result[:4] + [{
            "title": overflow[0].get("title", ""),
            "text": "\n\n".join(item["text"] for item in overflow),
            "concept_hint": "",
            "split_mode": "llm",
        }]
    if len(result) > 1:
        telemetry.log_context_event("topic_split", {"mode": "llm", "count": len(result)})
        return result
    return _structural_fallback("paragraph_chunks")


def _safe_node_label(text: str, max_words: int = 6, max_chars: int = 44) -> str:
    # Treat em-dash/en-dash as a natural break — take only the part before it
    for dash in (" — ", " – ", " - "):
        if dash in (text or ""):
            text = text.split(dash)[0]
            break
    # Strip Mermaid-unsafe chars
    cleaned = _re.sub(r"[*_`#\[\]{}()<>|:\"']", " ", text or "")
    cleaned = _re.sub(r"\s+", " ", cleaned).strip()
    words = cleaned.split()[:max_words]
    short = " ".join(words)[:max_chars].strip()
    # Strip trailing punctuation that makes labels look mid-sentence
    short = short.rstrip(",:;–—-")
    return short or "Insight"


def _should_generate_diagram(
    source_text: str,
    summary: list[str],
    key_concepts: list[str],
    depth: str,
) -> bool:
    if depth == "short":
        return False

    joined = " ".join(
        [source_text[:5000]] + [str(s) for s in (summary or [])] + [str(k) for k in (key_concepts or [])]
    ).lower()
    tokens = set(_re.findall(r"[a-z0-9\-]+", joined))

    structure_keywords = {
        "architecture", "pipeline", "workflow", "flow", "tier", "layer", "system",
        "service", "server", "client", "api", "gateway", "queue", "broker",
        "database", "storage", "cache", "ingestion", "retrieval", "embedding",
        "inference", "agent", "orchestrator", "router", "scheduler", "monitoring",
    }
    flow_verbs = {
        "sends", "routes", "calls", "writes", "reads", "returns", "processes",
        "fetches", "stores", "retrieves", "syncs", "triggers", "transforms",
    }

    kw_hits = len(tokens.intersection(structure_keywords))
    verb_hits = len(tokens.intersection(flow_verbs))
    signal = kw_hits * 2 + verb_hits

    # Medium content needs stronger evidence; long-form tolerates lower signal.
    threshold = 7 if depth == "medium" else 5
    return signal >= threshold


def _diagram_quality_low(diagram: str) -> bool:
    if not diagram:
        return True
    lines = [ln.strip() for ln in diagram.splitlines() if ln.strip()]
    edge_lines = [ln for ln in lines if "-->" in ln or "---" in ln]
    node_ids = set(_re.findall(r"\b[A-Za-z][A-Za-z0-9_]*\b(?=\s*\[)", diagram))
    return len(edge_lines) < 4 or len(node_ids) < 4


def _distill_bullet(text: str) -> str:
    """Strip 'I learned that' style openers so bullets work as diagram labels."""
    text = str(text).strip()
    for opener in ("I learned that ", "This shows that ", "I learned ", "This shows "):
        if text.startswith(opener):
            text = text[len(opener):]
            break
    return text


def _vision_diagram(images: list, title: str, summary: list) -> str:
    """
    Dedicated Sonnet call: look at the image(s) and reproduce the visual
    architecture/topology as a Mermaid diagram.  Used when the extraction
    pass produced a low-quality diagram despite having image input.
    """
    if not images:
        return ""

    content: list = []
    for img in images[:3]:
        content.append({
            "type": "image",
            "source": {"type": "base64",
                       "media_type": img.get("mediaType", "image/png"),
                       "data": img["data"]},
        })

    bullets = "\n".join(f"- {s}" for s in (summary or []))
    content.append({"type": "text", "text": f"""Convert the visual diagram/architecture in the image(s) into a Mermaid flowchart.

Topic: {title}
Context bullets:
{bullets}

Rules:
- Use the actual node labels, component names, and arrow directions you see in the image
- If the image shows GPU 0..N, write those nodes. If it shows a parameter server, name it that.
- flowchart LR (left-to-right) or TD (top-down) — match the image orientation
- Up to 14 nodes if necessary to be accurate
- Node labels: 2-5 words max, no special chars (no quotes, colons, brackets inside labels)
- If the image has no structural diagram, return exactly: NONE

Return ONLY the raw Mermaid code (no markdown fences, no explanation)."""})

    try:
        raw = llm_client.complete(
            task="diagram",
            model=None,  # routes to vision model for active provider (gemini-2.5-flash or claude-sonnet)
            max_tokens=600,
            messages=[{"role": "user", "content": content}],
        ).strip()
        raw = _re.sub(r"^```(?:mermaid)?\s*", "", raw)
        raw = _re.sub(r"\s*```$", "", raw).strip()
        if raw and raw != "NONE" and not _diagram_quality_low(raw):
            return raw
    except Exception:
        pass
    return ""


def _llm_diagram(title: str, summary: list, key_concepts: list) -> str:
    """
    Ask Haiku to generate a Mermaid diagram appropriate for the content structure.
    Detects sequential pipelines, taxonomies, architectures, etc.
    Falls back to _fallback_diagram_from_summary if LLM output is unusable.
    """
    bullets = "\n".join(f"- {s}" for s in (summary or []))
    concepts = ", ".join(str(c) for c in (key_concepts or []))
    prompt = f"""You are generating a Mermaid flowchart for a personal knowledge wiki page.

Title: {title}
Key concepts: {concepts}
Summary bullets:
{bullets}

Instructions:
- Detect the structure type first:
  * SEQUENTIAL PIPELINE (numbered steps, "then", "next", "followed by", "step N"): use a left-to-right chain — A --> B --> C --> D
  * HIERARCHY / TAXONOMY (types, categories, "types of", "kinds of"): use a top-down tree
  * ARCHITECTURE (components, services, layers, tiers): use Input→Processing→Output tiers
  * COMPARISON (X vs Y, tradeoffs): use two parallel columns
- flowchart LR (or TD for hierarchies)
- 5-8 nodes max, labels 2-4 words each
- No special chars in labels: no quotes, colons, semicolons, pipes, brackets inside labels
- Return ONLY the raw Mermaid code, nothing else, no markdown fences

Mermaid code:"""

    try:
        raw = llm_client.complete(
            task="diagram",
            model=None,
            max_tokens=400,
            messages=[{"role": "user", "content": prompt}],
        ).strip()
        # Strip markdown fences if present
        raw = _re.sub(r"^```(?:mermaid)?\s*", "", raw)
        raw = _re.sub(r"\s*```$", "", raw).strip()
        if raw and not _diagram_quality_low(raw):
            return raw
    except Exception:
        pass
    return _fallback_diagram_from_summary(title, summary, key_concepts)


def _fallback_diagram_from_summary(title: str, summary: list, key_concepts: list) -> str:
    center = _safe_node_label(title, max_words=5, max_chars=36)
    # Processing Tier: key_concepts are short names — far better than full sentences
    concepts = [_safe_node_label(c, max_words=4, max_chars=30) for c in (key_concepts or []) if str(c).strip()]
    # Output Tier: distilled summary phrases (opener stripped)
    outcomes = [
        _safe_node_label(_distill_bullet(s), max_words=5, max_chars=32)
        for s in (summary or []) if str(s).strip()
    ]

    context = " ".join([title or ""] + [str(x) for x in (summary or [])] + [str(x) for x in (key_concepts or [])]).lower()
    has_web_stack = any(k in context for k in ["dns", "web server", "browser", "mobile", "api"]) and "database" in context
    has_db_taxonomy = any(k in context for k in ["relational", "nosql", "mysql", "postgres", "cassandra", "dynamodb"])

    if has_web_stack and has_db_taxonomy:
        return "\n".join([
            "flowchart LR",
            '  U["User Browser or App"] --> D["DNS Lookup"]',
            '  D --> W["Web API Server"]',
            '  W <--> DB["Database Tier"]',
            '  DB --> CH["DB Choice"]',
            '  CH --> R["Relational SQL"]',
            '  CH --> N["NoSQL"]',
        ])

    # Pair P-nodes with O-nodes exactly — prevents orphan nodes that Mermaid
    # renders as floating ghosts outside subgraphs.
    n = min(len(concepts), len(outcomes), 4)
    p_nodes = concepts[:n] if n else ["Key concepts"]
    o_nodes = outcomes[:n] if n else (outcomes[:1] if outcomes else ["Key outcomes"])
    n = len(p_nodes)

    lines = [
        "flowchart LR",
        '  subgraph IN["Input Tier"]',
        f'    C["{center}"]',
        "  end",
        '  subgraph P["Processing Tier"]',
    ]
    for i, b in enumerate(p_nodes, 1):
        lines.append(f'    P{i}["{b}"]')
    lines.append("  end")
    lines.append('  subgraph O["Output Tier"]')
    for i, c in enumerate(o_nodes, 1):
        lines.append(f'    O{i}["{c}"]')
    lines.append("  end")

    for i in range(1, n + 1):
        lines.append(f"  C --> P{i}")
    for i in range(1, n + 1):
        lines.append(f"  P{i} --> O{i}")

    return "\n".join(lines)


# ── /queue-url (iOS Share Sheet fast-path) ───────────────────────────────────

async def _background_extract(item_id: str, url: str) -> None:
    """
    Run fetch + Sonnet extraction in the background after /queue-url returns.
    Updates the queue item in-place so it's ready to review when the user opens the UI.
    On failure, marks the item with extraction_error so the UI can show a retry option.
    """
    loop = asyncio.get_running_loop()
    try:
        # Run blocking I/O in a thread so we don't block the event loop
        raw_text = await loop.run_in_executor(None, _fetch_url, url)
        existing_pages = [p["name"] for p in vault_reader.list_concept_pages()]
        extraction = await loop.run_in_executor(
            None, _extract_with_sonnet, raw_text, [], url, existing_pages
        )
        item = queue_manager.get_by_id(item_id)
        if item:
            item.update({
                "title": extraction["title"],
                "key_concepts": extraction.get("key_concepts", []),
                "summary": extraction["summary"],
                "suggested_page": extraction["suggested_page"],
                "suggested_wikilinks": extraction.get("suggested_wikilinks", []),
                "tags": extraction.get("tags", []),
                "references": extraction.get("references", []),
                "diagram": extraction.get("diagram", ""),
                **_curation_fields(extraction),
                "pending_extraction": False,
                "extraction_error": None,
                "extracted_at": datetime.now().isoformat(),
            })
            queue_manager.update(item_id, item)
    except Exception as e:
        # Mark as failed so the UI shows a retry option instead of spinning forever
        item = queue_manager.get_by_id(item_id)
        if item:
            item.update({
                "pending_extraction": False,
                "extraction_error": str(e),
                "summary": [f"Extraction failed: {e}"],
                "title": item.get("url", url),
            })
            queue_manager.update(item_id, item)


@app.post("/queue-url")
async def queue_url(req: IngestRequest):
    """
    Lightweight endpoint for the iOS Share Sheet.
    Queues a URL instantly (no fetch, no LLM) so the shortcut
    gets a response in <1s. Extraction runs in the background so the
    item is ready to review by the time the user opens the web UI.
    """
    url = (req.url or "").strip()
    if not url:
        raise HTTPException(400, "url is required")

    # Dedup — don't queue the same URL twice
    if not req.force:
        duplicate = _find_duplicate(url)
        if duplicate:
            return {"queued": False, "reason": f"already ingested: {duplicate}"}

    item_id = str(uuid.uuid4())
    item = {
        "id": item_id,
        "url": url,
        "title": url,                      # placeholder — replaced by background task
        "key_concepts": [],
        "summary": ["Extracting in background…"],
        "suggested_page": "unprocessed",
        "suggested_wikilinks": [],
        "tags": [],
        "diagram": "",
        "source_verdict": "ingest",
        "educational_core": [],
        "discarded_context": [],
        "knowledge_shape": "none",
        "diagram_plan": {"needed": False, "type": "none", "reason": "Extraction pending."},
        "pending_extraction": True,
        "queued_at": datetime.now().isoformat(),
    }
    queue_manager.enqueue(item)

    # Fire-and-forget background extraction — does not block the response
    asyncio.create_task(_background_extract(item_id, url))

    return {"queued": True, "id": item_id, "url": url}


# ── /ingest-direct ───────────────────────────────────────────────────────────

@app.post("/ingest-direct")
async def ingest_direct(req: IngestRequest):
    """
    Extract and immediately write to vault — no HITL queue.
    Used by the 'Quick save' button and iOS shortcut when the user
    has already decided to save and doesn't need a review step.
    """
    if not req.url and not req.text and not req.image_base64 and not req.images:
        raise HTTPException(400, "Provide url, text, image_base64, or images")

    source_url = req.url or ""

    if source_url and not req.force:
        duplicate = _find_duplicate(source_url)
        if duplicate:
            raise HTTPException(409, f"Already ingested: {duplicate}")

    loop = asyncio.get_running_loop()
    raw_text = await loop.run_in_executor(None, _fetch_url, req.url) if req.url else ""
    if req.text:
        raw_text = (raw_text + "\n\n" + req.text).strip()

    all_images = list(req.images or [])
    if req.image_base64 and not all_images:
        all_images = [{"data": req.image_base64, "mediaType": "image/png"}]

    existing_pages = [p["name"] for p in vault_reader.list_concept_pages()]
    extraction = await loop.run_in_executor(
        None, _extract_with_sonnet, raw_text, all_images, source_url, existing_pages
    )

    item = {
        "id": str(uuid.uuid4()),
        "url": source_url,
        "title": extraction["title"],
        "key_concepts": extraction["key_concepts"],
        "summary": extraction["summary"],
        "suggested_page": extraction["suggested_page"],
        "suggested_wikilinks": extraction["suggested_wikilinks"],
        "tags": extraction["tags"],
        "references": extraction.get("references", []),
        "diagram": extraction.get("diagram", ""),
        **_curation_fields(extraction),
        "staged_at": datetime.now().isoformat(),
        "status": "approved",
    }

    try:
        file_path = wiki_writer.write_approved(item)
    except Exception as e:
        raise HTTPException(500, f"Vault write failed: {e}")

    try:
        wiki_writer.fix_page_wikilinks(Path(file_path).stem)
    except Exception:
        pass
    try:
        memory_store.index_page(Path(file_path).stem)
    except Exception as e:
        logger.warning("memory index update failed for %s: %s", file_path, e)

    try:
        api_key = os.environ.get("ANTHROPIC_API_KEY", "")
        tag_classifier.classify_new_tags(item.get("tags", []), api_key)
    except Exception:
        pass

    return {
        "success": True,
        "file_written": file_path,
        "title": item["title"],
        "suggested_page": item["suggested_page"],
        "tags": item["tags"],
    }


# ── /queue ───────────────────────────────────────────────────────────────────

@app.get("/queue")
async def get_queue():
    return {"items": queue_manager.get_all()}


@app.get("/queue-status")
async def queue_status(ids: str):
    """Resolve the outcome of previously staged clip ids: pending, saved, rejected, or unknown.

    Approve/reject remove an item from the queue, so once it's gone the only
    record of what happened is its trace in traces.jsonl (item_id now logged
    there for exactly this lookup).
    """
    requested = [item_id.strip() for item_id in ids.split(",") if item_id.strip()]
    results: dict[str, dict] = {item_id: {"status": "unknown"} for item_id in requested}

    for item_id in requested:
        if queue_manager.get_by_id(item_id):
            results[item_id] = {"status": "pending"}

    remaining = {item_id for item_id in requested if results[item_id]["status"] == "unknown"}
    if remaining:
        vault_path = Path(os.environ.get("VAULT_PATH", "/Users/sakethv7/SakethVault"))
        traces_path = vault_path / "_wiki" / "meta" / "traces.jsonl"
        if traces_path.exists():
            with open(traces_path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        trace = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    trace_id = trace.get("item_id")
                    if trace_id in remaining:
                        if trace.get("approved"):
                            results[trace_id] = {
                                "status": "saved",
                                "final_page": trace.get("final_page", ""),
                            }
                        else:
                            results[trace_id] = {"status": "rejected"}
                        remaining.discard(trace_id)
                    if not remaining:
                        break

    return {"results": results}


@app.post("/queue/regenerate/{item_id}")
async def queue_regenerate(item_id: str, req: RegenerateRequest):
    item = queue_manager.get_by_id(item_id)
    if not item:
        raise HTTPException(404, f"Item {item_id} not found in queue")
    mode = (req.mode or "full").strip().lower()
    if mode not in {"full", "diagram"}:
        raise HTTPException(400, "mode must be one of: full, diagram")

    loop = asyncio.get_running_loop()
    updated = await loop.run_in_executor(None, lambda: _regen_item(item, mode=mode))
    ok = queue_manager.update(item_id, updated)
    if not ok:
        raise HTTPException(500, f"Failed to update queue item {item_id}")

    return {
        "success": True,
        "mode": mode,
        "id": item_id,
        "diff_preview": {
            "title": updated.get("title", ""),
            "summary": updated.get("summary", []),
            "suggested_page": updated.get("suggested_page", ""),
            "suggested_wikilinks": updated.get("suggested_wikilinks", []),
            "tags": updated.get("tags", []),
            "key_concepts": updated.get("key_concepts", []),
            "references": updated.get("references", []),
            "diagram": updated.get("diagram", ""),
            **_curation_fields(updated),
        },
    }


@app.post("/queue/{item_id}/diagram-regenerations")
async def create_diagram_regeneration(item_id: str, req: DiagramRegenerationRequest):
    """Create a validated candidate without changing the selected queue draft."""
    if req.intent not in DIAGRAM_INTENTS:
        raise HTTPException(422, "intent must be faithful_source, explain_mechanism, or compare_alternatives")
    if not req.idempotency_key.strip():
        raise HTTPException(422, "idempotency_key is required")
    if len(req.feedback) > 500:
        raise HTTPException(422, "feedback must be at most 500 characters")
    item = queue_manager.get_by_id(item_id)
    if not item:
        raise HTTPException(404, f"Item {item_id} not found in queue")
    if int(item.get("revision", 0)) != req.expected_revision:
        raise HTTPException(409, detail={"message": "Queue item changed; refresh before regenerating.", "item": item})
    for candidate in item.get("diagram_revisions", []):
        if candidate.get("idempotency_key") == req.idempotency_key:
            return {"id": item_id, "revision": item.get("revision", 0), "candidate": candidate,
                    "selected_candidate_id": item.get("selected_diagram_candidate_id")}

    evidence, images = _diagram_evidence(item)
    if not evidence and not images:
        raise HTTPException(422, "This queued item has no retained source evidence; use full regeneration or edit manually.")
    plan = _regeneration_plan(item, evidence, req.intent)
    try:
        diagram = _generate_evidence_diagram(item, evidence, images, plan, req.intent, req.feedback.strip())
    except Exception as exc:
        logger.warning("diagram regeneration failed for %s: %s", item_id, exc)
        raise HTTPException(503, "Diagram generation is unavailable; the current draft was unchanged.")
    validation = _validate_diagram_candidate(diagram, plan, evidence, item.get("key_concepts", []))
    if not validation.get("selectable"):
        raise HTTPException(422, detail={"message": "Candidate failed validation; the current draft was unchanged.",
                                         "validation": validation})
    candidate = {
        "id": str(uuid.uuid4()), "idempotency_key": req.idempotency_key,
        "parent_revision": req.expected_revision, "created_at": datetime.now().isoformat(),
        "intent": req.intent, "feedback": req.feedback.strip(),
        "evidence_hash": hashlib.sha256((evidence + str(len(images))).encode()).hexdigest(),
        "diagram_plan": plan, "diagram": diagram, "validation": validation,
        "generator": {"prompt_version": "evidence-diagram-v1"},
    }
    updated = dict(item)
    updated["diagram_revisions"] = [*item.get("diagram_revisions", []), candidate]
    updated["revision"] = req.expected_revision + 1
    ok, current = queue_manager.update_if_revision(item_id, req.expected_revision, updated)
    if not ok:
        if current is None:
            raise HTTPException(404, f"Item {item_id} not found in queue")
        raise HTTPException(409, detail={"message": "Queue item changed; refresh before regenerating.", "item": current})
    telemetry.log_context_event("diagram_regeneration", {
        "item_id": item_id, "intent": req.intent, "plan_type": plan.get("type"),
        "evidence_hash": candidate["evidence_hash"], "validation": validation,
    })
    return {"id": item_id, "revision": updated["revision"], "candidate": candidate,
            "selected_candidate_id": updated.get("selected_diagram_candidate_id")}


@app.post("/queue/{item_id}/diagram-selection")
async def select_diagram_candidate(item_id: str, req: DiagramSelectionRequest):
    item = queue_manager.get_by_id(item_id)
    if not item:
        raise HTTPException(404, f"Item {item_id} not found in queue")
    if int(item.get("revision", 0)) != req.expected_revision:
        raise HTTPException(409, detail={"message": "Queue item changed; refresh before selecting.", "item": item})
    candidate = next((c for c in item.get("diagram_revisions", []) if c.get("id") == req.candidate_id), None)
    if not candidate:
        raise HTTPException(404, "Diagram candidate not found")
    if not candidate.get("validation", {}).get("selectable"):
        raise HTTPException(422, "Only validated diagram candidates can be selected")
    updated = dict(item)
    updated["diagram"] = candidate.get("diagram", "")
    updated["diagram_plan"] = candidate.get("diagram_plan", {})
    updated["selected_diagram_candidate_id"] = candidate["id"]
    updated["revision"] = req.expected_revision + 1
    ok, current = queue_manager.update_if_revision(item_id, req.expected_revision, updated)
    if not ok:
        if current is None:
            raise HTTPException(404, f"Item {item_id} not found in queue")
        raise HTTPException(409, detail={"message": "Queue item changed; refresh before selecting.", "item": current})
    return {"id": item_id, "revision": updated["revision"], "diff_preview": {
        "diagram": updated["diagram"], "diagram_plan": updated["diagram_plan"],
        "selected_diagram_candidate_id": updated["selected_diagram_candidate_id"],
    }}


# ── queue decisions ───────────────────────────────────────────────────────────

async def _decide_queue_item(item_id: str, req: ApproveRequest):
    item = queue_manager.get_by_id(item_id)
    if not item:
        raise HTTPException(404, f"Item {item_id} not found in queue")

    if not req.approved:
        queue_manager.remove(item_id)
        try:
            trace = {
                "ts": datetime.now().isoformat(),
                "item_id": item_id,
                "url": item.get("url", ""),
                "source_type": item.get("source_type") or ("tweet" if _is_tweet_url(item.get("url", "")) else ("text" if not item.get("url") else "url")),
                "approved": False,
                "title": item.get("title", ""),
                "summary": item.get("summary", [])[:6],
                "key_concepts": item.get("key_concepts", [])[:8],
                **_curation_fields(item),
                "diagram": item.get("diagram", ""),
                "suggested_page": item.get("suggested_page", ""),
                "final_page": None,
                "page_corrected": False,
                "evolution_type": None,
                "was_duplicate": False,
                "tags_suggested": item.get("tags", []),
                "tags_final": [],
                "tags_corrected": False,
                "wikilinks_suggested": item.get("suggested_wikilinks", []),
                "deep_dive": False,
            }
            _append_trace(trace)
            preference_memory.record_approval_trace(trace)
        except Exception:
            pass
        return {"success": True, "action": "rejected", "file_written": None}

    # If queued via Share Sheet (pending_extraction=True), run extraction now
    if item.get("pending_extraction") and item.get("url"):
        try:
            _approve_loop = asyncio.get_running_loop()
            raw_text = await _approve_loop.run_in_executor(None, _fetch_url, item["url"])
            existing_pages = [p["name"] for p in vault_reader.list_concept_pages()]
            extraction = await _approve_loop.run_in_executor(
                None, _extract_with_sonnet, raw_text, [], item["url"], existing_pages
            )
            item.update({
                "title": extraction["title"],
                "key_concepts": extraction.get("key_concepts", []),
                "summary": extraction["summary"],
                "suggested_page": extraction["suggested_page"],
                "suggested_wikilinks": extraction.get("suggested_wikilinks", []),
                "tags": extraction.get("tags", []),
                "references": extraction.get("references", []),
                "diagram": extraction.get("diagram", ""),
                **_curation_fields(extraction),
                "pending_extraction": False,
            })
        except Exception as _e:
            logger.warning("background extraction failed for item %s: %s", item_id, _e)

    # Snapshot original values before edits (for trace logging)
    item["_original_suggested_page"] = item.get("suggested_page", "")
    item["_original_tags"] = list(item.get("tags", []))

    # Merge any human edits onto the item before writing
    if req.edits:
        allowed = {
            "title", "summary", "suggested_page", "suggested_wikilinks", "tags",
            "diagram", "source_verdict", "educational_core", "discarded_context",
            "knowledge_shape", "diagram_plan",
        }
        for k, v in req.edits.items():
            if k in allowed and v is not None:
                item[k] = v
        item.update(_normalize_extraction_contract(item, depth="medium"))
    item["suggested_page"] = identity.resolve_slug(item.get("suggested_page", "general"))
    item["suggested_wikilinks"] = [
        identity.resolve_slug(link) for link in item.get("suggested_wikilinks", [])
    ]

    # Write to vault (atomic — either fully succeeds or raises, queue untouched)
    try:
        file_path = wiki_writer.write_approved(item)
    except Exception as e:
        raise HTTPException(500, f"Vault write failed: {e}")

    # Only remove from queue after successful vault write
    queue_manager.remove(item_id)

    # Auto-fix wikilinks on the written page (kebab-case normalization)
    page_name = Path(file_path).stem
    try:
        wiki_writer.fix_page_wikilinks(page_name)
    except Exception as _e:
        logger.warning("fix_page_wikilinks failed for %s: %s", page_name, _e)
    try:
        memory_store.index_page(page_name)
    except Exception as _e:
        logger.warning("memory index update failed for %s: %s", page_name, _e)

    try:
        _record_processed_clip(item, file_path)
    except Exception as _e:
        logger.warning("_record_processed_clip failed for %s: %s", item_id, _e)

    # Classify any new tags so the frontend has colors for them
    try:
        api_key = os.environ.get("ANTHROPIC_API_KEY", "")
        tag_classifier.classify_new_tags(item.get("tags", []), api_key)
    except Exception as _e:
        logger.warning("tag_classifier failed: %s", _e)

    # If deep-dive requested, add tag to the written page's frontmatter
    deep_dive_tagged = False
    if req.open_thread:
        try:
            _add_deep_dive_tag(file_path)
            deep_dive_tagged = True
        except Exception as _e:
            logger.warning("_add_deep_dive_tag failed for %s: %s", file_path, _e)

    evolution = item.get("_evolution", {})

    # Append trace for self-learning loop
    try:
        trace = {
            "ts": datetime.now().isoformat(),
            "item_id": item_id,
            "url": item.get("url", ""),
            "source_type": item.get("source_type") or ("tweet" if _is_tweet_url(item.get("url", "")) else ("text" if not item.get("url") else "url")),
            "approved": True,
            "title": item.get("title", ""),
            "summary": item.get("summary", [])[:6],
            "key_concepts": item.get("key_concepts", [])[:8],
            **_curation_fields(item),
            "diagram": item.get("diagram", ""),
            "suggested_page": item.get("_original_suggested_page", item.get("suggested_page", "")),
            "final_page": item.get("suggested_page", ""),
            "page_corrected": item.get("_original_suggested_page", item.get("suggested_page", "")) != item.get("suggested_page", ""),
            "evolution_type": evolution.get("evolution_type", "extends"),
            "was_duplicate": evolution.get("evolution_type") == "duplicates",
            "tags_suggested": item.get("_original_tags", item.get("tags", [])),
            "tags_final": item.get("tags", []),
            "tags_corrected": item.get("_original_tags", item.get("tags", [])) != item.get("tags", []),
            "wikilinks_suggested": item.get("suggested_wikilinks", []),
            "deep_dive": deep_dive_tagged,
        }
        _append_trace(trace)
        preference_memory.record_approval_trace(trace)
    except Exception as _e:
        logger.warning("_append_trace failed on approve: %s", _e)

    return {
        "success": True,
        "action": "approved",
        "file_written": file_path,
        "evolution_type": evolution.get("evolution_type", "extends"),
        "evolution_reason": evolution.get("evolution_reason", ""),
        **({"deep_dive_tagged": True} if deep_dive_tagged else {}),
    }


@app.post("/approve/{item_id}")
async def approve(item_id: str, req: ApproveRequest):
    return await _decide_queue_item(item_id, req)


@app.post("/queue/batch-decision")
async def batch_queue_decision(req: BatchDecisionRequest):
    item_ids = [item_id.strip() for item_id in req.item_ids if isinstance(item_id, str) and item_id.strip()]
    if not 1 <= len(item_ids) <= 100:
        raise HTTPException(422, "item_ids must contain between 1 and 100 non-empty IDs")
    if len(item_ids) != len(req.item_ids):
        raise HTTPException(422, "item_ids must not contain empty values")
    if len(set(item_ids)) != len(item_ids):
        raise HTTPException(422, "item_ids must be unique")

    results = []
    for item_id in item_ids:
        item = queue_manager.get_by_id(item_id)
        if req.approved and item and (item.get("pending_extraction") or item.get("extraction_error")):
            results.append({
                "item_id": item_id,
                "success": False,
                "code": "not_ready",
                "message": f"Item {item_id} is not ready for approval",
            })
            continue
        try:
            result = await _decide_queue_item(item_id, ApproveRequest(approved=req.approved))
            results.append({"item_id": item_id, **result})
        except HTTPException as exc:
            code = "not_found" if exc.status_code == 404 else "decision_failed"
            results.append({
                "item_id": item_id,
                "success": False,
                "code": code,
                "message": str(exc.detail),
            })
        except Exception as exc:
            logger.exception("Batch queue decision failed for %s", item_id)
            results.append({
                "item_id": item_id,
                "success": False,
                "code": "decision_failed",
                "message": str(exc),
            })

    completed_count = sum(1 for result in results if result.get("success"))
    failed_count = len(results) - completed_count
    return {
        "success": failed_count == 0,
        "decision": "approved" if req.approved else "rejected",
        "requested_count": len(item_ids),
        "completed_count": completed_count,
        "failed_count": failed_count,
        "results": results,
    }


# ── /chat ────────────────────────────────────────────────────────────────────

_KNOWLEDGE_QUERY_RE = _re.compile(
    r"\b(what (do i|have i|did i) (know|learn|understand|capture)|"
    r"what('s| is) my (understanding|knowledge) (of|about|on)|"
    r"how (well |much )?do i (know|understand)|"
    r"summarize (my|what i know about)|"
    r"what (have i (read|captured|saved)|do i have) (on|about))\b",
    _re.IGNORECASE,
)

def _is_knowledge_query(msg: str) -> bool:
    return bool(_KNOWLEDGE_QUERY_RE.search(msg))

def _extract_topic(msg: str, relevant_names: list) -> Optional[str]:
    """Return the best concept page slug for a knowledge query.
    Prefers pages whose slug words appear directly in the message."""
    if not relevant_names:
        return None
    alias_topic = identity.resolve_slug(msg)
    if alias_topic in relevant_names:
        return alias_topic
    msg_lower = msg.lower()
    # Score each candidate: how many slug words appear in the message
    def slug_score(name: str) -> int:
        words = name.replace("-", " ").split()
        return sum(1 for w in words if w in msg_lower and len(w) > 2)
    scored = sorted(relevant_names, key=slug_score, reverse=True)
    return scored[0]


@app.post("/chat")
async def chat(req: ChatRequest):
    if not req.message or not req.message.strip():
        raise HTTPException(400, "message cannot be empty")

    loop = asyncio.get_event_loop()
    memory_hits = []
    try:
        memory_hits = await loop.run_in_executor(None, memory_store.search, req.message, RAG_TOP_K)
    except Exception as e:
        logger.warning("memory search failed; falling back to keyword match: %s", e)

    relevant_names = [hit["page_name"] for hit in memory_hits]

    # Single fallback: keyword scan with synonym expansion. No LLM, no index.
    if not relevant_names:
        relevant_names = await loop.run_in_executor(
            None, vault_reader.find_relevant_pages, req.message
        )

    context_budget = _chat_context_budget()
    context_parts = []
    retrieved_chunks_total = 0
    retrieved_chunks_used = 0
    retrieved_chunks_dropped = 0
    context_chars_dropped = 0
    if memory_hits:
        for hit in memory_hits[:RAG_TOP_K]:
            snippet_lines = [f"- {snippet}" for snippet in hit.get("snippets", []) if snippet]
            retrieved_chunks_total += len(snippet_lines)
            headings = ", ".join(hit.get("headings", []))
            section = "\n".join(
                part
                for part in [
                    f"=== [[{hit['page_name']}]] ({hit['folder']}) ===",
                    f"Current understanding: {hit.get('current_understanding', '')}".strip(),
                    f"Relevant headings: {headings}" if headings else "",
                    *snippet_lines,
                ]
                if part
            )
            current_len = len("\n\n".join(context_parts))
            remaining = context_budget - current_len - (2 if context_parts else 0)
            if remaining <= 0:
                retrieved_chunks_dropped += len(snippet_lines)
                context_chars_dropped += len(section)
                continue
            if len(section) > remaining:
                context_parts.append(section[:remaining].rstrip())
                retrieved_chunks_used += len(snippet_lines)
                context_chars_dropped += len(section) - remaining
                if snippet_lines:
                    retrieved_chunks_dropped += 1
                break
            context_parts.append(section)
            retrieved_chunks_used += len(snippet_lines)
        if len(memory_hits) > RAG_TOP_K:
            retrieved_chunks_dropped += sum(
                len(hit.get("snippets", []) or []) for hit in memory_hits[RAG_TOP_K:]
            )
    else:
        retrieved_chunks_total = len(relevant_names)
        retrieved_chunks_dropped = max(0, len(relevant_names) - RAG_TOP_K)
        pages_content = vault_reader.read_pages_content(relevant_names[:RAG_TOP_K])
        n_pages = len(pages_content)
        per_page = context_budget // n_pages if n_pages else context_budget
        for name, content in pages_content.items():
            body = _strip_frontmatter(content)
            used_body = body[:per_page]
            context_parts.append(f"=== [[{name}]] ===\n{used_body}")
            retrieved_chunks_used += 1
            context_chars_dropped += max(0, len(body) - len(used_body))
    context = "\n\n".join(context_parts) if context_parts else "No matching pages found yet."
    telemetry.log_context_event(
        "chat_context",
        {
            "task": "chat_answer",
            "query": req.message,
            "memory_hits": len(memory_hits),
            "pages_read": relevant_names[:RAG_TOP_K],
            "retrieved_chunks_total": retrieved_chunks_total,
            "retrieved_chunks_used": retrieved_chunks_used,
            "retrieved_chunks_dropped": retrieved_chunks_dropped,
            "context_chars_used": len(context),
            "context_chars_dropped": context_chars_dropped,
            "context_budget": context_budget,
        },
    )

    index_content = vault_reader.read_index()
    chat_preferences = preference_memory.chat_hints()
    preference_system_text = (
        f"\n\nUser response preferences:\n{chat_preferences}"
        if chat_preferences else ""
    )

    # Prompt caching: system block cached across calls
    system_block = [
        {
            "type": "text",
            "text": (
                "You are Saketh's personal AI knowledge assistant for his SakethWiki"
                " — a second brain covering CS, AI/ML, DSA, system design, humanities, science, and more.\n\n"
                "Answer from the wiki pages only. Be direct and specific. Use [[PageName]] notation."
                " If the wiki doesn't cover the topic, say so clearly and suggest what to capture.\n\n"
                f"{preference_system_text}\n\n"
                f"Wiki index:\n{index_content[:800]}"
            ),
            "cache_control": {"type": "ephemeral"},
        }
    ]

    history_messages = []
    for turn in req.history:
        history_messages.append({"role": turn["role"], "content": turn["content"]})

    user_content = [
        {
            "type": "text",
            "text": f"Relevant wiki pages:\n{context}",
            "cache_control": {"type": "ephemeral"},
        },
        {
            "type": "text",
            "text": req.message,
        },
    ]
    history_messages.append({"role": "user", "content": user_content})

    answer = llm_client.complete(
        task="chat_answer",
        model=None,
        max_tokens=1200,
        system=system_block,
        messages=history_messages,
    )

    # Attach structured knowledge card for self-knowledge queries
    knowledge_card = None
    if _is_knowledge_query(req.message):
        concept_candidates = [
            hit["page_name"]
            for hit in memory_hits
            if hit.get("folder") in {"cs", "science", "humanities"}
        ]
        topic = _extract_topic(req.message, concept_candidates or relevant_names)
        if topic:
            knowledge_card = vault_reader.parse_concept_page(topic)

    if memory_hits:
        source_paths = [f"_wiki/{hit['folder']}/{hit['page_name']}.md" for hit in memory_hits]
    else:
        folder_by_page = {page["name"]: page["folder"] for page in vault_reader.list_concept_pages()}
        source_paths = [f"_wiki/{folder_by_page.get(name, 'cs')}/{name}.md" for name in relevant_names]

    return {
        "answer": answer,
        "sources": source_paths,
        "pages_read": relevant_names,
        **({"knowledge_card": knowledge_card} if knowledge_card else {}),
    }


@app.post("/chat-notes")
async def add_chat_note(req: ChatNoteRequest):
    note_type = (req.note_type or "").strip().lower()
    note = (req.note or "").strip()
    if note_type not in CHAT_NOTE_TYPES:
        raise HTTPException(400, f"note_type must be one of {sorted(CHAT_NOTE_TYPES)}")
    if not note:
        raise HTTPException(400, "note cannot be empty")

    pages_read = [
        identity.resolve_slug(str(page))
        for page in (req.pages_read or [])
        if str(page).strip()
    ][:12]
    sources = [str(source).strip() for source in (req.sources or []) if str(source).strip()][:12]
    trace_id = f"chat-note-{uuid.uuid4().hex[:12]}"
    trace = {
        "id": trace_id,
        "ts": datetime.now().isoformat(),
        "event_type": "chat_note",
        "note_type": note_type,
        "status": "candidate",
        "question": (req.question or "").strip()[:1000],
        "answer_excerpt": (req.answer_excerpt or "").strip()[:1600],
        "note": note[:4000],
        "pages_read": pages_read,
        "sources": sources,
        "target_pages": pages_read,
        "source_type": "chat_note",
    }
    _append_trace(trace)
    telemetry.log_context_event(
        "chat_note",
        {
            "trace_id": trace_id,
            "note_type": note_type,
            "note_chars": len(note),
            "question": trace["question"],
            "pages_read": pages_read,
            "target_pages_count": len(pages_read),
        },
    )
    return {"success": True, "id": trace_id, "note_type": note_type, "target_pages": pages_read}


# ── /revision ────────────────────────────────────────────────────────────────
# Daily recall practice (replaces /interview). See docs/daily-revision/.

class RateRequest(BaseModel):
    slug: str
    rating: str   # forgot | shaky | knew


# Plain def: reads every page to pick the set; runs in the threadpool.
@app.get("/revision/today")
def revision_today(summary: bool = False):
    return revision.today_view(date.today(), summary=summary)


@app.post("/revision/rate")
def revision_rate(req: RateRequest):
    if req.rating not in revision.RATINGS:
        raise HTTPException(422, f"rating must be one of {', '.join(revision.RATINGS)}")
    if not vault_reader.read_page(req.slug):
        raise HTTPException(404, f"Page '{req.slug}' not found")
    due = revision.append_rating(req.slug, req.rating, date.today())
    return {"success": True, "next_due": due.isoformat()}


# ── /pages ───────────────────────────────────────────────────────────────────


@app.get("/health")
async def health():
    """Lightweight liveness probe — no DB, no LLM, just confirms the process is up."""
    return {"status": "ok", "ops_enabled": ENABLE_OPS}


@app.get("/pages")
async def list_pages(folder: str = "cs"):
    return {"pages": vault_reader.list_pages_in_folder(folder)}


@app.get("/memory/status")
async def get_memory_status():
    try:
        return memory_store.status()
    except Exception as e:
        raise HTTPException(500, f"Memory status failed: {e}")


@app.get("/aliases")
async def get_aliases():
    pages = [p["name"] for p in vault_reader.list_concept_pages()]
    return {
        "aliases": identity.alias_map(),
        "duplicate_candidates": identity.duplicate_candidates(pages),
    }


@app.get("/preferences")
async def get_preferences():
    data = preference_memory.load()
    return {
        "path": str(preference_memory.path()),
        "preferences": data,
        "review_candidates": preference_memory.review_candidates(),
        "prompt_hints": preference_memory.prompt_hints(),
        "chat_hints": preference_memory.chat_hints(),
    }


@app.post("/preferences/review")
async def review_preference(req: PreferenceReviewRequest):
    try:
        data = preference_memory.set_preference_status(
            req.kind,
            req.key,
            req.value,
            req.status,
        )
        return {
            "ok": True,
            "preferences": data,
            "review_candidates": preference_memory.review_candidates(),
            "prompt_hints": preference_memory.prompt_hints(),
        }
    except KeyError as e:
        raise HTTPException(404, str(e))
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.post("/memory/reindex")
async def reindex_memory():
    try:
        result = memory_store.sync_index()
        result.update(memory_store.status())
        return result
    except Exception as e:
        raise HTTPException(500, f"Memory reindex failed: {e}")


@app.get("/vault/recent-pages")
async def recent_pages(days: int = 7):
    """Return pages from all domain folders sorted by last_updated desc, filtered to last N days."""
    from datetime import timedelta
    cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    all_pages = []
    for folder in ["cs", "science", "humanities"]:
        for p in vault_reader.list_pages_in_folder(folder):
            if (p.get("last_updated") or p.get("date") or "") >= cutoff:
                all_pages.append(p)
    all_pages.sort(key=lambda p: p.get("last_saved_at") or p.get("last_updated") or p.get("date") or "", reverse=True)
    return {"pages": all_pages}


# ── /tag-colors ──────────────────────────────────────────────────────────────

@app.get("/tag-colors")
async def get_tag_colors():
    """Return tag→group mapping. Frontend maps group→Tailwind color classes."""
    return tag_classifier.load()


# ── /page/{page_name} ─────────────────────────────────────────────────────────

@app.get("/page/{page_name}")
async def get_page(page_name: str):
    page_name = identity.resolve_slug(page_name)
    content = vault_reader.read_page(page_name)
    if content is None:
        raise HTTPException(404, f"Page '{page_name}' not found")
    parsed = vault_reader.parse_concept_page(page_name)
    backlinks_index = vault_reader.build_backlinks_index()
    backlinks = backlinks_index.get(page_name, [])
    return {"name": page_name, "content": content, "parsed": parsed, "backlinks": backlinks}


# ── /page-history/{page_name} ─────────────────────────────────────────────────

@app.get("/page-history/{page_name}")
async def get_page_history(page_name: str):
    """
    Return all approval traces that reference this concept page (as final_page).
    Used by the evolution timeline modal in the frontend.
    """
    vault_path = Path(os.environ.get("VAULT_PATH", "/Users/sakethv7/SakethVault"))
    traces_path = vault_path / "_wiki" / "meta" / "traces.jsonl"
    events = []
    if traces_path.exists():
        for line in traces_path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                t = json.loads(line)
            except Exception:
                continue
            if t.get("final_page") == page_name and t.get("approved"):
                events.append({
                    "ts": t.get("ts", ""),
                    "source": t.get("url") or t.get("source_type", "text"),
                    "source_type": t.get("source_type", "text"),
                    "evolution_type": t.get("evolution_type", "extends"),
                    "evolution_reason": t.get("evolution_reason", ""),
                    "tags": t.get("tags_final", []),
                    "page_corrected": t.get("page_corrected", False),
                })
    # Return chronological order (oldest first)
    events.sort(key=lambda e: e["ts"])
    return {"page": page_name, "events": events}


# ── /dashboard-stats ──────────────────────────────────────────────────────────

def _parse_iso_datetime(value) -> Optional[datetime]:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is not None:
        return parsed.astimezone().replace(tzinfo=None)
    return parsed


def _normalize_dashboard_source(value: str) -> str:
    source = str(value or "unknown").strip().lower().replace("-", "_")
    labels = {
        "clip_markdown": "clip",
        "quick_note": "quick note",
        "quick-note": "quick note",
        "url": "link",
        "text": "text",
        "lecture": "lecture",
        "tweet": "tweet",
        "gap_fill": "gap fill",
        "unknown": "unknown",
    }
    return labels.get(source, source.replace("_", " "))


def _dashboard_stats_from_traces(
    traces: list[dict],
    now: Optional[datetime] = None,
    period_days: int = 30,
    heatmap_days: int = 112,
    until: Optional[datetime] = None,
) -> dict:
    now = now or datetime.now()
    period_cutoff = now - timedelta(days=period_days)
    heatmap_cutoff = now - timedelta(days=heatmap_days)
    week_cutoff = now - timedelta(days=7)

    parsed_rows = []
    for trace in traces:
        ts = _parse_iso_datetime(trace.get("ts"))
        if not ts:
            continue
        parsed_rows.append((trace, ts))

    # `until` bounds the window from above so an earlier window (for trends)
    # doesn't also count everything after it.
    period_rows = [(t, ts) for t, ts in parsed_rows if ts > period_cutoff and (until is None or ts <= until)]
    period_approved = [(t, ts) for t, ts in period_rows if t.get("approved")]
    period_rejected = [(t, ts) for t, ts in period_rows if t.get("approved") is False]
    heatmap_approved = [(t, ts) for t, ts in parsed_rows if t.get("approved") and ts > heatmap_cutoff]

    activity_by_date: dict[str, int] = {}
    for _, ts in heatmap_approved:
        date = ts.date().isoformat()
        activity_by_date[date] = activity_by_date.get(date, 0) + 1

    pages_30d = {
        str(t.get("final_page") or "").strip()
        for t, _ in period_approved
        if str(t.get("final_page") or "").strip()
    }
    week_pages_touched = {
        str(t.get("final_page") or "").strip()
        for t, ts in period_approved
        if ts > week_cutoff and str(t.get("final_page") or "").strip()
    }

    first_seen: dict[str, datetime] = {}
    for trace, ts in parsed_rows:
        if not trace.get("approved"):
            continue
        page = str(trace.get("final_page") or "").strip()
        if not page:
            continue
        if page not in first_seen or ts < first_seen[page]:
            first_seen[page] = ts
    new_pages_week = {page for page, ts in first_seen.items() if ts > week_cutoff}

    tag_counts: dict[str, int] = {}
    for trace, _ in period_approved:
        tags = trace.get("tags_final")
        if not isinstance(tags, list):
            tags = trace.get("tags") if isinstance(trace.get("tags"), list) else []
        for tag in tags:
            tag_name = str(tag).strip()
            if tag_name:
                tag_counts[tag_name] = tag_counts.get(tag_name, 0) + 1
    top_tags = sorted(tag_counts.items(), key=lambda x: (-x[1], x[0].lower()))[:10]

    source_counts: dict[str, int] = {}
    for trace, _ in period_approved:
        source = _normalize_dashboard_source(trace.get("source_type", "unknown"))
        source_counts[source] = source_counts.get(source, 0) + 1
    top_sources = sorted(source_counts.items(), key=lambda x: (-x[1], x[0]))

    denominator = max(1, period_days / 7)
    total_period_events = len(period_approved) + len(period_rejected)
    return {
        "period_days": period_days,
        "heatmap_days": heatmap_days,
        "total_events": total_period_events,
        "total_approved": len(period_approved),
        "total_rejected": len(period_rejected),
        "approval_rate": round(len(period_approved) / total_period_events, 4) if total_period_events else None,
        "unique_concepts": len(pages_30d),
        "activity_by_date": activity_by_date,
        "learning_velocity": {
            "entries_per_week": round(len(period_approved) / denominator, 2),
            "concepts_per_week": round(len(pages_30d) / denominator, 2),
        },
        "top_tags": [{"tag": tag, "count": count} for tag, count in top_tags],
        "top_sources": [{"source": src, "count": count} for src, count in top_sources],
        "new_concepts_this_week": len(new_pages_week),
        "concepts_touched_this_week": len(week_pages_touched),
    }


_QUESTION_EVENTS = {"chat_context": "chat_questions", "interview_context": "interview_questions"}


def _recall_stats(
    reads: list[dict],
    context_events: list[dict],
    now: Optional[datetime] = None,
    period_days: int = 30,
    until: Optional[datetime] = None,
    revision_log: Optional[list[dict]] = None,
) -> dict:
    """Recall side of the loop: page reads and questions asked in the period."""
    now = now or datetime.now()
    cutoff = now - timedelta(days=period_days)

    def in_window(ts: Optional[datetime]) -> bool:
        return bool(ts) and ts > cutoff and (until is None or ts <= until)

    read_pages = []
    for entry in reads:
        # /log-read writes concept/ts; older rows may use page/timestamp.
        ts = _parse_iso_datetime(entry.get("ts") or entry.get("timestamp"))
        page = str(entry.get("concept") or entry.get("page") or "").strip()
        if in_window(ts) and page:
            read_pages.append(page)

    counts = {field: 0 for field in _QUESTION_EVENTS.values()}
    for event in context_events:
        field = _QUESTION_EVENTS.get(event.get("event_type"))
        ts = _parse_iso_datetime(event.get("ts"))
        if field and in_window(ts):
            counts[field] += 1

    revisions = sum(1 for row in (revision_log or []) if in_window(_parse_iso_datetime(row.get("ts"))))
    return {
        "pages_read": len(read_pages),
        "unique_pages_read": len(set(read_pages)),
        "questions_asked": sum(counts.values()),
        **counts,
        "revisions": revisions,
    }


@app.get("/dashboard-stats")
async def get_dashboard_stats():
    """
    Return learning metrics for the last 30 days:
    - Activity timeline (concepts added by date)
    - Learning velocity (entries/week, concepts/week)
    - Most-referenced tags (frequency)
    - Top sources (source_type frequency)
    - New concepts this week
    """
    vault_path = Path(os.environ.get("VAULT_PATH", "/Users/sakethv7/SakethVault"))
    traces_path = vault_path / "_wiki" / "meta" / "traces.jsonl"

    # Parse all traces
    traces = []
    if traces_path.exists():
        for line in traces_path.read_text().splitlines():
            if line.strip():
                try:
                    traces.append(json.loads(line))
                except Exception:
                    continue

    reads = []
    reads_path = vault_path / "_wiki" / "meta" / "reads.jsonl"
    if reads_path.exists():
        for line in reads_path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                try:
                    reads.append(json.loads(line))
                except Exception:
                    continue

    context_events = telemetry.read_context_events()
    now = datetime.now()
    stats = _dashboard_stats_from_traces(traces, now=now)
    period = stats["period_days"]
    revision_log = revision.read_log()
    stats["recall"] = _recall_stats(reads, context_events, now=now, period_days=period, revision_log=revision_log)

    # Same-length window just before the current one, for trend arrows.
    prev_end = now - timedelta(days=period)
    prev = _dashboard_stats_from_traces(traces, now=prev_end, period_days=period, until=prev_end)
    prev_recall = _recall_stats(reads, context_events, now=prev_end, period_days=period, until=prev_end,
                                revision_log=revision_log)
    stats["previous"] = {
        "total_approved": prev["total_approved"],
        "approval_rate": prev["approval_rate"],
        "pages_read": prev_recall["pages_read"],
        "questions_asked": prev_recall["questions_asked"],
        "revisions": prev_recall["revisions"],
    }
    return stats


# ── /analyze-traces ──────────────────────────────────────────────────────────

# Only the fields the analysis prompt describes. Full traces carry summaries,
# diagrams and curation text that the prompt never uses and that made each
# call ~84K input tokens.
_ANALYSIS_TRACE_FIELDS = (
    "approved", "event_type", "note_type", "note", "target_pages", "title",
    "suggested_page", "final_page", "page_corrected", "evolution_type",
    "was_duplicate", "tags_suggested", "tags_final", "tags_corrected", "source_type",
)


def _compact_trace(trace: dict) -> dict:
    return {k: trace[k] for k in _ANALYSIS_TRACE_FIELDS if k in trace}


# Plain def: one ~30s blocking LLM call. As async def it froze every other
# request for that long, both on "Run analysis" and on the weekly schedule.
@app.post("/analyze-traces")
def analyze_traces():
    """
    Read traces.jsonl, send to Sonnet, write findings + prompt hints to
    _wiki/meta/system-insights.md. Returns the written insights.
    """
    vault_path = Path(os.environ.get("VAULT_PATH", "/Users/sakethv7/SakethVault"))
    traces_path = vault_path / "_wiki" / "meta" / "traces.jsonl"
    if not traces_path.exists():
        raise HTTPException(404, "No traces recorded yet — approve some items first")

    lines = [l for l in traces_path.read_text().splitlines() if l.strip()]
    if not lines:
        raise HTTPException(404, "traces.jsonl is empty")

    traces = []
    for line in lines:
        try:
            traces.append(json.loads(line))
        except Exception:
            continue

    if len(traces) < 3:
        raise HTTPException(400, f"Need at least 3 traces to analyze (have {len(traces)})")

    # Build compact trace summary for the prompt
    trace_summary = "\n".join(
        json.dumps(_compact_trace(t)) for t in traces[-SELF_LEARN_TRACE_WINDOW:]
    )

    # Static instructions go in `system` (auto prompt-cached). The trace dump
    # — by far the largest and most repeated part of this call, since
    # back-to-back "Run analysis" clicks often see an unchanged window — is
    # its own cached content block so repeat calls hit cache instead of
    # re-billing the full window every time.
    system_prompt = """You are analyzing usage traces from a personal knowledge wiki system to find patterns and suggest improvements.

Most traces are approve/reject ingestion events; some have event_type=chat_note and capture typed user feedback on chat answers.

Fields:
- approved: was the item approved (True) or rejected (False)
- event_type: chat_note means the user attached correction, contradiction, example, or nuance to a chat answer
- note_type / note / target_pages: chat-note label, user note text, and related wiki pages
- suggested_page / final_page: what the AI suggested vs what the user chose
- page_corrected: True if user changed the suggested page
- evolution_type: extends/refines/supersedes/duplicates/contradicts
- was_duplicate: True if classified as duplicate
- tags_suggested / tags_final: AI-suggested vs user-approved tags
- tags_corrected: True if user changed tags
- source_type: tweet / url / text

Analyze these traces and write a structured insights report. Be specific — name actual page slugs, actual tags, actual patterns you see in the data.

Respond with a JSON object (no markdown fences):
{
  "patterns": [
    "pattern description 1",
    "pattern description 2"
  ],
  "tag_confusion": [
    "specific tag confusion observed"
  ],
  "duplicate_signals": [
    "topics or sources that frequently produce duplicates"
  ],
  "rejection_patterns": [
    "what types of content gets rejected"
  ],
  "prompt_hints": [
    "Concrete one-line hint to inject into the extraction prompt to fix a specific observed problem",
    "Another hint"
  ],
  "routing_recommendations": [
    "Specific change to model routing or tag vocabulary"
  ],
  "architecture_recommendations": [
    "Larger structural change worth considering"
  ],
  "summary": "2-3 sentence overall summary of system health"
}

Keep every list to at most 5 items, each item under 30 words.

prompt_hints must be actionable, specific, and short — they will be directly injected into the extraction system prompt. E.g.:
- "Twitter content about agent tooling maps to existing pages more often than it needs a new page — prefer existing slugs"
- "The tag Agentic is frequently corrected to Agents — use Agents for tool-use and orchestration content"
"""

    user_content = [
        {
            "type": "text",
            "text": f"Here are the {len(traces[-SELF_LEARN_TRACE_WINDOW:])} most recent traces:\n\n{trace_summary}",
            "cache_control": {"type": "ephemeral"},
        },
        {
            "type": "text",
            "text": f"(Total traces recorded so far: {len(traces)}.)",
        },
    ]

    raw = llm_client.complete(
        task="analyze_traces",
        model=None,
        max_tokens=4000,
        system=system_prompt,
        messages=[{"role": "user", "content": user_content}],
        expect_json=True,
        required_json_keys=["patterns", "prompt_hints", "summary"],
    ).strip()
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
        raw = raw.strip()

    try:
        insights = json.loads(raw)
    except json.JSONDecodeError as e:
        raise HTTPException(500, f"Analysis returned invalid JSON: {e}")

    # Write to system-insights.md
    today = datetime.now().strftime("%Y-%m-%d")
    hints_md = "\n".join(f"- {h}" for h in insights.get("prompt_hints", []))
    patterns_md = "\n".join(f"- {p}" for p in insights.get("patterns", []))
    tag_md = "\n".join(f"- {t}" for t in insights.get("tag_confusion", []))
    dup_md = "\n".join(f"- {d}" for d in insights.get("duplicate_signals", []))
    reject_md = "\n".join(f"- {r}" for r in insights.get("rejection_patterns", []))
    routing_md = "\n".join(f"- {r}" for r in insights.get("routing_recommendations", []))
    arch_md = "\n".join(f"- {a}" for a in insights.get("architecture_recommendations", []))

    content = f"""---
last_analyzed: {today}
traces_analyzed: {len(traces)}
---

# System Insights

> {insights.get("summary", "")}

## Extraction Patterns
{patterns_md or "- No patterns found yet"}

## Tag Confusion
{tag_md or "- None observed"}

## Duplicate Signals
{dup_md or "- None observed"}

## Rejection Patterns
{reject_md or "- None observed"}

## Prompt Hints
<!-- Auto-injected into extraction prompt on every ingest -->
{hints_md or "- None yet"}

## Routing Recommendations
{routing_md or "- None yet"}

## Architecture Recommendations
{arch_md or "- None yet"}
"""

    insights_path = vault_path / "_wiki" / "meta" / "system-insights.md"
    insights_path.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write_path(insights_path, content)
    # Trace analysis (above) is the knowledge loop and always runs. Routing the
    # findings into bounded system actions is Operations — gated.
    routed = system_loop.run_system_loop(auto_apply=True) if ENABLE_OPS else None

    return {
        "success": True,
        "traces_analyzed": len(traces),
        "insights": insights,
        "file_written": str(insights_path.relative_to(vault_path)),
        "system_loop": routed,
    }


@app.get("/system-insights")
async def get_system_insights():
    """Return current system-insights.md content."""
    vault_path = Path(os.environ.get("VAULT_PATH", "/Users/sakethv7/SakethVault"))
    insights_path = vault_path / "_wiki" / "meta" / "system-insights.md"
    if not insights_path.exists():
        return {"exists": False, "content": None, "traces_count": 0}

    traces_path = vault_path / "_wiki" / "meta" / "traces.jsonl"
    traces_count = 0
    if traces_path.exists():
        traces_count = sum(1 for l in traces_path.read_text().splitlines() if l.strip())

    content = insights_path.read_text(encoding="utf-8")

    # Parse out sections for structured frontend display
    from vault_reader import _parse_frontmatter
    meta = _parse_frontmatter(content)

    sections = {}
    current = None
    for line in content.splitlines():
        if line.startswith("## "):
            current = line[3:].strip()
            sections[current] = []
        elif current and line.startswith("- ") and not line.startswith("<!-- "):
            sections[current].append(line[2:].strip())

    return {
        "exists": True,
        "last_analyzed": meta.get("last_analyzed", ""),
        "traces_analyzed": int(meta.get("traces_analyzed", 0)),
        "traces_count": traces_count,
        "sections": sections,
        "content": content,
    }


# ── Operations subsystem ─────────────────────────────────────────────────────
# These routes register only when ENABLE_OPS is true (see include_router below).
ops_router = APIRouter()


@ops_router.post("/inference-report")
async def generate_inference_report():
    """Generate a Markdown inference report from runtime LLM/context telemetry."""
    return telemetry.generate_inference_report()


@ops_router.post("/system-loop/run")
async def run_system_loop(auto_apply: bool = True):
    """
    Generate inference + system-loop reports and route bounded system improvements.

    Low-risk repeated correction preferences can be activated automatically.
    Runtime model-routing overrides require enough critical-task telemetry.
    """
    return system_loop.run_system_loop(auto_apply=auto_apply)


@ops_router.get("/system-loop/actions")
async def get_system_loop_actions(limit: int = 100):
    """Return recent system-loop routed actions."""
    return {"actions": telemetry.read_system_actions(limit=limit)}


@ops_router.get("/system-actions")
async def get_system_actions(status: Optional[str] = None):
    """Return staged/applied/rejected system action candidates."""
    return {"candidates": system_loop.list_action_candidates(status=status)}


@ops_router.post("/trace-critic/run")
async def run_trace_critic():
    """Run the bounded LLM trace critic and stage only supported action candidates."""
    actions: list[dict] = []
    return system_loop.run_trace_critic(actions)


@ops_router.post("/system-actions/{candidate_id}/approve")
async def approve_system_action(candidate_id: str):
    try:
        return {"candidate": system_loop.approve_action_candidate(candidate_id)}
    except KeyError as e:
        raise HTTPException(404, str(e))


@ops_router.post("/system-actions/{candidate_id}/reject")
async def reject_system_action(candidate_id: str, req: RejectSystemActionRequest):
    try:
        return {"candidate": system_loop.reject_action_candidate(candidate_id, req.reason)}
    except KeyError as e:
        raise HTTPException(404, str(e))


@ops_router.post("/system-actions/{candidate_id}/eval")
async def eval_system_action(candidate_id: str):
    try:
        return system_loop.run_candidate_eval(candidate_id)
    except KeyError as e:
        raise HTTPException(404, str(e))


@ops_router.post("/evals/run")
async def run_evals(include_judge: bool = True):
    """Run the current replay eval suite and write an eval report."""
    report = eval_harness.run_eval(include_judge=include_judge)
    actions = system_loop.route_eval_findings(report)
    return {"eval": report, "actions": actions}


@ops_router.post("/reports/read")
async def read_report(req: ReportPathRequest):
    report_dir = telemetry.reports_dir().resolve()
    try:
        path = Path(req.path).resolve()
    except OSError:
        raise HTTPException(400, "Invalid report path")
    if report_dir not in path.parents or path.suffix != ".md" or not path.exists():
        raise HTTPException(404, "Report not found")
    return {
        "name": path.name,
        "path": str(path),
        "content": path.read_text(encoding="utf-8"),
        "updated_at": datetime.fromtimestamp(path.stat().st_mtime).isoformat(),
    }


@ops_router.get("/operations-overview")
async def operations_overview():
    llm_summary = telemetry.summarize_llm_calls()
    context_summary = telemetry.summarize_context_events()
    eval_summary = eval_harness.run_system_level_eval()
    actions = telemetry.read_system_actions(limit=50)
    candidates = system_loop.list_action_candidates()
    errors = [
        row for row in telemetry.read_llm_calls(limit=200)
        if row.get("error") or row.get("contract_ok") is False
    ][-50:]
    settings = system_loop.load_runtime_settings()
    overrides = system_loop.load_runtime_overrides()
    report_dir = telemetry.reports_dir()
    reports = []
    if report_dir.exists():
        for path in sorted(report_dir.glob("*.md"), key=lambda p: p.stat().st_mtime, reverse=True)[:20]:
            reports.append({
                "name": path.name,
                "path": str(path),
                "updated_at": datetime.fromtimestamp(path.stat().st_mtime).isoformat(),
                "kind": path.name.split("-", 1)[0],
            })
    return {
        "llm_summary": llm_summary,
        "context_summary": context_summary,
        "eval_summary": eval_summary,
        "actions": actions,
        "candidates": candidates,
        "errors": errors,
        "runtime_settings": settings,
        "runtime_overrides": overrides,
        "reports": reports,
    }


if ENABLE_OPS:
    app.include_router(ops_router)


# ── /follow-up/{page_name} ───────────────────────────────────────────────────

@app.get("/follow-up/{page_name}")
async def follow_up(page_name: str, recently_read: str = ""):
    """
    Return follow-up page suggestions for a given page.
    Pure regex scoring — zero LLM.

    Scoring:
      +3  candidate is a direct wikilink in current page
      +3  current page is a direct wikilink in candidate (mutual link)
      +2  per shared tag
      +1  candidate appears in wikilinks of recently-read pages
      -5  candidate was recently read (don't repeat)
    """
    vault_path = Path(os.environ.get("VAULT_PATH", "/Users/sakethv7/SakethVault"))
    concepts_dir = vault_path / "_wiki" / "cs"
    if not concepts_dir.exists():
        return {"suggestions": []}

    recent = [r.strip() for r in recently_read.split(",") if r.strip()] if recently_read else []
    recent_set = set(recent)

    # Read current page
    current_path = concepts_dir / f"{page_name}.md"
    if not current_path.exists():
        return {"suggestions": []}

    current_content = current_path.read_text(encoding="utf-8")
    current_links = set(l.lower().replace(" ", "-") for l in _re.findall(r"\[\[([^\]]+)\]\]", current_content))
    current_meta = vault_reader._parse_frontmatter(current_content)
    current_tags = set(t.lower() for t in (current_meta.get("tags", []) if isinstance(current_meta.get("tags"), list) else []))

    # Build wikilinks for recently-read pages
    recent_links: set[str] = set()
    for rp in recent:
        rpath = concepts_dir / f"{rp}.md"
        if rpath.exists():
            try:
                rc = rpath.read_text(encoding="utf-8")
                for l in _re.findall(r"\[\[([^\]]+)\]\]", rc):
                    recent_links.add(l.lower().replace(" ", "-"))
            except OSError:
                pass

    # Score all other concept pages
    scores: list[tuple[float, dict]] = []
    for md_file in concepts_dir.glob("*.md"):
        cname = md_file.stem
        if cname == page_name:
            continue
        try:
            content = md_file.read_text(encoding="utf-8")
        except OSError:
            continue
        meta = vault_reader._parse_frontmatter(content)
        candidate_links = set(l.lower().replace(" ", "-") for l in _re.findall(r"\[\[([^\]]+)\]\]", content))
        candidate_tags = set(t.lower() for t in (meta.get("tags", []) if isinstance(meta.get("tags"), list) else []))

        score: float = 0
        reasons: list[str] = []

        # Direct wikilink from current page to candidate
        if cname in current_links:
            score += 3
            reasons.append("linked from this page")

        # Mutual link (candidate links back to current)
        if page_name in candidate_links:
            score += 3
            if "linked from this page" not in reasons:
                reasons.append("links back here")
            else:
                reasons[0] = "mutual link"

        # Shared tags
        shared = current_tags & candidate_tags
        if shared:
            score += len(shared) * 2
            reasons.append(f"shares {', '.join(sorted(shared)[:2])}")

        # Appears in recently-read wikilinks
        if cname in recent_links:
            score += 1

        # Penalty for recently read
        if cname in recent_set:
            score -= 5

        if score > 0:
            scores.append((score, {
                "name": cname,
                "title": meta.get("title", cname.replace("-", " ").title()),
                "tags": meta.get("tags", []) if isinstance(meta.get("tags"), list) else [],
                "entry_count": int(meta.get("entry_count", 1)),
                "last_updated": meta.get("last_updated", ""),
                "reason": reasons[0] if reasons else "",
                "score": score,
            }))

    scores.sort(key=lambda x: x[0], reverse=True)
    return {"suggestions": [s for _, s in scores[:5]]}


# ── /graph ───────────────────────────────────────────────────────────────────

@app.get("/graph")
async def get_graph():
    """Return knowledge graph nodes + edges for visualization."""
    return vault_reader.build_graph()


# ── /backlinks/{page_name} ────────────────────────────────────────────────────

@app.get("/backlinks/{page_name}")
async def get_backlinks(page_name: str):
    """Return all pages that link to the given page."""
    index = vault_reader.build_backlinks_index()
    return {"page": page_name, "backlinks": index.get(page_name, [])}


# ── /review-queue ─────────────────────────────────────────────────────────────

@app.get("/review-queue")
def review_queue(limit: int = 50, min_priority: str = "low"):
    """Return active review queue ranked by maturity, staleness, backlinks, and conflicts.

    Plain def: build_queue reads every page (~1s), so FastAPI runs it in a
    worker thread instead of blocking the event loop for other requests."""
    pages = active_review.build_queue(limit=limit, min_priority=min_priority)
    return {"pages": pages, "total": len(pages)}


# Plain def: scores every page pair (~seconds); FastAPI runs it in a worker
# thread so the rest of the dashboard isn't frozen while it runs.
@app.get("/consolidation-candidates")
def consolidation_candidates(limit: int = 50, include_weak: bool = False):
    """Return conservative duplicate/merge candidates without mutating the vault."""
    candidates = consolidation.find_candidates(limit=limit, include_weak=include_weak)
    return {"candidates": candidates, "total": len(candidates), "merge_max_chars": CONSOLIDATE_MAX_INPUT_CHARS}


# Plain def: runs the pair scorer (~2s); FastAPI runs it in a worker thread.
@app.get("/attention")
def attention(pairs_limit: int = 8, orphans_limit: int = 8):
    """Dashboard "Needs attention": contradictions, likely duplicate pairs, and
    unlinked pages each paired with their closest page. No LLM."""
    review = active_review.build_queue(limit=200, min_priority="low")
    contradictions = [
        {"name": p["name"], "folder": p.get("folder"), "reasons": p["reasons"]}
        for p in review if any("conflict marker" in r for r in p["reasons"])
    ]
    pairs = consolidation.find_candidates(limit=pairs_limit, include_weak=True)
    in_pairs = {slug for c in pairs for slug in (c["source"], c["target"])}
    unlinked = [
        p for p in review
        if p["priority"] == "high" and "no backlinks" in p["reasons"] and p["name"] not in in_pairs
    ]
    partners = consolidation.best_partners(
        [p["name"] for p in unlinked[:orphans_limit]],
        exclude_pairs={consolidation._pair_key(c["source"], c["target"]) for c in pairs},
    )
    orphans = [
        {**partners[p["name"]], "source": p["name"], "folder": p.get("folder")}
        for p in unlinked[:orphans_limit] if p["name"] in partners
    ]
    return {
        "contradictions": contradictions,
        "pairs": pairs,
        "orphans": orphans,
        "counts": {
            "contradictions": len(contradictions),
            "pairs": len(pairs),
            "unlinked_total": len(unlinked),
        },
        "merge_max_chars": CONSOLIDATE_MAX_INPUT_CHARS,
    }


@app.post("/consolidation-candidates/dismiss")
def dismiss_consolidation_candidate(req: DismissPairRequest):
    """Remember that two pages are not duplicates so the pair stops appearing."""
    consolidation.dismiss_pair(req.source, req.target)
    return {"success": True}


# ── /quick-note ───────────────────────────────────────────────────────────────

class QuickNoteRequest(BaseModel):
    page: str          # concept page slug to append to
    note: str          # the thought/note text
    create_if_missing: bool = False


@app.post("/quick-note")
async def quick_note(req: QuickNoteRequest):
    """
    Append a quick thought to an existing concept page, skipping the
    full ingest pipeline. No LLM — pure file append.
    """
    if not req.note.strip():
        raise HTTPException(400, "note cannot be empty")

    vault_path = Path(os.environ.get("VAULT_PATH", "/Users/sakethv7/SakethVault"))
    wiki_dir = vault_path / "_wiki"

    # Find page (case-insensitive) across all domain folders
    page_path = None
    for folder in ["cs", "science", "humanities", "insights", "sources", "open-threads"]:
        for f in (wiki_dir / folder).glob("*.md"):
            if f.stem.lower() == req.page.lower():
                page_path = f
                break
        if page_path:
            break

    if page_path is None:
        if not req.create_if_missing:
            raise HTTPException(404, f"Page '{req.page}' not found")
        # Create minimal stub
        page_path = concepts_dir / f"{req.page}.md"
        today = datetime.now().strftime("%Y-%m-%d")
        stub = f"""---
title: "{req.page.replace('-', ' ').title()}"
tags: []
entry_count: 0
last_updated: {today}
understanding_version: 1
---

> **Current understanding** 🔵
> (No entries yet — built from quick notes)
"""
        page_path.parent.mkdir(parents=True, exist_ok=True)
        page_path.write_text(stub, encoding="utf-8")

    today = datetime.now().strftime("%Y-%m-%d")
    now = datetime.now().strftime("%Y-%m-%d %H:%M")

    # Append a note section
    note_section = f"""
## 💭 Quick note · {now}
{req.note.strip()}
"""

    content = page_path.read_text(encoding="utf-8")
    content = content.rstrip() + "\n" + note_section

    # Update last_updated in frontmatter
    content = re.sub(r"(last_updated:\s*)[\d-]+", f"\\g<1>{today}", content)

    # Bump entry_count
    def bump(m):
        return m.group(1) + str(int(m.group(2)) + 1)
    content = re.sub(r"(entry_count:\s*)(\d+)", bump, content)

    _atomic_write_path(page_path, content)
    try:
        memory_store.index_page(page_path.stem)
    except Exception as _e:
        logger.warning("memory index update failed for quick-note %s: %s", page_path.stem, _e)

    # Append trace
    try:
        _append_trace({
            "ts": datetime.now().isoformat(),
            "url": "",
            "source_type": "quick-note",
            "approved": True,
            "title": f"Quick note on {req.page}",
            "suggested_page": req.page,
            "final_page": req.page,
            "page_corrected": False,
            "evolution_type": "extends",
            "was_duplicate": False,
            "tags_suggested": [],
            "tags_final": [],
            "tags_corrected": False,
            "wikilinks_suggested": [],
            "deep_dive": False,
        })
    except Exception:
        pass

    return {"success": True, "file_written": str(page_path.relative_to(vault_path))}


# ── /open-thread ─────────────────────────────────────────────────────────────

@app.post("/open-thread")
async def create_open_thread(req: OpenThreadRequest):
    """Create or overwrite an open-thread stub from the Browse UI."""
    if not req.title.strip():
        raise HTTPException(400, "title is required")
    file_written = _write_open_thread(req.title.strip(), req.notes, req.tags)
    return {"success": True, "file_written": file_written}


@app.delete("/open-thread/{name}")
async def delete_open_thread(name: str):
    """Delete an open thread file."""
    vault_path = Path(os.environ.get("VAULT_PATH", "/Users/sakethv7/SakethVault"))
    threads_dir = vault_path / "_wiki" / "open-threads"
    for f in threads_dir.glob("*.md"):
        if f.stem.lower() == name.lower():
            f.unlink()
            return {"success": True}
    raise HTTPException(404, f"Thread '{name}' not found")


@app.post("/close-open-thread/{page_name}")
async def close_open_thread(page_name: str):
    """Remove deep-dive tag from a concept page and delete its open-thread stub."""
    vault_path = Path(os.environ.get("VAULT_PATH", "/Users/sakethv7/SakethVault"))

    # Remove deep-dive tag from concept page (search all domain folders)
    page_file = None
    for folder in ["cs", "humanities", "science", "concepts", "lectures"]:
        candidate = vault_path / "_wiki" / folder / f"{page_name}.md"
        if candidate.exists():
            page_file = candidate
            break

    if page_file:
        content = page_file.read_text(encoding="utf-8")
        if content.startswith("---"):
            end = content.find("---", 3)
            if end != -1:
                fm = content[3:end]
                body = content[end:]
                lines = fm.splitlines()
                new_lines = []
                for line in lines:
                    if line.strip().startswith("tags:"):
                        stripped = line.strip()[5:].strip()
                        if stripped.startswith("[") and stripped.endswith("]"):
                            inner = [t.strip().strip('"\'') for t in stripped[1:-1].split(",")]
                            inner = [t for t in inner if t.lower() != "deep-dive"]
                            line = line[:line.index("tags:")] + f"tags: [{', '.join(inner)}]"
                    new_lines.append(line)
                new_content = "---" + "\n".join(new_lines) + body
                _atomic_write_path(page_file, new_content)

    # Delete matching open-thread stub
    threads_dir = vault_path / "_wiki" / "open-threads"
    for f in threads_dir.glob("*.md"):
        if f.stem.lower() == page_name.lower():
            f.unlink()
            break

    return {"success": True}


# ── /save-answer ─────────────────────────────────────────────────────────────

@app.post("/save-answer")
async def save_answer(req: SaveAnswerRequest):
    """File a chat answer back into the wiki as an insight note."""
    vault_path = Path(os.environ.get("VAULT_PATH", "/Users/sakethv7/SakethVault"))
    insights_dir = vault_path / "_wiki" / "insights"
    insights_dir.mkdir(parents=True, exist_ok=True)

    today = datetime.now().strftime("%Y-%m-%d")
    time_str = datetime.now().strftime("%Y-%m-%d %H:%M")
    slug = _slug_text(req.question)[:50]
    filename = f"{today}-{slug}.md"
    file_path = insights_dir / filename

    wikilinks = " ".join(f"[[{p}]]" for p in req.pages_read) if req.pages_read else ""
    sources_list = "\n".join(f"- {s}" for s in req.sources) if req.sources else "- none"

    content = f"""---
title: "{req.question[:80]}"
date: {today}
type: insight
pages_read: {req.pages_read}
---

# Q: {req.question}

*{time_str}*

{req.answer}

---

**Sources:** {wikilinks}
{sources_list}
"""
    _atomic_write_path(file_path, content)

    _append_log(vault_path / "_wiki" / "log.md",
                f"\n## {time_str} · insight\nQ: {req.question[:80]}\nWritten to: _wiki/insights/{filename}\n")
    try:
        memory_store.index_page(file_path.stem)
    except Exception as e:
        logger.warning("memory index update failed for insight %s: %s", file_path.stem, e)

    return {"success": True, "file_written": f"_wiki/insights/{filename}"}


# ── /lint ─────────────────────────────────────────────────────────────────────

import hashlib

_LINT_CACHE_PATH = Path(__file__).parent / "lint_cache.json"

def _compute_pages_hash(pages: list) -> str:
    """Compute hash of page list (names + entry_counts) to detect changes."""
    content = json.dumps([(p["name"], p["entry_count"]) for p in sorted(pages, key=lambda x: x["name"])], sort_keys=True)
    return hashlib.md5(content.encode()).hexdigest()


def _load_lint_cache() -> Optional[dict]:
    """Load cached lint report if it exists."""
    try:
        if _LINT_CACHE_PATH.exists():
            return json.loads(_LINT_CACHE_PATH.read_text())
    except Exception:
        pass
    return None


def _is_cache_valid(cached: dict, pages: list) -> bool:
    """Check if cached lint report is still valid (<24h and page list unchanged)."""
    if not cached:
        return False

    try:
        # Check timestamp (must be < 24h old)
        cached_ts = datetime.fromisoformat(cached.get("ran_at", ""))
        age = (datetime.now() - cached_ts).total_seconds()
        if age > LINT_CACHE_TTL_SECONDS:
            return False

        # Check page list hash (must match current pages)
        cached_hash = cached.get("_pages_hash")
        current_hash = _compute_pages_hash(pages)
        if cached_hash != current_hash:
            return False

        return True
    except Exception:
        return False


def _save_lint_cache(report: dict, pages: list) -> None:
    """Save lint report with metadata (timestamp, pages_hash)."""
    try:
        cached = {
            **report,
            "ran_at": datetime.now().isoformat(),
            "_pages_hash": _compute_pages_hash(pages)
        }
        _LINT_CACHE_PATH.write_text(json.dumps(cached, indent=2))
    except Exception:
        pass


def _build_link_consistency_audit(pages: list[dict]) -> dict:
    """Deterministic wikilink audit for consistency and redundancy.

    This is analysis-only: it does not mutate files.
    """
    page_names = [p.get("name", "") for p in pages if p.get("name")]
    existing = set(page_names)

    broken_links = []
    redundant_links = []
    weak_links = []
    suggested_additions = []

    total_links = 0
    penalty = 0

    for page in pages:
        name = page.get("name", "")
        if not name:
            continue

        content = vault_reader.read_page(name) or ""
        body = _strip_frontmatter(content)
        raw_links = _re.findall(r"\[\[([^\]]+)\]\]", body)
        normalized = [_normalize_slug(l) for l in raw_links if _normalize_slug(l)]
        total_links += len(normalized)

        # Redundant links inside same page
        counts = {}
        for l in normalized:
            counts[l] = counts.get(l, 0) + 1
        for link, c in counts.items():
            if c > 1:
                redundant_links.append({"page": name, "link": link, "count": c})
                penalty += min(6, c - 1)

        uniq_links = sorted(set(normalized))

        # Broken/self links
        for link in uniq_links:
            if link == name:
                weak_links.append({"page": name, "link": link, "reason": "self-link"})
                penalty += 2
            elif link not in existing:
                broken_links.append({"page": name, "link": link})
                penalty += 5

        # Weak semantic links + missing related links
        scope_text = " ".join([name] + [str(x) for x in page.get("tags", [])] + [body[:1200]]).lower()
        scope_tokens = set(_re.findall(r"[a-z0-9]+", scope_text))

        for link in uniq_links:
            if link not in existing or link == name:
                continue
            overlap = len(set(link.split("-")) & scope_tokens)
            if overlap == 0:
                weak_links.append({"page": name, "link": link, "reason": "low-overlap"})
                penalty += 2

        candidates = _top_matching_pages(scope_text, page_names, max_pages=8)
        adds = []
        linked = set(uniq_links)
        for cand in candidates:
            if cand == name or cand in linked:
                continue
            ov = len(set(cand.split("-")) & scope_tokens)
            if ov >= 2:
                adds.append(cand)
            if len(adds) >= 3:
                break
        if adds:
            suggested_additions.append({"page": name, "add": adds})

    consistency_score = max(0, 100 - penalty)
    return {
        "consistency_score": consistency_score,
        "pages_audited": len(page_names),
        "total_links": total_links,
        "broken_links": broken_links[:200],
        "redundant_links": redundant_links[:200],
        "weak_links": weak_links[:200],
        "suggested_additions": suggested_additions[:200],
    }

def _apply_safe_link_fixes_to_content(content: str, valid_pages: set[str]) -> tuple[str, dict]:
    """Apply deterministic, low-risk link fixes to one page content.

    Safe fixes:
    - Remove duplicate wikilinks in the same page.
    - Remove broken wikilinks pointing to non-existent pages.
    """
    body = _strip_frontmatter(content)
    fm = content[:len(content) - len(body)]

    pattern = _re.compile(r"\[\[([^\]]+)\]\]")
    seen = set()
    duplicate_removed = 0
    broken_removed = 0

    def repl(m):
        nonlocal duplicate_removed, broken_removed
        raw = m.group(1)
        slug = _normalize_slug(raw)
        if not slug:
            broken_removed += 1
            return ""
        if slug not in valid_pages:
            broken_removed += 1
            return ""
        if slug in seen:
            duplicate_removed += 1
            return ""
        seen.add(slug)
        return f"[[{slug}]]"

    new_body = pattern.sub(repl, body)
    # Clean punctuation artifacts caused by link removals.
    new_body = _re.sub(r",\s*,", ", ", new_body)
    new_body = _re.sub(r"\(\s*\)", "", new_body)
    new_body = _re.sub(r"\s{2,}", " ", new_body)
    new_body = _re.sub(r"\n[ \t]+\n", "\n\n", new_body)

    return fm + new_body, {
        "duplicate_removed": duplicate_removed,
        "broken_removed": broken_removed,
    }





# Plain def: reads the vault; FastAPI runs it in a worker thread.
@app.get("/lint")
def get_lint_cache():
    """
    Return the last cached lint report if it exists, or 204 if no cache.
    Use POST /lint to generate or refresh the health check report.
    """
    cached = _load_lint_cache()
    if cached is None:
        return Response(status_code=204)
    cached_clean = {k: v for k, v in cached.items() if not k.startswith("_")}
    pages = vault_reader.list_concept_pages()
    cached_clean["identity_resolution"] = {
        "duplicate_candidates": identity.duplicate_candidates(p["name"] for p in pages),
        "alias_count": len(identity.alias_map()),
    }
    cached_clean["cached"] = True
    return cached_clean


# Plain def: blocking LLM call; FastAPI runs it in a worker thread so
# other requests aren't frozen while it waits.
@app.post("/lint")
def lint_wiki(req: LintRequest):
    """
    Scan the entire wiki with Sonnet and return a structured health report:
    - inconsistencies across pages
    - missing connections between concepts
    - suggested new articles
    - orphaned pages (no wikilinks pointing to them)
    Optionally saves the report as _wiki/insights/YYYY-MM-DD-lint.md

    Cache strategy:
    - If force_refresh=False, checks cache for <24h validity + matching page list hash
    - If cache valid, returns cached report (saves ~$0.10 and 30s latency)
    - If cache invalid or force_refresh=True, runs full Sonnet scan
    """
    pages = vault_reader.list_concept_pages()
    if not pages:
        raise HTTPException(400, "No concept pages to lint yet.")

    # ── Check cache first ────────────────────────────────────────────────────────
    if not req.force_refresh:
        cached = _load_lint_cache()
        if _is_cache_valid(cached, pages):
            # Remove metadata fields from cached report before returning
            cached_clean = {k: v for k, v in cached.items() if not k.startswith("_")}
            cached_clean["identity_resolution"] = {
                "duplicate_candidates": identity.duplicate_candidates(p["name"] for p in pages),
                "alias_count": len(identity.alias_map()),
            }
            cached_clean["from_cache"] = True
            return cached_clean

    # Read all pages — strip frontmatter, cap each at 1200 chars for token efficiency
    pages_context = []
    for p in pages:
        content = vault_reader.read_page(p["name"]) or ""
        body = _strip_frontmatter(content)
        pages_context.append(
            f"### [[{p['name']}]] (tags: {', '.join(p['tags'])}, entries: {p['entry_count']})\n{body[:1200]}"
        )
    full_context = "\n\n".join(pages_context)

    # Load wiki standards (health-check section only)
    wiki_standards = _load_wiki_standards("For Health Check")

    # Map pages to semantic groups for per-category scoring
    tag_groups = tag_classifier.load()
    def page_primary_group(p):
        group_counts: dict = {}
        for tag in p.get("tags", []):
            g = tag_groups.get(tag)
            if g:
                group_counts[g] = group_counts.get(g, 0) + 1
        return max(group_counts, key=group_counts.get) if group_counts else "meta"

    group_page_map: dict = {}
    for p in pages:
        g = page_primary_group(p)
        group_page_map.setdefault(g, []).append(p["name"])

    group_summary = "\n".join(
        f"- {g}: {', '.join(names)}"
        for g, names in sorted(group_page_map.items())
    )

    standards_section = f"\nCuration standards (enforce these, not generic wiki rules):\n{wiki_standards}\n" if wiki_standards else ""

    prompt = f"""You are auditing Saketh's personal ML/AI knowledge wiki. Analyze ALL pages below and produce a structured health report.
{standards_section}
Wiki pages ({len(pages)} total):

{full_context}

Semantic groups (pages mapped by their primary topic):
{group_summary}

Return a JSON object (no markdown fences) with exactly these fields:
{{
  "health_score": <integer 0-100, overall wiki quality>,
  "category_scores": {{
    "<group_name>": <integer 0-100>
  }},
  "inconsistencies": [
    {{"pages": ["page1", "page2"], "issue": "description of contradiction or inconsistency"}}
  ],
  "missing_connections": [
    {{"from_page": "page1", "to_page": "page2", "reason": "why these should be linked"}}
  ],
  "suggested_articles": [
    {{"title": "concept-slug", "reason": "gap this would fill", "related_to": ["existing-page"]}}
  ],
  "orphaned_pages": ["page names with no inbound wikilinks from other pages"],
  "quick_wins": ["short actionable improvements, e.g. 'add [[KVCache]] link to agents.md'"]
}}

Rules:
- category_scores: score each group that has pages (0-100); score = depth + breadth + interconnection within that group
- inconsistencies: conflicting facts or definitions across pages (not style issues)
- missing_connections: pairs of pages that discuss related concepts but don't link to each other
- suggested_articles: concepts repeatedly mentioned across pages but not yet having their own page
- orphaned_pages: pages no other page links to via [[wikilinks]]
- quick_wins: max 5, concrete and specific
- health_score: 100 = complete, well-connected, no gaps; start at 100 and deduct for each real issue"""

    raw = llm_client.complete(
        task="lint_scan",
        model=None,
        max_tokens=4000,
        messages=[{"role": "user", "content": prompt}],
        expect_json=True,
        required_json_keys=[
            "health_score",
            "category_scores",
            "inconsistencies",
            "missing_connections",
            "suggested_articles",
            "orphaned_pages",
            "quick_wins",
        ],
    ).strip()
    # Strip any markdown fences (```json ... ``` or ``` ... ```)
    if "```" in raw:
        parts = raw.split("```")
        # Take the first fenced block
        for part in parts[1::2]:  # odd indices are inside fences
            candidate = part.strip()
            if candidate.startswith("json"):
                candidate = candidate[4:].strip()
            if candidate.startswith("{"):
                raw = candidate
                break
    # If still not JSON, try to find the first { ... } block
    if not raw.startswith("{"):
        start = raw.find("{")
        if start != -1:
            raw = raw[start:]

    try:
        report = json.loads(raw)
    except json.JSONDecodeError as first_err:
        # Retry once with an explicit "fix the JSON" prompt
        try:
            raw2 = llm_client.complete(
                task="lint_json_fix",
                model=None,
                max_tokens=4000,
                messages=[
                    {"role": "user", "content": prompt},
                    {"role": "assistant", "content": raw},
                    {"role": "user", "content": "Your previous response was not valid JSON. Return ONLY the raw JSON object, no prose or markdown."},
                ],
                expect_json=True,
                required_json_keys=[
                    "health_score",
                    "category_scores",
                    "inconsistencies",
                    "missing_connections",
                    "suggested_articles",
                    "orphaned_pages",
                    "quick_wins",
                ],
            ).strip().lstrip("```json").lstrip("```").rstrip("```").strip()
            report = json.loads(raw2)
        except Exception:
            raise HTTPException(500, f"LLM returned unparseable JSON: {first_err}\nRaw: {raw[:300]}")

    # Deterministic link consistency audit (non-LLM, non-mutating).
    report["link_consistency"] = _build_link_consistency_audit(pages)
    report["identity_resolution"] = {
        "duplicate_candidates": identity.duplicate_candidates(p["name"] for p in pages),
        "alias_count": len(identity.alias_map()),
    }

    file_written = None
    if req.save:
        vault_path = Path(os.environ.get("VAULT_PATH", "/Users/sakethv7/SakethVault"))
        insights_dir = vault_path / "_wiki" / "insights"
        insights_dir.mkdir(parents=True, exist_ok=True)
        today = datetime.now().strftime("%Y-%m-%d")
        time_str = datetime.now().strftime("%Y-%m-%d %H:%M")
        lint_path = insights_dir / f"{today}-lint-report.md"

        inconsistencies_md = "\n".join(
            f"- **{i['pages']}**: {i['issue']}" for i in report.get("inconsistencies", [])
        ) or "- None found"
        connections_md = "\n".join(
            f"- [[{c['from_page']}]] → [[{c['to_page']}]]: {c['reason']}"
            for c in report.get("missing_connections", [])
        ) or "- None found"
        articles_md = "\n".join(
            f"- **{a['title']}**: {a['reason']} (related: {', '.join(a.get('related_to', []))})"
            for a in report.get("suggested_articles", [])
        ) or "- None found"
        orphans_md = "\n".join(f"- [[{p}]]" for p in report.get("orphaned_pages", [])) or "- None"
        wins_md = "\n".join(f"- {w}" for w in report.get("quick_wins", [])) or "- None"
        lc = report.get("link_consistency", {}) or {}
        broken_md = "\n".join(f"- [[{x['page']}]] -> [[{x['link']}]]" for x in lc.get("broken_links", [])[:20]) or "- None"
        redundant_md = "\n".join(f"- [[{x['page']}]] repeats [[{x['link']}]] x{x['count']}" for x in lc.get("redundant_links", [])[:20]) or "- None"
        weak_md = "\n".join(f"- [[{x['page']}]] -> [[{x['link']}]] ({x['reason']})" for x in lc.get("weak_links", [])[:20]) or "- None"
        add_md = "\n".join(f"- [[{x['page']}]] add: " + ", ".join(f"[[{a}]]" for a in x.get("add", [])) for x in lc.get("suggested_additions", [])[:20]) or "- None"
        identity_report = report.get("identity_resolution", {}) or {}
        alias_md = "\n".join(
            f"- [[{x['alias']}]] -> [[{x['canonical']}]]: {x['reason']}"
            for x in identity_report.get("duplicate_candidates", [])
        ) or "- None"

        content = f"""---
title: "Wiki Lint Report"
date: {today}
type: lint
health_score: {report.get('health_score', '?')}
pages_scanned: {len(pages)}
---

# Wiki Lint Report · {time_str}

**Health score:** {report.get('health_score', '?')}/100
**Pages scanned:** {len(pages)}

## Inconsistencies
{inconsistencies_md}

## Missing Connections
{connections_md}

## Suggested New Articles
{articles_md}

## Orphaned Pages
{orphans_md}

## Quick Wins
{wins_md}

## Link Consistency Audit
**Consistency score:** {lc.get('consistency_score', '?')}/100
**Pages audited:** {lc.get('pages_audited', 0)}
**Total links scanned:** {lc.get('total_links', 0)}

### Broken Links
{broken_md}

### Redundant Links
{redundant_md}

### Weak Links
{weak_md}

### Suggested Related Links
{add_md}

## Identity Resolution
**Known aliases:** {identity_report.get('alias_count', 0)}

### Duplicate / Redirect Candidates
{alias_md}
"""
        _atomic_write_path(lint_path, content)
        file_written = f"_wiki/insights/{lint_path.name}"

        _append_log(vault_path / "_wiki" / "log.md",
                    f"\n## {time_str} · lint\nPages scanned: {len(pages)}\nHealth score: {report.get('health_score')}\nWritten to: {file_written}\n")

    result = {**report, "pages_scanned": len(pages), "file_written": file_written,
              "from_cache": False, "ran_at": datetime.now().isoformat()}
    _save_lint_cache(result, pages)
    return result


@app.post("/lint/apply-link-fixes")
async def apply_link_fixes(req: ApplyLinkFixesRequest):
    """Apply safe deterministic wikilink fixes across concept pages.

    Applies only:
    - remove duplicate wikilinks in-page
    - remove broken wikilinks to non-existent pages

    Does not auto-remove weak semantic links.
    """
    pages = vault_reader.list_concept_pages()
    if not pages:
        raise HTTPException(400, "No concept pages to process")

    valid_pages = {p.get("name") for p in pages if p.get("name")}
    vault_path = Path(os.environ.get("VAULT_PATH", "/Users/sakethv7/SakethVault"))
    concepts_dir = vault_path / "_wiki" / "concepts"

    changed_pages = []
    total_duplicate_removed = 0
    total_broken_removed = 0

    for p in pages:
        name = p.get("name")
        if not name:
            continue
        fp = concepts_dir / f"{name}.md"
        if not fp.exists():
            continue
        original = fp.read_text(encoding="utf-8")
        updated, stats = _apply_safe_link_fixes_to_content(original, valid_pages)

        if stats["duplicate_removed"] or stats["broken_removed"]:
            total_duplicate_removed += stats["duplicate_removed"]
            total_broken_removed += stats["broken_removed"]
            changed_pages.append({
                "page": name,
                "duplicate_removed": stats["duplicate_removed"],
                "broken_removed": stats["broken_removed"],
            })
            if not req.dry_run:
                _atomic_write_path(fp, updated)

    if changed_pages and not req.dry_run:
        wiki_writer._update_index()
        time_str = datetime.now().strftime("%Y-%m-%d %H:%M")
        _append_log(
            vault_path / "_wiki" / "log.md",
            f"\n## {time_str} · apply-link-fixes\n"
            f"Pages changed: {len(changed_pages)}\n"
            f"Duplicate links removed: {total_duplicate_removed}\n"
            f"Broken links removed: {total_broken_removed}\n"
        )

    return {
        "dry_run": req.dry_run,
        "pages_scanned": len(pages),
        "pages_changed": len(changed_pages),
        "duplicate_links_removed": total_duplicate_removed,
        "broken_links_removed": total_broken_removed,
        "changed_pages": changed_pages,
    }




# ── DELETE /page/{page_name} ─────────────────────────────────────────────────

@app.delete("/page/{page_name}")
async def delete_page(page_name: str):
    """Permanently delete a page from any vault folder."""
    vault_path = Path(os.environ.get("VAULT_PATH", "/Users/sakethv7/SakethVault"))
    wiki_dir = vault_path / "_wiki"
    page_name = identity.resolve_slug(page_name)

    # Search all folders for the page
    search_folders = ["cs", "science", "humanities", "insights", "sources", "open-threads", "meta"]
    page_path = None
    for folder in search_folders:
        candidate = wiki_dir / folder / f"{page_name}.md"
        if candidate.exists():
            page_path = candidate
            rel_path = f"_wiki/{folder}/{page_name}.md"
            break

    if page_path is None:
        raise HTTPException(404, f"Page '{page_name}' not found")

    page_path.unlink()
    try:
        memory_store.remove_page(page_name)
    except Exception as e:
        logger.warning("memory index delete failed for %s: %s", page_name, e)

    # Rebuild index (only matters for concepts but harmless for others)
    wiki_writer._update_index()

    time_str = datetime.now().strftime("%Y-%m-%d %H:%M")
    _append_log(wiki_dir / "log.md",
                f"\n## {time_str} · delete\nDeleted: {rel_path}\n")

    return {"success": True, "deleted": rel_path}


# ── POST /consolidate ─────────────────────────────────────────────────────────

def _draft_merge(source: str, target: str, source_content: str, target_content: str) -> str:
    """Ask the LLM for the merged page text. Writes nothing."""
    prompt = f"""You are merging two wiki pages about the same topic into one clean, canonical page.

TARGET page (keep this slug/title): [[{target}]]
{target_content}

SOURCE page (merge into target, then it will be deleted): [[{source}]]
{source_content}

Rules:
1. Deduplicate: if both pages have an entry from the same URL, keep only one (the fuller one)
2. Merge all unique ## sections, ordered chronologically by date (oldest first)
3. Standardise ALL wikilinks to kebab-case: [[ChainOfThought]] → [[chain-of-thought]], [[VectorDatabase]] → [[vector-database]]
4. Write a single clean YAML frontmatter block using the TARGET page's title and slug
5. Combine tags from both pages (no duplicates)
6. Set entry_count = total number of ## sections in the merged result
7. Set last_updated = today ({datetime.now().strftime("%Y-%m-%d")})
8. Output ONLY the final merged markdown file, nothing else"""

    merged = llm_client.complete(
        task="consolidate_pages",
        model=None,
        max_tokens=3000,
        messages=[{"role": "user", "content": prompt}],
    ).strip()
    # Strip accidental code fences
    if merged.startswith("```"):
        merged = merged.split("```", 2)[1]
        if merged.startswith("markdown") or merged.startswith("md"):
            merged = merged.split("\n", 1)[1]
        merged = merged.rstrip("`").strip()
    return merged


# Plain def: blocking LLM call; FastAPI runs it in a worker thread so
# other requests aren't frozen while it waits.
@app.post("/consolidate")
def consolidate(req: ConsolidateRequest):
    """
    Merge `source` page into `target` using Sonnet:
    - Deduplicates entries from the same URL
    - Merges all ## sections chronologically
    - Standardises wikilink slugs to kebab-case
    - Rewrites clean frontmatter
    - Deletes `source` after merge
    """
    vault_path = Path(os.environ.get("VAULT_PATH", "/Users/sakethv7/SakethVault"))
    wiki_dir = vault_path / "_wiki"

    def _find_page(name: str):
        for folder in ["cs", "science", "humanities", "insights", "sources", "open-threads"]:
            c = wiki_dir / folder / f"{name}.md"
            if c.exists():
                return c
        return None

    source = identity.slugify(req.source)
    target = identity.resolve_slug(req.target)
    safety = consolidation.validate_pair(source, target)
    if not req.force and not safety.get("safe_auto"):
        raise HTTPException(
            400,
            {
                "message": "Consolidation is not high-confidence. Re-run with force=true for a manual override.",
                "safety": safety,
            },
        )
    source_path = _find_page(source)
    target_path = _find_page(target)

    if not source_path:
        raise HTTPException(404, f"Source page '{source}' not found")
    if not target_path:
        raise HTTPException(404, f"Target page '{target}' not found")
    if source == target:
        raise HTTPException(400, "Source and target resolve to the same canonical page")

    source_content = source_path.read_text(encoding="utf-8")
    target_content = target_path.read_text(encoding="utf-8")
    source_sha = hashlib.sha256(source_content.encode()).hexdigest()
    target_sha = hashlib.sha256(target_content.encode()).hexdigest()

    if req.merged is not None:
        # Applying a previewed draft: refuse if either page changed since.
        if (req.source_sha, req.target_sha) != (source_sha, target_sha):
            raise HTTPException(409, "A page changed since the preview. Preview the merge again.")
        merged = req.merged
    else:
        input_chars = len(source_content) + len(target_content)
        if input_chars > CONSOLIDATE_MAX_INPUT_CHARS:
            raise HTTPException(
                413,
                f"These pages are too big to merge safely ({input_chars:,} characters; "
                f"limit {CONSOLIDATE_MAX_INPUT_CHARS:,}). The merged draft would be cut off.",
            )
        merged = _draft_merge(source, target, source_content, target_content)

    if req.dry_run:
        return {
            "success": True,
            "preview": merged,
            "source": source,
            "target": target,
            "source_sha": source_sha,
            "target_sha": target_sha,
            # A merged page much shorter than its inputs usually means the LLM
            # output hit max_tokens and was cut off.
            "input_chars": len(source_content) + len(target_content),
            "merged_chars": len(merged),
        }

    # Keep both originals so any merge can be undone by hand.
    backup_dir = wiki_dir / "meta" / "consolidation-backups" / datetime.now().strftime("%Y%m%d-%H%M%S")
    backup_dir.mkdir(parents=True, exist_ok=True)
    (backup_dir / f"{source}.md").write_text(source_content, encoding="utf-8")
    (backup_dir / f"{target}.md").write_text(target_content, encoding="utf-8")

    _atomic_write_path(target_path, merged)
    source_path.unlink()
    try:
        memory_store.remove_page(source)
        memory_store.index_page(target)
    except Exception as e:
        logger.warning("memory index consolidate update failed for %s -> %s: %s", source, target, e)

    wiki_writer._update_index()

    time_str = datetime.now().strftime("%Y-%m-%d %H:%M")
    _append_log(vault_path / "_wiki" / "log.md",
                f"\n## {time_str} · consolidate\nMerged: [[{source}]] → [[{target}]]\nDeleted: _wiki/concepts/{source}.md\n")

    return {
        "success": True,
        "merged_into": f"_wiki/concepts/{target}.md",
        "deleted": f"_wiki/concepts/{source}.md",
        "backup": str(backup_dir.relative_to(vault_path)),
    }


# ── POST /fix-page/{page_name} ───────────────────────────────────────────────

@app.post("/fix-page/{page_name}")
async def fix_page(page_name: str):
    """
    Auto-fix a concept page without LLM (pure Python):
    1. Standardise wikilinks to kebab-case  [[CamelCase]] → [[kebab-case]]
    2. Sort ## sections chronologically by the date in the heading
    3. Recount and update entry_count in frontmatter
    Returns counts of what was fixed.
    """
    vault_path = Path(os.environ.get("VAULT_PATH", "/Users/sakethv7/SakethVault"))
    wiki_dir = vault_path / "_wiki"
    page_path = None
    for folder in ["cs", "science", "humanities", "insights", "sources", "open-threads", "meta"]:
        candidate = wiki_dir / folder / f"{page_name}.md"
        if candidate.exists():
            page_path = candidate
            break

    if page_path is None:
        raise HTTPException(404, f"Page '{page_name}' not found")

    content = page_path.read_text(encoding="utf-8")
    original = content

    # 1. Standardise wikilinks: [[CamelCase]] or [[Title Case]] → [[kebab-case]]
    import re
    def _to_kebab(m):
        inner = m.group(1)
        # CamelCase → kebab-case
        kebab = re.sub(r"(?<=[a-z])(?=[A-Z])", "-", inner)
        kebab = re.sub(r"\s+", "-", kebab).lower()
        kebab = re.sub(r"[^\w-]", "", kebab)
        return f"[[{kebab}]]"

    content, wikilink_fixes = re.subn(r"\[\[([^\]]+)\]\]", _to_kebab, content)

    # 2. Sort ## sections chronologically
    # Split into frontmatter + title + sections
    parts = re.split(r"(?=^## )", content, flags=re.MULTILINE)
    if len(parts) > 2:
        header = parts[0]  # frontmatter + page title
        sections = parts[1:]

        def _section_date(s):
            m = re.search(r"·\s*(\d{4}-\d{2}-\d{2})", s)
            return m.group(1) if m else "0000-00-00"

        sections_sorted = sorted(sections, key=_section_date)
        content = header + "".join(sections_sorted)

    # 3. Recount entry_count
    section_count = len(re.findall(r"^## ", content, flags=re.MULTILINE))
    content = re.sub(r"(entry_count:\s*)\d+", f"\\g<1>{section_count}", content)

    changes = content != original
    if changes:
        _atomic_write_path(page_path, content)
        try:
            memory_store.index_page(page_name)
        except Exception as e:
            logger.warning("memory index update failed for fixed page %s: %s", page_name, e)

    return {
        "success": True,
        "page": page_name,
        "wikilinks_fixed": wikilink_fixes,
        "sections_sorted": len(parts) > 2,
        "entry_count_updated": section_count,
        "changes_made": changes,
    }


# ── POST /calculate-maturity/{page} ───────────────────────────────────────────

@app.post("/calculate-maturity/{page_name}")
async def calculate_maturity(page_name: str):
    """
    Calculate and store understanding maturity score (0-100) for a concept page.

    Formula (v2):
    - backlinks      (40%): pages that reference this one — best signal of load-bearing knowledge
    - evolution      (25%): how many times understanding was revisited/refined
    - source_count   (20%): capped at 5 — marginal value of source #6 is ~zero
    - activity       (10%): read recently (reads.jsonl) > just updated recently
    - contradictions  (5%): penalty for unresolved [!warning] callouts

    Updates frontmatter with understanding_maturity: 0-100
    """
    import re
    vault_path = Path(os.environ.get("VAULT_PATH", "/Users/sakethv7/SakethVault"))
    wiki_dir = vault_path / "_wiki"

    # Find the page
    page_path = None
    for folder in ["cs", "science", "humanities", "insights", "sources", "open-threads", "meta"]:
        candidate = wiki_dir / folder / f"{page_name}.md"
        if candidate.exists():
            page_path = candidate
            break

    if page_path is None:
        raise HTTPException(404, f"Page '{page_name}' not found")

    content = page_path.read_text(encoding="utf-8")

    # Parse frontmatter via canonical util (vault_reader._parse_frontmatter)
    fm = vault_reader._parse_frontmatter(content)
    if not fm:
        raise HTTPException(400, "Invalid page frontmatter")

    # Preserve raw block for rewrite later
    fm_match = re.match(r"^---\n(.*?)\n---\n", content, re.DOTALL)
    fm_text = fm_match.group(1) if fm_match else ""

    last_updated = fm.get("last_updated")
    try:
        understanding_version = int(fm.get("understanding_version", 1))
    except (ValueError, TypeError):
        understanding_version = 1

    # Count sources (## sections) — capped at 5, diminishing returns beyond that
    SOURCE_CAP = 5
    raw_source_count = len(re.findall(r"^## ", content, re.MULTILINE))
    source_count = min(raw_source_count, SOURCE_CAP)

    # Count contradictions ([!warning] callouts)
    contradiction_count = len(re.findall(r"\[!warning\]", content))

    # Count incoming links (backlinks) — pages that reference this one
    all_pages = list(wiki_dir.glob("**/*.md"))
    backlink_count = 0
    kebab_name = page_name.lower().replace(" ", "-")
    for page_file in all_pages:
        if page_file == page_path:
            continue
        try:
            text = page_file.read_text(encoding="utf-8")
            if f"[[{kebab_name}]]" in text or f"[[{page_name}]]" in text:
                backlink_count += 1
        except OSError:
            pass

    # Activity score: was this page read recently? (reads.jsonl)
    # Better than last_updated recency — a concept you return to is alive;
    # a concept that's just old but stable shouldn't be penalised.
    reads_path = vault_path / "_wiki" / "meta" / "reads.jsonl"
    days_since_read = 999
    if reads_path.exists():
        for line in reads_path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                r = json.loads(line)
                if r.get("concept") == page_name:
                    read_date = datetime.fromisoformat(r["ts"]).date()
                    gap = (datetime.now().date() - read_date).days
                    days_since_read = min(days_since_read, gap)
            except Exception:
                continue
    # Fall back to last_updated if never read via the app
    if days_since_read == 999 and last_updated:
        try:
            days_since_read = (datetime.now().date() - datetime.strptime(last_updated, "%Y-%m-%d").date()).days
        except (ValueError, TypeError):
            pass
    # Full score if touched within 14 days, linear decay to 0 at 90 days, floor 20
    activity_score = max(20, 100 - max(0, days_since_read - 14) * (80 / 76))

    # ── Weighted formula (total 100) ─────────────────────────────────────────
    # backlinks:      saturates at 8 (beyond that you have a pillar concept)
    # evolution:      saturates at 5 revisits
    # source:         capped at SOURCE_CAP above
    # activity:       0–100 score computed above
    # contradictions: flat penalty per unresolved warning
    backlink_score  = min(backlink_count / 8, 1.0) * 40
    evolution_score = min(understanding_version / 5, 1.0) * 25
    source_score    = (source_count / SOURCE_CAP) * 20
    activity_part   = (activity_score / 100) * 10
    contradiction_penalty = min(contradiction_count * 2.5, 5)   # max -5pts
    contradiction_part = 5 - contradiction_penalty

    score = backlink_score + evolution_score + source_score + activity_part + contradiction_part

    # Clamp to 0-100
    maturity_score = int(max(0, min(100, score)))

    # Update frontmatter with maturity score
    new_fm = re.sub(
        r"(understanding_version:\s*)\d+",
        f"\\g<1>{understanding_version}",
        fm_text
    )

    # Add or update understanding_maturity
    if "understanding_maturity:" in new_fm:
        new_fm = re.sub(
            r"(understanding_maturity:\s*)\d+",
            f"\\g<1>{maturity_score}",
            new_fm
        )
    else:
        # Add it before understanding_version if possible, otherwise at the end
        if "understanding_version:" in new_fm:
            new_fm = new_fm.replace(
                f"understanding_version: {understanding_version}",
                f"understanding_maturity: {maturity_score}\nunderstanding_version: {understanding_version}"
            )
        else:
            new_fm += f"\nunderstanding_maturity: {maturity_score}"

    new_content = content.replace(fm_match.group(0), f"---\n{new_fm}\n---\n")

    # Atomic write. Keep the file's mtime: a score is metadata, not an edit,
    # and Browse's "Updated" sort and Recent folder order by mtime.
    original_stat = page_path.stat()
    _atomic_write_path(page_path, new_content)
    os.utime(page_path, ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns))

    return {
        "success": True,
        "page": page_name,
        "understanding_maturity": maturity_score,
        "components": {
            "backlink_count": backlink_count,
            "backlink_score": round(backlink_score, 1),
            "understanding_version": understanding_version,
            "evolution_score": round(evolution_score, 1),
            "source_count": raw_source_count,
            "source_count_capped": source_count,
            "source_score": round(source_score, 1),
            "days_since_read": days_since_read if days_since_read < 999 else None,
            "activity_score": round(activity_score, 1),
            "contradiction_count": contradiction_count,
            "contradiction_part": round(contradiction_part, 1),
        },
    }


# ── POST /calculate-all-maturity ──────────────────────────────────────────────

@app.post("/calculate-all-maturity")
async def calculate_all_maturity():
    """
    Calculate maturity score for all concept pages.
    Returns list of updated pages with their scores.
    """
    vault_path = Path(os.environ.get("VAULT_PATH", "/Users/sakethv7/SakethVault"))
    wiki_dir = vault_path / "_wiki"

    results = []
    all_files = []
    for folder in ["cs", "science", "humanities"]:
        all_files.extend(sorted((wiki_dir / folder).glob("*.md")))
    for page_file in all_files:
        page_name = page_file.stem
        try:
            result = await calculate_maturity(page_name)
            results.append({
                "page": page_name,
                "maturity": result["understanding_maturity"],
                "success": True,
            })
        except Exception as e:
            results.append({
                "page": page_name,
                "error": str(e),
                "success": False,
            })

    return {
        "success": True,
        "total_pages": len(results),
        "success_count": sum(1 for r in results if r["success"]),
        "pages": results,
    }


# ── POST /add-link ────────────────────────────────────────────────────────────

class AddLinkRequest(BaseModel):
    from_page: str
    to_page: str

@app.post("/add-link")
async def add_link(req: AddLinkRequest):
    """
    Insert [[to_page]] wikilink into from_page.
    Appends to an existing 'See also:' line or creates one at the end.
    Zero LLM — pure string manipulation.
    """
    import re
    vault_path = Path(os.environ.get("VAULT_PATH", "/Users/sakethv7/SakethVault"))
    wiki_dir = vault_path / "_wiki"
    from_page = identity.resolve_slug(req.from_page)
    to_page = identity.resolve_slug(req.to_page)
    from_file = None
    for folder in ["cs", "science", "humanities", "insights", "sources", "open-threads"]:
        c = wiki_dir / folder / f"{from_page}.md"
        if c.exists():
            from_file = c
            break
    if from_file is None:
        raise HTTPException(404, f"Page '{from_page}' not found")

    content = from_file.read_text(encoding="utf-8")
    link = f"[[{to_page}]]"

    if link in content:
        return {"added": False, "message": "Link already exists"}

    # Append to existing 'See also:' line if present
    if re.search(r"^See also:", content, re.MULTILINE):
        content = re.sub(r"(^See also:.*)", rf"\1 {link}", content, flags=re.MULTILINE)
    else:
        content = content.rstrip() + f"\n\nSee also: {link}\n"

    _atomic_write_path(from_file, content)
    try:
        memory_store.index_page(from_page)
    except Exception as _e:
        logger.warning("memory index update failed for %s: %s", from_page, _e)
    return {"added": True, "message": f"Added {link} to {from_page}"}


# ── POST /create-stub ─────────────────────────────────────────────────────────

class CreateStubRequest(BaseModel):
    slug: str
    reason: str = ""

@app.post("/create-stub")
async def create_stub(req: CreateStubRequest):
    """
    Create a minimal stub concept page so it can be filled later via Capture.
    Zero LLM — pure template fill.
    """
    vault_path = Path(os.environ.get("VAULT_PATH", "/Users/sakethv7/SakethVault"))
    concepts_dir = vault_path / "_wiki" / "cs"
    concepts_dir.mkdir(parents=True, exist_ok=True)
    slug = identity.resolve_slug(req.slug)
    file_path = concepts_dir / f"{slug}.md"

    if file_path.exists():
        raise HTTPException(409, f"Page '{slug}' already exists")

    today = datetime.now().strftime("%Y-%m-%d")
    title = slug.replace("-", " ").title()
    reason_line = f"\n> *Created from health check: {req.reason}*" if req.reason else ""

    content = f"""---
title: "{title}"
tags: []
entry_count: 0
last_updated: {today}
understanding_version: 1
---

> **Current understanding** 🔵
> Stub — no entries yet. Add content via Capture.{reason_line}
"""
    _atomic_write_path(file_path, content)
    try:
        memory_store.index_page(slug)
    except Exception as _e:
        logger.warning("memory index update failed for stub %s: %s", slug, _e)
    return {"created": True, "slug": slug, "message": f"Created stub page '{title}'"}


# ── utilities ─────────────────────────────────────────────────────────────────

def _analysis_due(insights_path: Path, traces_path: Path,
                  last_attempt: Optional[datetime], now: datetime) -> bool:
    """True when a week has passed since the last successful analysis and no
    attempt was made in the last day. The attempt check stops a failing
    analysis from being retried (and billed) every hour."""
    if last_attempt and (now - last_attempt).total_seconds() < ANALYSIS_RETRY_BACKOFF_SECONDS:
        return False
    if insights_path.exists():
        from vault_reader import _parse_frontmatter
        last = _parse_frontmatter(insights_path.read_text(encoding="utf-8")).get("last_analyzed", "")
        if not last:
            return False
        return (now.date() - datetime.fromisoformat(str(last)).date()).days >= 7
    if traces_path.exists():
        # Never run before — run if we have at least 5 traces
        return sum(1 for l in traces_path.read_text().splitlines() if l.strip()) >= 5
    return False


async def _weekly_analysis_scheduler():
    """
    Background task: runs trace analysis automatically once a week.
    Checks every hour if a week has passed since last analysis.
    """
    last_attempt: Optional[datetime] = None
    while True:
        try:
            vault_path = Path(os.environ.get("VAULT_PATH", "/Users/sakethv7/SakethVault"))
            insights_path = vault_path / "_wiki" / "meta" / "system-insights.md"
            traces_path = vault_path / "_wiki" / "meta" / "traces.jsonl"

            if _analysis_due(insights_path, traces_path, last_attempt, datetime.now()):
                last_attempt = datetime.now()
                try:
                    await asyncio.to_thread(analyze_traces)
                except Exception as _e:
                    logger.warning("weekly trace analysis failed; next attempt in 24h: %s", _e)

        except Exception as _e:
            logger.warning("weekly analysis scheduler outer loop error: %s", _e)

        await asyncio.sleep(WEEKLY_ANALYSIS_INTERVAL_SECONDS)


def _append_trace(trace: dict) -> None:
    """Append one trace record to _wiki/meta/traces.jsonl (one JSON line per event)."""
    vault_path = Path(os.environ.get("VAULT_PATH", "/Users/sakethv7/SakethVault"))
    traces_path = vault_path / "_wiki" / "meta" / "traces.jsonl"
    traces_path.parent.mkdir(parents=True, exist_ok=True)
    with open(traces_path, "a", encoding="utf-8") as f:
        f.write(json.dumps(trace) + "\n")


def _load_extraction_hints() -> str:
    """
    Read the ## Prompt Hints section from system-insights.md.
    Returns a newline-joined string of hints, or "" if none exist.
    """
    vault_path = Path(os.environ.get("VAULT_PATH", "/Users/sakethv7/SakethVault"))
    insights_path = vault_path / "_wiki" / "meta" / "system-insights.md"
    if not insights_path.exists():
        return ""
    try:
        content = insights_path.read_text(encoding="utf-8")
        in_hints = False
        hints = []
        for line in content.splitlines():
            if line.startswith("## Prompt Hints"):
                in_hints = True
                continue
            if in_hints:
                if line.startswith("## "):
                    break  # next section
                if line.startswith("- ") and not line.startswith("<!-- "):
                    hints.append(line[2:].strip())
        return "\n".join(hints)
    except Exception:
        return ""


def _append_log(log_path: Path, text: str) -> None:
    """Append to log.md — never crashes caller even if file is locked/missing."""
    try:
        log_path.chmod(0o644)
    except Exception:
        pass
    try:
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(text)
    except Exception:
        pass


def _find_duplicate(url: str) -> Optional[str]:
    """Return a description of where url already exists, or None if unseen."""
    # Check pending queue
    for item in queue_manager.get_all():
        if item.get("url") == url:
            return f"pending in queue (id={item['id'][:8]}…)"
    # Check written source records
    sources_dir = Path(os.environ.get("VAULT_PATH", "/Users/sakethv7/SakethVault")) / "_wiki" / "sources"
    if sources_dir.exists():
        for f in sources_dir.glob("*.md"):
            if url in f.read_text(encoding="utf-8"):
                return f"already written ({f.name})"
    return None


def _slug_text(text: str) -> str:
    import re
    text = text.lower()
    text = re.sub(r"[^\w\s-]", "", text)
    text = re.sub(r"[\s_]+", "-", text).strip("-")
    return text or "note"


def _add_deep_dive_tag(file_path: str) -> None:
    """Add 'deep-dive' tag to a page's frontmatter if not already present."""
    p = Path(file_path)
    if not p.exists():
        return
    content = p.read_text(encoding="utf-8")
    if not content.startswith("---"):
        return
    end = content.find("---", 3)
    if end == -1:
        return
    fm = content[3:end]
    body = content[end:]
    # Use canonical parser to check existing tags before mutating
    existing_tags = vault_reader._parse_frontmatter(content).get("tags", [])
    if "deep-dive" in (existing_tags if isinstance(existing_tags, list) else []):
        return
    # Kept for backwards compat: also skip if raw text already has deep-dive
    if "deep-dive" in fm:
        return
    # Find tags line and inject deep-dive
    lines = fm.splitlines()
    new_lines = []
    for line in lines:
        if line.strip().startswith("tags:"):
            # tags: [a, b]  →  tags: [a, b, deep-dive]
            stripped = line.strip()[5:].strip()  # the "[a, b]" part
            if stripped.startswith("[") and stripped.endswith("]"):
                inner = stripped[1:-1].strip()
                if inner:
                    line = line[:line.index("tags:")] + f"tags: [{inner}, deep-dive]"
                else:
                    line = line[:line.index("tags:")] + "tags: [deep-dive]"
            new_lines.append(line)
        else:
            new_lines.append(line)
    new_fm = "\n".join(new_lines)
    new_content = "---" + new_fm + body
    _atomic_write_path(p, new_content)


def _write_open_thread(title: str, notes: str, tags: list, summary_bullets: list = []) -> str:
    """Write an open-thread stub. Returns the file path written."""
    vault_path = Path(os.environ.get("VAULT_PATH", "/Users/sakethv7/SakethVault"))
    threads_dir = vault_path / "_wiki" / "open-threads"
    threads_dir.mkdir(parents=True, exist_ok=True)
    today = datetime.now().strftime("%Y-%m-%d")
    slug = _slug_text(title)[:60]
    file_path = threads_dir / f"{slug}.md"
    tags_str = ", ".join(tags) if tags else ""
    bullets_md = "\n".join(f"- {b}" for b in summary_bullets) if summary_bullets else "- (see approved page)"
    notes_md = notes.strip() if notes.strip() else "- TBD — add notes here"
    content = f"""---
title: "{title}"
date: {today}
tags: [{tags_str}]
last_updated: {today}
status: want-to-explore
---

# {title}

## What I just learned
{bullets_md}

## What I want to go deeper on
{notes_md}
"""
    _atomic_write_path(file_path, content)
    return str(file_path.relative_to(vault_path))


def _atomic_write_path(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(content, encoding="utf-8")
    tmp.rename(path)


def _strip_frontmatter(content: str) -> str:
    """Remove YAML frontmatter block — not useful as LLM context."""
    if content.startswith("---"):
        end = content.find("---", 3)
        if end != -1:
            return content[end + 3:].lstrip("\n")
    return content


# ── /history ─────────────────────────────────────────────────────────────────

@app.get("/history")
async def get_history(limit: int = 20):
    """Return last `limit` ingest/consolidate/insight entries from log.md."""
    vault_path = Path(os.environ.get("VAULT_PATH", "/Users/sakethv7/SakethVault"))
    log_path = vault_path / "_wiki" / "log.md"
    if not log_path.exists():
        return {"entries": []}

    text = log_path.read_text(encoding="utf-8")
    # Split on section headers: ## YYYY-MM-DD HH:MM · type
    import re as _re
    raw_sections = _re.split(r"(?=^## \d{4}-\d{2}-\d{2})", text, flags=_re.MULTILINE)
    entries = []
    for section in raw_sections:
        section = section.strip()
        if not section:
            continue
        header_m = _re.match(r"^## (\d{4}-\d{2}-\d{2} \d{2}:\d{2}) · (\w+)", section)
        if not header_m:
            continue
        ts, entry_type = header_m.group(1), header_m.group(2)
        source_m = _re.search(r"^Source: (https?://\S+)", section, _re.MULTILINE)
        written_m = _re.search(r"^Written to:\s*(\S+)", section, _re.MULTILINE)
        tags_m = _re.search(r"^Tags:\s*(\[.+?\])", section, _re.MULTILINE)
        merged_m = _re.search(r"^Merged:\s*(.+)$", section, _re.MULTILINE)
        question_m = _re.search(r"^Q:\s*(.+)$", section, _re.MULTILINE)
        deleted_m = _re.search(r"^Deleted:\s*(\S+)", section, _re.MULTILINE)
        entries.append({
            "ts": ts,
            "type": entry_type,
            "source": (source_m.group(1).strip() if source_m else ""),
            "written_to": (written_m.group(1).strip() if written_m else ""),
            "deleted": (deleted_m.group(1).strip() if deleted_m else ""),
            "tags": (tags_m.group(1).strip() if tags_m else ""),
            "merged": (merged_m.group(1).strip() if merged_m else ""),
            "question": (question_m.group(1).strip() if question_m else ""),
        })
    # Most recent first
    entries.reverse()
    return {"entries": entries[:limit]}


# ── /log-read  ───────────────────────────────────────────────────────────────

class LogReadRequest(BaseModel):
    page: str
    duration_seconds: int = 0


@app.post("/log-read")
async def log_read(req: LogReadRequest):
    vault_path = Path(os.environ.get("VAULT_PATH", "/Users/sakethv7/SakethVault"))
    reads_path = vault_path / "_wiki" / "meta" / "reads.jsonl"
    reads_path.parent.mkdir(parents=True, exist_ok=True)
    entry = json.dumps({"ts": datetime.utcnow().isoformat(), "concept": req.page, "duration_seconds": req.duration_seconds})
    with reads_path.open("a", encoding="utf-8") as f:
        f.write(entry + "\n")
    return {"ok": True}


# ── /edit-page/{page} ────────────────────────────────────────────────────────

class EditPageRequest(BaseModel):
    updated_content: str


@app.post("/edit-page/{page_name}")
async def edit_page(page_name: str, req: EditPageRequest):
    import subprocess
    vault_path = Path(os.environ.get("VAULT_PATH", "/Users/sakethv7/SakethVault"))
    page_path = None
    for folder in ["cs", "science", "humanities", "insights", "open-threads", "sources"]:
        candidate = vault_path / "_wiki" / folder / f"{page_name}.md"
        if candidate.exists():
            page_path = candidate
            break
    if page_path is None:
        raise HTTPException(404, f"Page '{page_name}' not found")

    current = page_path.read_text(encoding="utf-8")

    # Extract existing frontmatter
    frontmatter = ""
    if current.startswith("---"):
        end = current.find("---", 3)
        if end != -1:
            frontmatter = current[: end + 3]

    # Validate updated_content doesn't start with frontmatter (we keep the original)
    body = req.updated_content
    if body.startswith("---"):
        # If user accidentally included frontmatter, strip it
        fm_end = body.find("---", 3)
        if fm_end != -1:
            body = body[fm_end + 3:].lstrip("\n")

    new_content = frontmatter + "\n" + body if frontmatter else body

    # Atomic write
    page_path.write_text(new_content, encoding="utf-8")

    # Git commit
    git_sha = ""
    try:
        subprocess.run(["git", "add", str(page_path)], cwd=str(vault_path), check=True, capture_output=True)
        result = subprocess.run(
            ["git", "commit", "-m", f"Updated {page_name} via web UI"],
            cwd=str(vault_path), check=True, capture_output=True, text=True,
        )
        sha_match = _re.search(r"\[[\w/]+ ([0-9a-f]+)\]", result.stdout)
        git_sha = sha_match.group(1) if sha_match else ""
    except Exception as e:
        logger.warning("git commit failed (write succeeded): %s", e)

    try:
        memory_store.index_page(page_name)
    except Exception as e:
        logger.warning("memory index update failed for edited page %s: %s", page_name, e)

    return {"success": True, "git_commit_sha": git_sha}


# ── /normalize-tags ──────────────────────────────────────────────────────────

@app.post("/normalize-tags")
async def normalize_tags_endpoint(payload: dict):
    tags = payload.get("tags", [])
    vault_path = Path(os.environ.get("VAULT_PATH", "/Users/sakethv7/SakethVault"))
    ontology_path = vault_path / "_wiki" / "meta" / "tag-ontology.json"
    if not ontology_path.exists():
        return {"normalized": tags, "mappings": {}}

    ontology = json.loads(ontology_path.read_text(encoding="utf-8"))

    # Build synonym → canonical lookup
    synonym_map: dict[str, str] = {}
    for canonical, info in ontology.items():
        for syn in info.get("synonyms", []):
            synonym_map[syn.lower()] = canonical

    normalized = []
    mappings = {}
    for tag in tags:
        canonical = synonym_map.get(tag.lower())
        if canonical and canonical != tag:
            mappings[tag] = canonical
            normalized.append(canonical)
        else:
            normalized.append(tag)

    # Deduplicate while preserving order
    seen_n: set[str] = set()
    deduped = []
    for t in normalized:
        if t not in seen_n:
            seen_n.add(t)
            deduped.append(t)

    return {"normalized": deduped, "mappings": mappings}


@app.get("/tag-ontology")
async def get_tag_ontology():
    vault_path = Path(os.environ.get("VAULT_PATH", "/Users/sakethv7/SakethVault"))
    ontology_path = vault_path / "_wiki" / "meta" / "tag-ontology.json"
    if not ontology_path.exists():
        return {}
    return json.loads(ontology_path.read_text(encoding="utf-8"))


# ── /random-concept ───────────────────────────────────────────────────────────

@app.get("/random-concept")
async def random_concept():
    pages = vault_reader.list_concept_pages()
    if not pages:
        raise HTTPException(400, "No concept pages found")
    page = random.choice(pages)
    return {"name": page["name"]}


# ── /rewrite-notes ────────────────────────────────────────────────────────────

class RewriteNotesRequest(BaseModel):
    bullets: list
    title: str = ""
    context: str = ""


# Plain def: blocking LLM call; FastAPI runs it in a worker thread so
# other requests aren't frozen while it waits.
@app.post("/store-image")
def store_image(req: IngestRequest):
    """
    Save a pasted image directly to _wiki/assets/ without LLM extraction.
    Returns the Obsidian-compatible embed path so the frontend can insert it.
    Also runs a quick vision pass to suggest a filename and caption.
    """
    request_started = time.perf_counter()
    stage_ms: dict[str, float] = {}

    def _mark_stage(name: str, started: float) -> None:
        stage_ms[name] = round((time.perf_counter() - started) * 1000, 1)

    all_images = list(req.images or [])
    if req.image_base64 and not all_images:
        all_images = [{"data": req.image_base64, "mediaType": "image/png"}]
    if not all_images:
        raise HTTPException(400, "No image provided")

    vault = _vault_path()
    assets_dir = vault / "_wiki" / "assets"
    assets_dir.mkdir(parents=True, exist_ok=True)

    today = datetime.now().strftime("%Y-%m-%d")
    saved = []
    max_caption_image_chars = int(os.environ.get("IMAGE_CAPTION_MAX_BASE64_CHARS", "300000"))

    for i, img in enumerate(all_images):
        media_type = img.get("mediaType", "image/png")
        ext = media_type.split("/")[-1].replace("jpeg", "jpg")
        started = time.perf_counter()
        raw_bytes = base64.b64decode(img["data"])
        _mark_stage(f"decode_image_{i + 1}", started)

        # Best-effort vision caption. This is optional asset metadata, so avoid
        # strict JSON contracts that create noisy reliability failures.
        started = time.perf_counter()
        if len(img.get("data", "")) > max_caption_image_chars:
            meta = {"slug": _fallback_image_slug(i), "caption": ""}
        else:
            try:
                caption_raw = llm_client.complete(
                    task="image_caption",
                    model=None,  # vision model selected by provider routing
                    max_tokens=120,
                    messages=[{"role": "user", "content": [
                        {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": img["data"]}},
                        {"type": "text", "text": 'Describe this image for a personal knowledge vault. Prefer JSON like {"slug":"2-4-word-kebab-slug","caption":"one sentence"}, but a short one-line caption is acceptable.'},
                    ]}],
                    expect_json=False,
                ).strip()
                meta = _parse_image_caption_response(caption_raw, index=i)
            except Exception:
                meta = {"slug": _fallback_image_slug(i), "caption": ""}
        _mark_stage(f"caption_image_{i + 1}", started)

        if len(img.get("data", "")) > max_caption_image_chars:
            stage_ms[f"caption_skipped_image_{i + 1}"] = 1.0

        slug = _re.sub(r"[^a-z0-9\-]", "", (meta.get("slug") or _fallback_image_slug(i)).lower().replace(" ", "-"))[:40] or _fallback_image_slug(i)
        suffix = f"-{i+1}" if i > 0 else ""
        filename = f"{today}-{slug}{suffix}.{ext}"
        file_path = assets_dir / filename

        started = time.perf_counter()
        file_path.write_bytes(raw_bytes)
        _mark_stage(f"write_image_{i + 1}", started)

        saved.append({
            "filename": filename,
            "obsidian_embed": f"![[{filename}]]",
            "caption": meta.get("caption", ""),
        })

    total_ms = round((time.perf_counter() - request_started) * 1000, 1)
    latency = {
        "total_ms": total_ms,
        "stage_ms": stage_ms,
        "source_type": "store_image",
        "image_count": len(all_images),
        "caption_skipped_count": sum(1 for img in all_images if len(img.get("data", "")) > max_caption_image_chars),
    }
    telemetry.log_context_event("store_image_latency", latency)
    return {"saved": saved, "latency": latency}


def _fallback_image_slug(index: int) -> str:
    return f"image-{index + 1}"


def _parse_image_caption_response(raw: str, index: int = 0) -> dict:
    text = (raw or "").strip()
    if not text:
        return {"slug": _fallback_image_slug(index), "caption": ""}

    parsed = None
    start = text.find("{")
    end = text.rfind("}") + 1
    if start >= 0 and end > start:
        try:
            parsed = json.loads(text[start:end])
        except json.JSONDecodeError:
            parsed = None
    if isinstance(parsed, dict):
        slug = str(parsed.get("slug") or "").strip()
        caption = str(parsed.get("caption") or "").strip()
    else:
        first_line = next((line.strip() for line in text.splitlines() if line.strip()), "")
        slug = first_line
        caption = first_line

    slug = identity.slugify(slug)[:40] or _fallback_image_slug(index)
    return {"slug": slug, "caption": caption[:220]}


class ExpandNotesRequest(BaseModel):
    bullets: list
    title: str = ""
    direction: str = ""  # e.g. "add a point about gradient accumulation"
    count: int = 2       # how many new bullets to add


# Plain def: blocking LLM call; FastAPI runs it in a worker thread so
# other requests aren't frozen while it waits.
@app.post("/expand-notes")
def expand_notes(req: ExpandNotesRequest):
    """
    Add new insight bullets in a given direction, appended to the existing set.
    """
    if not req.direction.strip():
        raise HTTPException(400, "direction is required")

    bullets_block = "\n".join(f"{i+1}. {b}" for i, b in enumerate(req.bullets))
    context_line = f"\nTopic: {req.title}" if req.title else ""

    prompt = f"""You are adding new insight bullets to an existing set of technical notes.{context_line}

Existing bullets:
{bullets_block}

Direction: {req.direction.strip()}

Write {req.count} new bullets that follow this direction. Rules:
- Each bullet is 1-2 sentences, dense with information
- No first-person ("I learned", "I found")
- State facts, mechanisms, and implications directly
- Do not repeat what is already covered above
- Match the technical depth and style of the existing bullets

Return ONLY a JSON array of the NEW bullets (not the existing ones), e.g.:
["New bullet 1.", "New bullet 2."]"""

    try:
        raw = llm_client.complete(
            task="expand_notes",
            model=None,
            max_tokens=600,
            messages=[{"role": "user", "content": prompt}],
            expect_json=False,
        ).strip()
        new_bullets = _parse_bullet_array(raw)
    except Exception as e:
        raise HTTPException(500, f"Expand failed: {e}")

    if not new_bullets:
        raise HTTPException(500, "Expand failed: model returned no usable bullets")

    return {"bullets": req.bullets + new_bullets}


def _parse_bullet_array(raw: str) -> list[str]:
    """
    Best-effort parser for note-generation endpoints.
    Accepts strict JSON arrays, fenced JSON, or plain markdown bullets.
    """
    text = (raw or "").strip()
    if not text:
        return []

    if text.startswith("```"):
        parts = text.split("```")
        for part in parts[1::2]:
            candidate = part.strip()
            if candidate.startswith("json"):
                candidate = candidate[4:].strip()
            if candidate:
                text = candidate
                break

    start = text.find("[")
    end = text.rfind("]") + 1
    if start >= 0 and end > start:
        try:
            parsed = json.loads(text[start:end])
            if isinstance(parsed, list):
                return [str(item).strip() for item in parsed if str(item).strip()]
        except json.JSONDecodeError:
            pass

    lines = []
    for line in text.splitlines():
        cleaned = _re.sub(r"^\s*(?:[-*•]|\d+[.)])\s+", "", line).strip()
        if cleaned and cleaned.lower() not in {"json", "new bullets:"}:
            lines.append(cleaned)
    return lines[:4]


# Plain def: blocking LLM call; FastAPI runs it in a worker thread so
# other requests aren't frozen while it waits.
@app.post("/rewrite-notes")
def rewrite_notes(req: RewriteNotesRequest):
    """
    Rewrite POV/first-person summary bullets as neutral, precise technical notes
    in the style of Lilian Weng's blog — factual, dense, no 'I learned' openers.
    """
    if not req.bullets:
        raise HTTPException(400, "bullets is required")

    bullets_block = "\n".join(f"{i+1}. {b}" for i, b in enumerate(req.bullets))
    context_line = f"\nTopic context: {req.title}" if req.title else ""
    extra_context = f"\nAdditional context: {req.context[:500]}" if req.context else ""

    prompt = f"""Rewrite the following notes as clean, neutral technical prose — the style of Lilian Weng's blog posts (lilianweng.github.io).{context_line}{extra_context}

Rules:
- No first-person ("I learned", "I found", "impressed me", "interesting that")
- State facts, mechanisms, and implications directly
- Keep the same number of bullets
- Each bullet 1-2 sentences, dense with information — no filler
- Preserve all technical details and specifics from the original
- Do not merge bullets or split them

Original notes:
{bullets_block}

Return ONLY a JSON array of strings (one per rewritten bullet), e.g.:
["Rewritten bullet 1.", "Rewritten bullet 2."]"""

    try:
        raw = llm_client.complete(
            task="rewrite_notes",
            model=None,
            max_tokens=800,
            messages=[{"role": "user", "content": prompt}],
            expect_json=True,
        ).strip()
        start = raw.find("[")
        end = raw.rfind("]") + 1
        bullets = json.loads(raw[start:end]) if start >= 0 else req.bullets
    except Exception as e:
        raise HTTPException(500, f"Rewrite failed: {e}")

    return {"bullets": bullets}


# ── /vault/rewrite-pov-notes ──────────────────────────────────────────────────

_POV_RE = _re.compile(
    r"^(- )(I learned that |I learned |I found that |I found |"
    r"This shows that |This shows |Select AI impressed|.+ impressed me|"
    r"I think |I believe |I noticed )",
    _re.IGNORECASE,
)

# In-memory task store: task_id -> progress dict
_polish_tasks: dict = {}


def _run_vault_polish(task_id: str) -> None:
    task = _polish_tasks[task_id]
    vault = _vault_path()

    # First pass: collect all pages that have POV bullets
    candidates = []
    for md_path in sorted(vault.rglob("*.md")):
        if "_wiki" not in str(md_path):
            continue
        try:
            content = md_path.read_text(encoding="utf-8")
        except Exception:
            continue
        lines = content.splitlines()
        pov_indices = [i for i, ln in enumerate(lines) if _POV_RE.match(ln.strip())]
        if pov_indices:
            candidates.append((md_path, lines, pov_indices))

    task["pages_total"] = len(candidates)
    task["status"] = "running" if candidates else "done"
    if not candidates:
        task["message"] = "No POV bullets found — vault is already clean."
        return

    pages_updated = 0
    bullets_updated = 0

    for md_path, lines, pov_indices in candidates:
        page_name = md_path.stem
        task["current_page"] = page_name
        task["pages_done"] = pages_updated

        pov_bullets = [lines[i].strip().lstrip("- ").strip() for i in pov_indices]
        try:
            raw = llm_client.complete(
                task="rewrite_notes",
                model=None,
                max_tokens=600,
                messages=[{"role": "user", "content":
                    "Rewrite these notes as neutral technical prose (no first-person, no 'I learned'). "
                    "Return ONLY a JSON array of strings with the same count:\n"
                    + "\n".join(f"{i+1}. {b}" for i, b in enumerate(pov_bullets))
                }],
                expect_json=True,
            ).strip()
            start = raw.find("[")
            end = raw.rfind("]") + 1
            rewritten = json.loads(raw[start:end]) if start >= 0 else None
        except Exception:
            rewritten = None

        if not rewritten or len(rewritten) != len(pov_indices):
            continue

        new_lines = list(lines)
        for idx, new_text in zip(pov_indices, rewritten):
            indent = len(lines[idx]) - len(lines[idx].lstrip())
            new_lines[idx] = " " * indent + "- " + new_text.strip().lstrip("- ")

        wiki_writer._atomic_write(md_path, "\n".join(new_lines) + "\n")
        pages_updated += 1
        bullets_updated += len(pov_indices)
        task["bullets_done"] = bullets_updated

    task["pages_done"] = pages_updated
    task["bullets_done"] = bullets_updated
    task["status"] = "done"
    task["message"] = f"Polished {bullets_updated} bullet{'s' if bullets_updated != 1 else ''} across {pages_updated} page{'s' if pages_updated != 1 else ''}."
    task["current_page"] = None

    if pages_updated:
        try:
            memory_store.sync_index()
        except Exception as _e:
            logger.warning("memory sync after vault polish failed: %s", _e)


@app.post("/vault/rewrite-pov-notes")
async def vault_rewrite_pov_notes():
    """Start a background vault polish task. Returns task_id for status polling."""
    task_id = str(uuid.uuid4())
    _polish_tasks[task_id] = {
        "status": "scanning",
        "pages_total": 0,
        "pages_done": 0,
        "bullets_done": 0,
        "current_page": None,
        "message": None,
    }
    loop = asyncio.get_running_loop()
    loop.run_in_executor(None, _run_vault_polish, task_id)
    return {"task_id": task_id}


@app.get("/vault/rewrite-pov-notes/status/{task_id}")
async def vault_rewrite_pov_notes_status(task_id: str):
    if task_id not in _polish_tasks:
        raise HTTPException(404, "Task not found")
    return _polish_tasks[task_id]


# ── /knowledge-gaps/{page_name} ───────────────────────────────────────────────

# Plain def: blocking LLM call; FastAPI runs it in a worker thread so
# other requests aren't frozen while it waits.
@app.post("/knowledge-gaps/{page_name}")
def knowledge_gaps(page_name: str):
    """
    Generate 5 questions Saketh probably can't answer yet from his own notes.
    Also returns prerequisites and a concept diagram.
    Replaces /generate-summary — we don't need summaries of our own notes, we need gaps.
    """
    content = vault_reader.read_page(page_name)
    if content is None:
        raise HTTPException(404, f"Page '{page_name}' not found")

    prompt = f"""You are reviewing a personal knowledge wiki page about "{page_name}".
Your job: find the GAPS — things the page touches on but doesn't fully explain,
questions a curious learner would ask after reading this that the page can't answer.

Page content:
{content[:4000]}

Return ONLY valid JSON with this exact structure (no markdown fences):
{{
  "gaps": [
    {{"q": "Question the page raises but doesn't fully answer?", "why": "Why this matters / what's missing"}},
    {{"q": "Another gap question?", "why": "..."}},
    {{"q": "A deeper follow-up?", "why": "..."}},
    {{"q": "A practical application question?", "why": "..."}},
    {{"q": "A connections question (how does this relate to X)?", "why": "..."}},
  ],
  "prerequisites": ["concept-slug-1", "concept-slug-2"],
  "diagram": "graph TD\\n  A[Core] --> B[Aspect1]\\n  A --> C[Aspect2]"
}}

Rules:
- gaps = exactly 5, each should feel like something you genuinely can't answer from the notes alone
- prerequisites = 2-4 concept slugs (kebab-case) the reader should understand first
- diagram = 6-10 nodes max, show how this concept connects to related ideas"""

    text = llm_client.complete(
        task="knowledge_gaps",
        model=None,
        max_tokens=1200,
        messages=[{"role": "user", "content": prompt}],
        expect_json=True,
        required_json_keys=["gaps", "prerequisites", "diagram"],
    ).strip()
    if text.startswith("```"):
        text = "\n".join(text.split("\n")[1:])
    if text.endswith("```"):
        text = "\n".join(text.split("\n")[:-1])

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        raise HTTPException(500, "Failed to parse knowledge gaps from model")


# ── mobile upload & QR code ──────────────────────────────────────────────────

def _local_ip() -> str:
    import socket
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


@app.get("/qr-code")
async def qr_code():
    """Return a QR code PNG pointing to the mobile upload page on the LAN."""
    import qrcode, io
    port = int(os.environ.get("PORT", 8001))
    url = f"http://{_local_ip()}:{port}/mobile"
    img = qrcode.make(url)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return Response(content=buf.getvalue(), media_type="image/png")


@app.get("/mobile")
async def mobile_upload_page():
    """Simple HTML upload page served to phones — no React needed."""
    port = int(os.environ.get("PORT", 8001))
    api_base = f"http://{_local_ip()}:{port}"
    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1">
  <title>FlashCapture</title>
  <style>
    * {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{ font-family: -apple-system, sans-serif; background: #fafaf8; color: #1c1917; padding: 24px 16px; }}
    h1 {{ font-size: 20px; font-weight: 700; margin-bottom: 4px; }}
    p.sub {{ font-size: 13px; color: #78716c; margin-bottom: 24px; }}
    label {{ display: block; font-size: 13px; font-weight: 600; color: #44403c; margin-bottom: 6px; }}
    textarea, input[type=text] {{ width: 100%; border: 1px solid #e7e5e4; border-radius: 12px;
      padding: 12px; font-size: 15px; background: white; margin-bottom: 16px; }}
    textarea {{ height: 80px; resize: none; }}
    .img-picker {{ width: 100%; border: 2px dashed #d6d3d1; border-radius: 12px;
      padding: 32px 16px; text-align: center; color: #a8a29e; font-size: 14px;
      cursor: pointer; margin-bottom: 16px; background: white; }}
    .img-picker input {{ display: none; }}
    #previews {{ display: flex; flex-wrap: wrap; gap: 8px; margin-bottom: 16px; }}
    #previews img {{ width: 80px; height: 80px; object-fit: cover; border-radius: 8px; }}
    button {{ width: 100%; background: #f97316; color: white; border: none;
      border-radius: 12px; padding: 14px; font-size: 16px; font-weight: 600; cursor: pointer; }}
    button:disabled {{ opacity: 0.5; }}
    #status {{ margin-top: 16px; font-size: 14px; text-align: center; color: #44403c; }}
  </style>
</head>
<body>
  <h1>📸 FlashCapture</h1>
  <p class="sub">Take a photo of your lecture slides to add to SakethWiki.</p>

  <label>Your understanding (optional)</label>
  <textarea id="notes" placeholder="What do you already understand about this? Helps the AI fill gaps..."></textarea>

  <label>Screenshots</label>
  <div class="img-picker" onclick="document.getElementById('filePicker').click()">
    Tap to take photos or choose from library
    <input type="file" id="filePicker" accept="image/*" multiple capture="environment">
  </div>
  <div id="previews"></div>

  <button id="submitBtn" onclick="submit()" disabled>Process</button>
  <div id="status"></div>

  <script>
    const files = [];
    document.getElementById('filePicker').addEventListener('change', e => {{
      Array.from(e.target.files).forEach(f => {{
        files.push(f);
        const img = document.createElement('img');
        img.src = URL.createObjectURL(f);
        document.getElementById('previews').appendChild(img);
      }});
      document.getElementById('submitBtn').disabled = files.length === 0;
    }});

    async function toB64(file) {{
      return new Promise(res => {{
        const r = new FileReader();
        r.onload = e => {{ const parts = e.target.result.split(','); res({{ data: parts[1], mediaType: file.type || 'image/jpeg' }}); }};
        r.readAsDataURL(file);
      }});
    }}

    async function submit() {{
      const btn = document.getElementById('submitBtn');
      const status = document.getElementById('status');
      btn.disabled = true;
      status.textContent = 'Processing…';
      try {{
        const images = await Promise.all(files.map(toB64));
        const notes = document.getElementById('notes').value.trim();
        const body = {{ images, source_type: 'lecture' }};
        if (notes) body.user_notes = notes;
        const res = await fetch('{api_base}/ingest', {{
          method: 'POST', headers: {{ 'Content-Type': 'application/json' }},
          body: JSON.stringify(body)
        }});
        if (!res.ok) throw new Error(await res.text());
        status.innerHTML = '✓ Queued for review in SakethWiki!';
        status.style.color = '#16a34a';
        files.length = 0;
        document.getElementById('previews').innerHTML = '';
        document.getElementById('notes').value = '';
      }} catch(e) {{
        status.textContent = 'Error: ' + e.message;
        status.style.color = '#dc2626';
        btn.disabled = false;
      }}
    }}
  </script>
</body>
</html>"""
    return Response(content=html, media_type="text/html")


# ── dev entrypoint ────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8001, reload=False)
