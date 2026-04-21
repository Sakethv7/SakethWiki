"""
SakethWiki FastAPI backend.

POST /ingest      — fetch URL / accept text/image, extract via Sonnet, stage to queue
GET  /queue       — list pending HITL items
POST /approve/{id} — approve or reject a queued item
POST /chat        — keyword-matched RAG chat via Haiku
GET  /pages       — list all concept pages with metadata
GET  /page/{name} — full content of a concept page
"""
import base64
import json
import logging
import os
import random
import re as _re
import uuid
from datetime import datetime
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

import anthropic
import httpx
from bs4 import BeautifulSoup
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

import queue_manager
import tag_classifier
import vault_reader
import wiki_writer

# ── app setup ────────────────────────────────────────────────────────────────

import asyncio
from contextlib import asynccontextmanager

@asynccontextmanager
async def lifespan(app):
    """Start background weekly analysis scheduler on app startup."""
    asyncio.create_task(_weekly_analysis_scheduler())
    yield

app = FastAPI(title="SakethWiki API", version="1.0.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

client = anthropic.Anthropic()

# ── tunable constants ─────────────────────────────────────────────────────────

URL_SCRAPE_CHAR_LIMIT   = 5000   # max chars kept from fetched URL body
URL_SCRAPE_LINK_LIMIT   = 20     # max external links scraped per page
RAG_TOP_K               = 10     # top-k pages considered for chat context
RAG_CONTEXT_BUDGET      = 6000   # total chars of vault context injected into chat
SELF_LEARN_TRACE_WINDOW = 100    # last N traces sent to Sonnet for weekly analysis
LINT_CACHE_TTL_SECONDS  = 86400  # 24 h — lint report cache validity
WEEKLY_ANALYSIS_INTERVAL_SECONDS = 3600  # scheduler checks every hour

# ─────────────────────────────────────────────────────────────────────────────

VALID_TAGS = [
    # ML / AI
    "RAG", "Agents", "Serving", "MLOps", "LLM", "Inference",
    "VectorDB", "Attention", "KVCache", "Quantization",
    "FineTuning", "Embeddings", "Agentic",
    # Tech / Engineering
    "Engineering", "Systems", "DevTools", "Product", "Security",
    # Finance / Business
    "Finance", "Investing", "Business", "Startups", "Economics",
    # Self-improvement / meta
    "Productivity", "Learning", "Health", "Mental-models", "Career",
]


# ── request/response models ──────────────────────────────────────────────────

class IngestRequest(BaseModel):
    url: Optional[str] = None
    text: Optional[str] = None
    image_base64: Optional[str] = None        # legacy single image
    images: Optional[list] = None             # [{data: b64, mediaType: "image/png"}, ...]
    force: bool = False
    deep_research: bool = False


class ApproveRequest(BaseModel):
    approved: bool
    redirect_note: Optional[str] = None
    # Optional human edits — overrides the extracted values before vault write
    edits: Optional[dict] = None
    open_thread: bool = False  # if True, add deep-dive tag to the concept page


class OpenThreadRequest(BaseModel):
    title: str
    notes: str = ""  # free-form "what I want to learn"
    tags: list = []


class ChatRequest(BaseModel):
    message: str
    history: list = []


class SaveAnswerRequest(BaseModel):
    question: str
    answer: str
    sources: list = []
    pages_read: list = []


class LintRequest(BaseModel):
    save: bool = False  # write lint report to _wiki/insights/ if True
    force_refresh: bool = False  # if True, bypass cache and re-run Sonnet scan


class ConsolidateRequest(BaseModel):
    source: str   # page to merge FROM (will be deleted after)
    target: str   # page to merge INTO (will be updated)


# ── /ingest ──────────────────────────────────────────────────────────────────

def _is_ios_shortcut(request: Request) -> bool:
    """Detect iOS Shortcuts / Share Sheet callers by User-Agent."""
    ua = request.headers.get("user-agent", "").lower()
    return any(s in ua for s in ("shortcuts", "cfnetwork", "darwin", "ios"))


@app.post("/ingest")
async def ingest(req: IngestRequest, request: Request):
    if not req.url and not req.text and not req.image_base64:
        raise HTTPException(400, "Provide url, text, or image_base64")

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

    # Step 1: fetch URL with httpx + parse with BeautifulSoup (zero LLM)
    if req.url:
        raw_text = _fetch_url(req.url)

    if req.text:
        raw_text = (raw_text + "\n\n" + req.text).strip()

    # Normalise images: merge legacy single image + new images list
    all_images = list(req.images or [])
    if req.image_base64 and not all_images:
        all_images = [{"data": req.image_base64, "mediaType": "image/png"}]

    # Step 2: call Sonnet to extract structured info (vault-aware)
    existing_pages = [p["name"] for p in vault_reader.list_concept_pages()]
    extraction = _extract_with_sonnet(raw_text, all_images, source_url, existing_pages)

    # Step 2b: deep research — run 6-lens analysis if requested
    deep_data = {}
    if req.deep_research and raw_text:
        try:
            deep_data = _deep_research_with_sonnet(raw_text, source_url, extraction)
        except Exception as _e:
            logger.warning("deep_research failed (non-blocking): %s", _e)

    # Step 3: stage to queue
    item_id = str(uuid.uuid4())
    item = {
        "id": item_id,
        "url": source_url,
        "title": extraction["title"],
        "key_concepts": extraction["key_concepts"],
        "summary": extraction["summary"],
        "suggested_page": extraction["suggested_page"],
        "suggested_wikilinks": extraction["suggested_wikilinks"],
        "tags": extraction["tags"],
        "references": extraction.get("references", []),
        "diagram": extraction.get("diagram", ""),
        "staged_at": datetime.now().isoformat(),
        "status": "pending",
    }
    if deep_data:
        item["lenses"] = deep_data.get("lenses", {})
        item["synthesis"] = deep_data.get("synthesis", "")
        item["open_questions"] = deep_data.get("open_questions", [])
    queue_manager.enqueue(item)

    return {
        "id": item_id,
        "diff_preview": {
            "title": item["title"],
            "summary": item["summary"],
            "suggested_page": item["suggested_page"],
            "suggested_wikilinks": item["suggested_wikilinks"],
            "tags": item["tags"],
            "key_concepts": item["key_concepts"],
            "references": item["references"],
            "diagram": item["diagram"],
            "lenses": item.get("lenses", {}),
            "synthesis": item.get("synthesis", ""),
            "open_questions": item.get("open_questions", []),
        },
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


def _extract_with_sonnet(text: str, images: Optional[list], source_url: str,
                         existing_pages: Optional[list] = None) -> dict:
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

    if content_len > 4000 or has_images:
        depth = "long-form"
        model = "claude-sonnet-4-6"   # complex reasoning / vision needed
        bullet_rule = "EXACTLY 5 bullets — the 5 most important, distinct, specific insights. Each 1-2 sentences. First-person ('I learned...'). No filler, no repetition."
        diagram_rule = "A simple, clean Mermaid diagram (6-10 nodes max). flowchart LR or graph LR preferred. Show only the core concept/architecture — not every detail. Labels must be short (3-5 words)."
        content_budget = 10000
        max_out = 1500
    elif content_len > 1500:
        depth = "medium"
        model = "claude-haiku-4-5-20251001"  # structured extraction, no deep reasoning
        bullet_rule = "3-4 bullets — each a distinct, specific insight. First-person ('I learned...'). No filler."
        diagram_rule = "A simple Mermaid diagram (5-8 nodes). flowchart LR preferred. Core concept only, short labels."
        content_budget = 6000
        max_out = 1200
    else:
        depth = "short"
        model = "claude-haiku-4-5-20251001"  # short structured extraction
        bullet_rule = "2-3 bullets — each a sharp, distinct insight. First-person."
        diagram_rule = "A minimal Mermaid diagram (3-5 nodes). Core idea only."
        content_budget = 3000
        max_out = 800

    user_content.append({
        "type": "text",
        "text": f"""Extract structured metadata from this content for a personal knowledge wiki.
This wiki covers anything educational: tech, ML/AI, finance, self-improvement, productivity, business, etc.

Source URL: {source_url}
Content depth: {depth} ({content_len} chars)
{standards_block}{hints_block}
{pages_hint}

Content:
{text[:content_budget]}

Respond with a JSON object (no markdown fences) with exactly these fields:
{{
  "title": "concise title of the source",
  "key_concepts": ["concept1", "concept2"],
  "summary": ["bullet 1", "bullet 2", ...],
  "suggested_page": "slug-for-concept-page (e.g. rag, kv-cache, compound-interest)",
  "suggested_wikilinks": ["related-concept-1", "related-concept-2"],
  "tags": ["Tag1", "Tag2"],
  "references": ["https://...", "https://..."],
  "diagram": "mermaid diagram as a single JSON string with \\n for newlines"
}}

Rules:
- summary: {bullet_rule}
- suggested_page: use an existing slug if one fits; otherwise create a lowercase-hyphenated slug
- suggested_wikilinks: 3-6 related concepts as kebab-case slugs — prefer existing page slugs listed above
- tags: pick 1-4 from this exact list only: {tags_list}
- key_concepts: 4-8 specific terms or ideas from the content
- references: pick the most valuable external links mentioned in the content (YouTube videos, GitHub repos, papers, key articles). Empty list [] if none. Max 5 links.
- diagram: {diagram_rule} Escape all newlines as \\n in the JSON string. No special chars in node labels.""",
    })

    message = client.messages.create(
        model=model,
        max_tokens=max_out,
        messages=[{"role": "user", "content": user_content}],
    )

    raw = message.content[0].text.strip()
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
        raw = raw.strip()

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        raise HTTPException(500, f"LLM returned invalid JSON: {e}\nRaw: {raw[:300]}")

    # Validate tags against allowed list
    data["tags"] = [t for t in data.get("tags", []) if t in VALID_TAGS]
    # Sanitize diagram: remove \n inside node labels — Mermaid doesn't support them
    if data.get("diagram"):
        import re as _re
        data["diagram"] = _re.sub(
            r'\[([^\]]*)\]',
            lambda m: '[' + m.group(1).replace('\\n', ' ').replace('\n', ' ') + ']',
            data["diagram"]
        )

    return data


def _deep_research_with_sonnet(text: str, source_url: str, base_extraction: dict) -> dict:
    """Run 6-lens deep research analysis on top of a base extraction."""
    content_budget = min(len(text), 12000)

    prompt = f"""You are a deep research analyst. Analyze this content through 6 distinct lenses.
Each lens must independently re-examine the content from a fundamentally different angle.
The tension between lenses is where the real insight lives.

Source: {source_url}
Title: {base_extraction.get('title', '')}

Content:
{text[:content_budget]}

Respond with a JSON object (no markdown fences) with exactly this structure:
{{
  "lenses": {{
    "technical": {{
      "label": "Technical",
      "finding": "2-3 sentences. What does the mechanism/data actually say? Strip narrative, focus on how it works.",
      "confidence": "high|medium|low"
    }},
    "economic": {{
      "label": "Economic",
      "finding": "2-3 sentences. Follow the money. Who pays, who profits, what incentives drive this?",
      "confidence": "high|medium|low"
    }},
    "historical": {{
      "label": "Historical",
      "finding": "2-3 sentences. What patterns repeat? What has been tried before? What context is missing?",
      "confidence": "high|medium|low"
    }},
    "contrarian": {{
      "label": "Contrarian",
      "finding": "2-3 sentences. What if the consensus here is wrong? Who benefits from the current framing? What is nobody saying?",
      "confidence": "high|medium|low"
    }},
    "first_principles": {{
      "label": "First Principles",
      "finding": "2-3 sentences. Forget everything. What are the fundamental truths? What is the simplest model that explains this?",
      "confidence": "high|medium|low"
    }},
    "practical": {{
      "label": "Practical",
      "finding": "2-3 sentences. What can I actually do with this? What is the most actionable takeaway?",
      "confidence": "high|medium|low"
    }}
  }},
  "synthesis": "3-4 sentences. Where do the lenses agree? Where do they contradict? What is the real insight that only emerges from all 6 angles together?",
  "open_questions": ["question this research raises but does not answer", "another open question"]
}}

Rules:
- Each lens MUST rethink the content, not just rephrase the same point
- Contrarian lens should feel like a different researcher who disagrees with the others
- Confidence reflects how much evidence in the content supports this lens's finding
- open_questions: 2-3 questions this content raises but does not answer"""

    message = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=2500,
        messages=[{"role": "user", "content": prompt}],
    )

    raw = message.content[0].text.strip()
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
        raw = raw.strip()

    # Find first { in case model adds prose
    brace = raw.find("{")
    if brace > 0:
        raw = raw[brace:]

    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {}


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
    if not req.url and not req.text and not req.image_base64:
        raise HTTPException(400, "Provide url, text, or image_base64")

    source_url = req.url or ""

    if source_url and not req.force:
        duplicate = _find_duplicate(source_url)
        if duplicate:
            raise HTTPException(409, f"Already ingested: {duplicate}")

    raw_text = _fetch_url(req.url) if req.url else ""
    if req.text:
        raw_text = (raw_text + "\n\n" + req.text).strip()

    all_images = list(req.images or [])
    if req.image_base64 and not all_images:
        all_images = [{"data": req.image_base64, "mediaType": "image/png"}]

    existing_pages = [p["name"] for p in vault_reader.list_concept_pages()]
    extraction = _extract_with_sonnet(raw_text, all_images, source_url, existing_pages)

    item = {
        "id": str(uuid.uuid4()),
        "url": source_url,
        "title": extraction["title"],
        "key_concepts": extraction["key_concepts"],
        "summary": extraction["summary"],
        "suggested_page": extraction["suggested_page"],
        "suggested_wikilinks": extraction["suggested_wikilinks"],
        "tags": extraction["tags"],
        "diagram": extraction.get("diagram", ""),
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


# ── /approve/{item_id} ────────────────────────────────────────────────────────

@app.post("/approve/{item_id}")
async def approve(item_id: str, req: ApproveRequest):
    item = queue_manager.get_by_id(item_id)
    if not item:
        raise HTTPException(404, f"Item {item_id} not found in queue")

    if not req.approved:
        queue_manager.remove(item_id)
        try:
            _append_trace({
                "ts": datetime.now().isoformat(),
                "url": item.get("url", ""),
                "source_type": "tweet" if _is_tweet_url(item.get("url", "")) else ("text" if not item.get("url") else "url"),
                "approved": False,
                "title": item.get("title", ""),
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
            })
        except Exception:
            pass
        return {"success": True, "action": "rejected", "file_written": None}

    # If queued via Share Sheet (pending_extraction=True), run extraction now
    if item.get("pending_extraction") and item.get("url"):
        try:
            raw_text = _fetch_url(item["url"])
            existing_pages = [p["name"] for p in vault_reader.list_concept_pages()]
            extraction = _extract_with_sonnet(raw_text, [], item["url"], existing_pages)
            item.update({
                "title": extraction["title"],
                "key_concepts": extraction.get("key_concepts", []),
                "summary": extraction["summary"],
                "suggested_page": extraction["suggested_page"],
                "suggested_wikilinks": extraction.get("suggested_wikilinks", []),
                "tags": extraction.get("tags", []),
                "references": extraction.get("references", []),
                "diagram": extraction.get("diagram", ""),
                "pending_extraction": False,
            })
        except Exception as _e:
            logger.warning("background extraction failed for item %s: %s", item_id, _e)

    # Snapshot original values before edits (for trace logging)
    item["_original_suggested_page"] = item.get("suggested_page", "")
    item["_original_tags"] = list(item.get("tags", []))

    # Merge any human edits onto the item before writing
    if req.edits:
        allowed = {"title", "summary", "suggested_page", "suggested_wikilinks", "tags", "diagram"}
        for k, v in req.edits.items():
            if k in allowed and v is not None:
                item[k] = v

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
        _append_trace({
            "ts": datetime.now().isoformat(),
            "url": item.get("url", ""),
            "source_type": "tweet" if _is_tweet_url(item.get("url", "")) else ("text" if not item.get("url") else "url"),
            "approved": True,
            "title": item.get("title", ""),
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
        })
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
    # Step 1: keyword-match to find relevant pages
    relevant_names = vault_reader.find_relevant_pages(req.message)

    # Step 2: read up to 5 most relevant pages with adaptive char budget
    pages_content = vault_reader.read_pages_content(relevant_names[:5])

    # Step 3: build context — strip frontmatter, distribute budget proportionally
    context_parts = []
    n_pages = len(pages_content)
    per_page = RAG_CONTEXT_BUDGET // n_pages if n_pages else RAG_CONTEXT_BUDGET
    for name, content in pages_content.items():
        body = _strip_frontmatter(content)
        context_parts.append(f"=== [[{name}]] ===\n{body[:per_page]}")
    context = "\n\n".join(context_parts) if context_parts else "No matching pages found yet."

    index_content = vault_reader.read_index()

    # Prompt caching: system (instructions + index) cached as a block — reused
    # between calls until the vault changes. Context pages cached separately in
    # the user turn — reused across queries that hit the same pages.
    # Cache writes: 1.25x normal price. Cache reads: 0.1x. Breaks even after 2 calls.
    system_block = [
        {
            "type": "text",
            "text": (
                "You are Saketh's personal AI knowledge assistant for his SakethWiki"
                " — ML/AI things learned in the wild.\n\n"
                "Answer from the wiki pages only. Be direct. Use [[PageName]] notation."
                " Say if info is missing.\n\n"
                f"Wiki index:\n{index_content[:800]}"
            ),
            "cache_control": {"type": "ephemeral"},
        }
    ]

    # Build history; inject cached context block into the first user turn
    history_messages = []
    for turn in req.history:
        history_messages.append({"role": turn["role"], "content": turn["content"]})

    # Current user turn: context (cacheable) + the actual question
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

    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=1200,
        system=system_block,
        messages=history_messages,
    )

    answer = response.content[0].text

    # If this is a self-knowledge query, attach the structured concept page
    knowledge_card = None
    if _is_knowledge_query(req.message):
        topic = _extract_topic(req.message, relevant_names)
        if topic:
            knowledge_card = vault_reader.parse_concept_page(topic)

    return {
        "answer": answer,
        "sources": [f"_wiki/concepts/{n}.md" for n in relevant_names],
        "pages_read": relevant_names,
        **({"knowledge_card": knowledge_card} if knowledge_card else {}),
    }


# ── /pages ───────────────────────────────────────────────────────────────────


@app.get("/health")
async def health():
    """Lightweight liveness probe — no DB, no LLM, just confirms the process is up."""
    return {"status": "ok"}


@app.get("/pages")
async def list_pages(folder: str = "concepts"):
    return {"pages": vault_reader.list_pages_in_folder(folder)}


# ── /tag-colors ──────────────────────────────────────────────────────────────

@app.get("/tag-colors")
async def get_tag_colors():
    """Return tag→group mapping. Frontend maps group→Tailwind color classes."""
    return tag_classifier.load()


# ── /page/{page_name} ─────────────────────────────────────────────────────────

@app.get("/page/{page_name}")
async def get_page(page_name: str):
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

    # Filter to last 30 days and only approved items
    from datetime import timedelta
    cutoff = datetime.now() - timedelta(days=30)
    recent_traces = []
    for t in traces:
        if not t.get("approved"):
            continue
        try:
            ts = datetime.fromisoformat(t.get("ts", ""))
        except (ValueError, TypeError):
            continue  # skip traces with missing or malformed timestamps
        if ts > cutoff:
            recent_traces.append(t)

    # Activity timeline: group by date
    activity_by_date = {}
    for trace in recent_traces:
        date = trace.get("ts", "").split("T")[0]
        activity_by_date[date] = activity_by_date.get(date, 0) + 1

    # Learning velocity: entries per week, unique concepts per week
    entries_per_week = len(recent_traces) / max(1, (datetime.now() - cutoff).days / 7)
    unique_concepts = len(set(t.get("final_page") for t in recent_traces if t.get("final_page")))
    concepts_per_week = unique_concepts / max(1, (datetime.now() - cutoff).days / 7)

    # Tags frequency
    tag_counts = {}
    for trace in recent_traces:
        for tag in trace.get("tags_final", []):
            tag_counts[tag] = tag_counts.get(tag, 0) + 1
    top_tags = sorted(tag_counts.items(), key=lambda x: x[1], reverse=True)[:10]

    # Source type frequency
    source_counts = {}
    for trace in recent_traces:
        source = trace.get("source_type", "unknown")
        source_counts[source] = source_counts.get(source, 0) + 1
    top_sources = sorted(source_counts.items(), key=lambda x: x[1], reverse=True)

    # New concepts this week
    week_cutoff = datetime.now() - timedelta(days=7)
    week_traces = [t for t in recent_traces if datetime.fromisoformat(t.get("ts", "")) > week_cutoff]
    new_concepts_week = len(set(t.get("final_page") for t in week_traces if t.get("final_page")))

    return {
        "period_days": 30,
        "total_approved": len(recent_traces),
        "unique_concepts": unique_concepts,
        "activity_by_date": activity_by_date,
        "learning_velocity": {
            "entries_per_week": round(entries_per_week, 2),
            "concepts_per_week": round(concepts_per_week, 2),
        },
        "top_tags": [{"tag": tag, "count": count} for tag, count in top_tags],
        "top_sources": [{"source": src, "count": count} for src, count in top_sources],
        "new_concepts_this_week": new_concepts_week,
    }


# ── /analyze-traces ──────────────────────────────────────────────────────────

@app.post("/analyze-traces")
async def analyze_traces():
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
    trace_summary = json.dumps(traces[-SELF_LEARN_TRACE_WINDOW:], indent=2)

    prompt = f"""You are analyzing usage traces from a personal knowledge wiki system to find patterns and suggest improvements.

Here are {len(traces)} traces (each is one approve/reject event):

{trace_summary}

Fields:
- approved: was the item approved (True) or rejected (False)
- suggested_page / final_page: what the AI suggested vs what the user chose
- page_corrected: True if user changed the suggested page
- evolution_type: extends/refines/supersedes/duplicates/contradicts
- was_duplicate: True if classified as duplicate
- tags_suggested / tags_final: AI-suggested vs user-approved tags
- tags_corrected: True if user changed tags
- source_type: tweet / url / text

Analyze these traces and write a structured insights report. Be specific — name actual page slugs, actual tags, actual patterns you see in the data.

Respond with a JSON object (no markdown fences):
{{
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
}}

prompt_hints must be actionable, specific, and short — they will be directly injected into the extraction system prompt. E.g.:
- "Twitter content about agent tooling maps to existing pages more often than it needs a new page — prefer existing slugs"
- "The tag Agentic is frequently corrected to Agents — use Agents for tool-use and orchestration content"
"""

    message = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=2000,
        messages=[{"role": "user", "content": prompt}],
    )
    raw = message.content[0].text.strip()
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

    return {
        "success": True,
        "traces_analyzed": len(traces),
        "insights": insights,
        "file_written": str(insights_path.relative_to(vault_path)),
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
    concepts_dir = vault_path / "_wiki" / "concepts"
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
async def review_queue(days: int = 30):
    """Return concept pages not updated in more than `days` days."""
    return {"pages": vault_reader.get_review_queue(days)}


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
    concepts_dir = vault_path / "_wiki" / "concepts"

    # Find page (case-insensitive)
    page_path = None
    for f in concepts_dir.glob("*.md"):
        if f.stem.lower() == req.page.lower():
            page_path = f
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


@app.get("/lint")
async def get_lint_cache():
    """
    Return the last cached lint report if it exists, or 204 if no cache.
    Use POST /lint to generate or refresh the health check report.
    """
    cached = _load_lint_cache()
    if cached is None:
        return Response(status_code=204)
    cached_clean = {k: v for k, v in cached.items() if not k.startswith("_")}
    cached_clean["cached"] = True
    return cached_clean


@app.post("/lint")
async def lint_wiki(req: LintRequest):
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

    message = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=4000,
        messages=[{"role": "user", "content": prompt}],
    )

    raw = message.content[0].text.strip()
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
            fix_msg = client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=4000,
                messages=[
                    {"role": "user", "content": prompt},
                    {"role": "assistant", "content": raw},
                    {"role": "user", "content": "Your previous response was not valid JSON. Return ONLY the raw JSON object, no prose or markdown."},
                ],
            )
            raw2 = fix_msg.content[0].text.strip().lstrip("```json").lstrip("```").rstrip("```").strip()
            report = json.loads(raw2)
        except Exception:
            raise HTTPException(500, f"LLM returned unparseable JSON: {first_err}\nRaw: {raw[:300]}")

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
"""
        _atomic_write_path(lint_path, content)
        file_written = f"_wiki/insights/{lint_path.name}"

        _append_log(vault_path / "_wiki" / "log.md",
                    f"\n## {time_str} · lint\nPages scanned: {len(pages)}\nHealth score: {report.get('health_score')}\nWritten to: {file_written}\n")

    result = {**report, "pages_scanned": len(pages), "file_written": file_written,
              "from_cache": False, "ran_at": datetime.now().isoformat()}
    _save_lint_cache(result, pages)
    return result


# ── DELETE /page/{page_name} ─────────────────────────────────────────────────

@app.delete("/page/{page_name}")
async def delete_page(page_name: str):
    """Permanently delete a page from any vault folder."""
    vault_path = Path(os.environ.get("VAULT_PATH", "/Users/sakethv7/SakethVault"))
    wiki_dir = vault_path / "_wiki"

    # Search all folders for the page
    search_folders = ["concepts", "insights", "sources", "open-threads", "meta"]
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

    # Rebuild index (only matters for concepts but harmless for others)
    wiki_writer._update_index()

    time_str = datetime.now().strftime("%Y-%m-%d %H:%M")
    _append_log(wiki_dir / "log.md",
                f"\n## {time_str} · delete\nDeleted: {rel_path}\n")

    return {"success": True, "deleted": rel_path}


# ── POST /consolidate ─────────────────────────────────────────────────────────

@app.post("/consolidate")
async def consolidate(req: ConsolidateRequest):
    """
    Merge `source` page into `target` using Sonnet:
    - Deduplicates entries from the same URL
    - Merges all ## sections chronologically
    - Standardises wikilink slugs to kebab-case
    - Rewrites clean frontmatter
    - Deletes `source` after merge
    """
    vault_path = Path(os.environ.get("VAULT_PATH", "/Users/sakethv7/SakethVault"))
    concepts_dir = vault_path / "_wiki" / "concepts"

    source_path = concepts_dir / f"{req.source}.md"
    target_path = concepts_dir / f"{req.target}.md"

    if not source_path.exists():
        raise HTTPException(404, f"Source page '{req.source}' not found")
    if not target_path.exists():
        raise HTTPException(404, f"Target page '{req.target}' not found")

    source_content = source_path.read_text(encoding="utf-8")
    target_content = target_path.read_text(encoding="utf-8")

    prompt = f"""You are merging two wiki pages about the same topic into one clean, canonical page.

