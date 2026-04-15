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
import os
import re as _re
import uuid
from datetime import datetime
from pathlib import Path
from typing import Optional

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
from fastapi import FastAPI, HTTPException, Response
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

import queue_manager
import tag_classifier
import vault_reader
import wiki_writer

# ── app setup ────────────────────────────────────────────────────────────────

app = FastAPI(title="SakethWiki API", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

client = anthropic.Anthropic()

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


class ConsolidateRequest(BaseModel):
    source: str   # page to merge FROM (will be deleted after)
    target: str   # page to merge INTO (will be updated)


# ── /ingest ──────────────────────────────────────────────────────────────────

@app.post("/ingest")
async def ingest(req: IngestRequest):
    if not req.url and not req.text and not req.image_base64:
        raise HTTPException(400, "Provide url, text, or image_base64")

    raw_text = ""
    source_url = req.url or ""

    # Deduplication: reject if URL is already pending in queue or written to vault
    if source_url and not req.force:
        duplicate = _find_duplicate(source_url)
        if duplicate:
            raise HTTPException(409, f"Already ingested: {duplicate}")

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
        except Exception:
            pass  # deep research is best-effort, never block the ingest

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
            if len(ext_links) >= 20:  # cap — let Sonnet pick the useful ones
                break

        links_block = ""
        if ext_links:
            links_block = "\n\nReferenced links:\n" + "\n".join(f"- {u}" for u in ext_links)

        return text[:5000] + links_block
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
        (text or "") + " " + source_url, existing_pages, max_pages=10
    )
    pages_hint = (
        "Existing concept pages (prefer mapping to these over creating new ones):\n"
        + ", ".join(relevant_pages)
        if relevant_pages else "No pages yet."
    )

    wiki_standards = _load_wiki_standards("For Ingest")
    standards_block = f"\nCuration standards to follow:\n{wiki_standards}\n" if wiki_standards else ""

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
{standards_block}
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

@app.post("/queue-url")
async def queue_url(req: IngestRequest):
    """
    Lightweight endpoint for the iOS Share Sheet.
    Queues a URL instantly (no fetch, no LLM) so the shortcut
    gets a response in <1s. Full extraction happens at approve time.
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
        "title": url,                      # placeholder — replaced at approve time
        "key_concepts": [],
        "summary": ["Queued from iOS Share Sheet — tap Approve to extract."],
        "suggested_page": "unprocessed",
        "suggested_wikilinks": [],
        "tags": [],
        "diagram": "",
        "pending_extraction": True,        # flag so UI shows "needs extraction"
        "queued_at": datetime.now().isoformat(),
    }
    queue_manager.enqueue(item)
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
        except Exception:
            pass  # if extraction fails, write with placeholder content

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
    except Exception:
        pass  # non-critical, don't fail the approval

    # Classify any new tags so the frontend has colors for them
    try:
        api_key = os.environ.get("ANTHROPIC_API_KEY", "")
        tag_classifier.classify_new_tags(item.get("tags", []), api_key)
    except Exception:
        pass  # non-critical

    # If deep-dive requested, add tag to the written page's frontmatter
    deep_dive_tagged = False
    if req.open_thread:
        try:
            _add_deep_dive_tag(file_path)
            deep_dive_tagged = True
        except Exception:
            pass  # non-critical

    evolution = item.get("_evolution", {})
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
    # Total context budget: ~6000 chars (~1500 tokens) split across pages
    CONTEXT_BUDGET = 6000
    pages_content = vault_reader.read_pages_content(relevant_names[:5])

    # Step 3: build context — strip frontmatter, distribute budget proportionally
    context_parts = []
    n_pages = len(pages_content)
    per_page = CONTEXT_BUDGET // n_pages if n_pages else CONTEXT_BUDGET
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
    return {"name": page_name, "content": content, "parsed": parsed}


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

_LINT_CACHE_PATH = Path(__file__).parent / "lint_cache.json"


def _load_lint_cache() -> Optional[dict]:
    try:
        if _LINT_CACHE_PATH.exists():
            return json.loads(_LINT_CACHE_PATH.read_text())
    except Exception:
        pass
    return None


def _save_lint_cache(report: dict) -> None:
    try:
        _LINT_CACHE_PATH.write_text(json.dumps(report))
    except Exception:
        pass


@app.get("/lint")
async def get_lint_cache():
    """Return the last cached lint report, or 204 if none exists yet."""
    cached = _load_lint_cache()
    if cached is None:
        return Response(status_code=204)
    return cached


@app.post("/lint")
async def lint_wiki(req: LintRequest):
    """
    Scan the entire wiki with Sonnet and return a structured health report:
    - inconsistencies across pages
    - missing connections between concepts
    - suggested new articles
    - orphaned pages (no wikilinks pointing to them)
    Optionally saves the report as _wiki/insights/YYYY-MM-DD-lint.md
    """
    pages = vault_reader.list_concept_pages()
    if not pages:
        raise HTTPException(400, "No concept pages to lint yet.")

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
              "ran_at": datetime.now().strftime("%Y-%m-%d %H:%M")}
    _save_lint_cache(result)
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


# ── utilities ─────────────────────────────────────────────────────────────────

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
    # Check if deep-dive already in tags
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


# ── dev entrypoint ────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8001, reload=False)