TARGET page (keep this slug/title): [[{req.target}]]
{target_content}

SOURCE page (merge into target, then it will be deleted): [[{req.source}]]
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

    message = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=3000,
        messages=[{"role": "user", "content": prompt}],
    )

    merged = message.content[0].text.strip()
    # Strip accidental code fences
    if merged.startswith("```"):
        merged = merged.split("```", 2)[1]
        if merged.startswith("markdown") or merged.startswith("md"):
            merged = merged.split("\n", 1)[1]
        merged = merged.rstrip("`").strip()

    _atomic_write_path(target_path, merged)
    source_path.unlink()

    wiki_writer._update_index()

    time_str = datetime.now().strftime("%Y-%m-%d %H:%M")
    _append_log(vault_path / "_wiki" / "log.md",
                f"\n## {time_str} · consolidate\nMerged: [[{req.source}]] → [[{req.target}]]\nDeleted: _wiki/concepts/{req.source}.md\n")

    return {
        "success": True,
        "merged_into": f"_wiki/concepts/{req.target}.md",
        "deleted": f"_wiki/concepts/{req.source}.md",
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
    # fix-page only applies to concepts; search concepts first, then all folders
    wiki_dir = vault_path / "_wiki"
    page_path = wiki_dir / "concepts" / f"{page_name}.md"
    if not page_path.exists():
        for folder in ["insights", "sources", "open-threads", "meta"]:
            candidate = wiki_dir / folder / f"{page_name}.md"
            if candidate.exists():
                page_path = candidate
                break

    if not page_path.exists():
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
    page_path = wiki_dir / "concepts" / f"{page_name}.md"
    if not page_path.exists():
        for folder in ["insights", "sources", "open-threads", "meta"]:
            candidate = wiki_dir / folder / f"{page_name}.md"
            if candidate.exists():
                page_path = candidate
                break

    if not page_path.exists():
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

    # Atomic write
    _atomic_write_path(page_path, new_content)

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
    concepts_dir = vault_path / "_wiki" / "concepts"

    results = []
    for page_file in sorted(concepts_dir.glob("*.md")):
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
    concepts_dir = vault_path / "_wiki" / "concepts"
    from_file = concepts_dir / f"{req.from_page}.md"
    if not from_file.exists():
        raise HTTPException(404, f"Page '{req.from_page}' not found")

    content = from_file.read_text(encoding="utf-8")
    link = f"[[{req.to_page}]]"

    if link in content:
        return {"added": False, "message": "Link already exists"}

    # Append to existing 'See also:' line if present
    if re.search(r"^See also:", content, re.MULTILINE):
        content = re.sub(r"(^See also:.*)", rf"\1 {link}", content, flags=re.MULTILINE)
    else:
        content = content.rstrip() + f"\n\nSee also: {link}\n"

    _atomic_write_path(from_file, content)
    return {"added": True, "message": f"Added {link} to {req.from_page}"}


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
    concepts_dir = vault_path / "_wiki" / "concepts"
    slug = req.slug.lower().replace(" ", "-")
    slug = _re.sub(r"[^\w-]", "", slug)
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
    return {"created": True, "slug": slug, "message": f"Created stub page '{title}'"}


# ── utilities ─────────────────────────────────────────────────────────────────

async def _weekly_analysis_scheduler():
    """
    Background task: runs trace analysis automatically once a week.
    Checks every hour if a week has passed since last analysis.
    """
    while True:
        try:
            vault_path = Path(os.environ.get("VAULT_PATH", "/Users/sakethv7/SakethVault"))
            insights_path = vault_path / "_wiki" / "meta" / "system-insights.md"
            traces_path = vault_path / "_wiki" / "meta" / "traces.jsonl"

            should_run = False
            if insights_path.exists():
                from vault_reader import _parse_frontmatter
                meta = _parse_frontmatter(insights_path.read_text(encoding="utf-8"))
                last = meta.get("last_analyzed", "")
                if last:
                    from datetime import date
                    last_date = date.fromisoformat(last)
                    if (date.today() - last_date).days >= 7:
                        should_run = True
            elif traces_path.exists():
                # Never run before — run if we have at least 5 traces
                count = sum(1 for l in traces_path.read_text().splitlines() if l.strip())
                if count >= 5:
                    should_run = True

            if should_run:
                try:
                    # Import httpx to call our own endpoint internally
                    import httpx as _httpx
                    _httpx.post("http://localhost:8001/analyze-traces", timeout=60)
                except Exception as _e:
                    logger.warning("weekly analysis scheduler HTTP call failed: %s", _e)

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


@app.get("/recent-reads")
async def recent_reads(limit: int = 10):
    vault_path = Path(os.environ.get("VAULT_PATH", "/Users/sakethv7/SakethVault"))
    reads_path = vault_path / "_wiki" / "meta" / "reads.jsonl"
    if not reads_path.exists():
        return {"reads": []}
    lines = reads_path.read_text(encoding="utf-8").splitlines()
    # Parse last 200 lines, deduplicate keeping most recent occurrence
    seen = {}
    for line in reversed(lines[-200:]):
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
            concept = entry.get("concept", "")
            if concept and concept not in seen:
                seen[concept] = entry
        except Exception:
            continue
    ordered = sorted(seen.values(), key=lambda e: e.get("ts", ""), reverse=True)
    return {"reads": ordered[:limit]}


# ── /edit-page/{page} ────────────────────────────────────────────────────────

class EditPageRequest(BaseModel):
    updated_content: str


@app.post("/edit-page/{page_name}")
async def edit_page(page_name: str, req: EditPageRequest):
    import subprocess
    vault_path = Path(os.environ.get("VAULT_PATH", "/Users/sakethv7/SakethVault"))
    page_path = vault_path / "_wiki" / "concepts" / f"{page_name}.md"
    if not page_path.exists():
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


# ── /generate-summary/{page_name} ────────────────────────────────────────────

@app.post("/generate-summary/{page_name}")
async def generate_summary(page_name: str):
    content = vault_reader.read_page(page_name)
    if content is None:
        raise HTTPException(404, f"Page '{page_name}' not found")

    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

    prompt = f"""You are studying a personal knowledge wiki page about "{page_name}".

Here is the full page content:
{content[:4000]}

Generate a structured study summary. Return ONLY valid JSON with this exact structure (no markdown fences):
{{
  "one_liner": "One sentence (max 20 words) capturing the core concept",
  "paragraph": "2-3 sentence explanation in simple terms a smart person can grasp quickly",
  "prerequisites": ["concept1", "concept2"],
  "self_test": [
    {{"q": "Question?", "a": "Answer"}},
    {{"q": "Question?", "a": "Answer"}},
    {{"q": "Question?", "a": "Answer"}},
    {{"q": "Question?", "a": "Answer"}},
    {{"q": "Question?", "a": "Answer"}}
  ],
  "diagram": "graph TD\\n  A[Core] --> B[Aspect1]\\n  A --> C[Aspect2]"
}}

Rules: prerequisites = 2-4 items, self_test = exactly 5 items, diagram = 6-10 nodes max."""

    resp = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1500,
        messages=[{"role": "user", "content": prompt}]
    )

    text = resp.content[0].text.strip()
    if text.startswith("```"):
        text = "\n".join(text.split("\n")[1:])
    if text.endswith("```"):
        text = "\n".join(text.split("\n")[:-1])

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        raise HTTPException(500, "Failed to parse summary from model")


# ── dev entrypoint ────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8001, reload=False)
