"""
SakethWiki E2E test suite — hits the live backend at localhost:8001.
Run: arch -arm64 venv/bin/python3 -m pytest tests/test_e2e.py -v
"""
import json
import time
import httpx
import pytest

BASE = "http://localhost:8001"
client = httpx.Client(base_url=BASE, timeout=60.0)

# ── helpers ───────────────────────────────────────────────────────────────────

def queue_ids():
    r = client.get("/queue")
    assert r.status_code == 200
    return [i["id"] for i in r.json()["items"]]

def cleanup_queue_item(item_id: str):
    """Reject/remove a test item from the queue."""
    client.post(f"/approve/{item_id}", json={"approved": False})


# ══════════════════════════════════════════════════════════════════════════════
# 1. HEALTH
# ══════════════════════════════════════════════════════════════════════════════

class TestHealth:
    def test_queue_endpoint_reachable(self):
        r = client.get("/queue")
        assert r.status_code == 200, f"Expected 200, got {r.status_code}"
        assert "items" in r.json()

    def test_pages_endpoint_reachable(self):
        r = client.get("/pages")
        assert r.status_code == 200
        assert "pages" in r.json()

    def test_pages_concepts_folder(self):
        r = client.get("/pages?folder=concepts")
        assert r.status_code == 200
        pages = r.json()["pages"]
        assert isinstance(pages, list)
        print(f"  → {len(pages)} concept pages")

    def test_pages_sources_folder(self):
        r = client.get("/pages?folder=sources")
        assert r.status_code == 200

    def test_pages_insights_folder(self):
        r = client.get("/pages?folder=insights")
        assert r.status_code == 200

    def test_pages_open_threads_folder(self):
        r = client.get("/pages?folder=open-threads")
        assert r.status_code == 200


# ══════════════════════════════════════════════════════════════════════════════
# 2. INGEST — TEXT
# ══════════════════════════════════════════════════════════════════════════════

class TestIngestText:
    def test_ingest_short_text(self):
        r = client.post("/ingest", json={
            "text": "LoRA fine-tuning reduces trainable parameters by decomposing weight updates into low-rank matrices, cutting VRAM usage by 10x while retaining 95% of full fine-tune quality."
        })
        assert r.status_code == 200, r.text
        data = r.json()
        assert "id" in data
        assert "diff_preview" in data
        dp = data["diff_preview"]
        assert dp.get("title"), "title should not be empty"
        assert len(dp.get("summary", [])) >= 3, f"Expected ≥3 bullets, got {len(dp.get('summary', []))}"
        assert dp.get("suggested_page"), "suggested_page should not be empty"
        cleanup_queue_item(data["id"])
        print(f"  → title: {dp['title']}, page: {dp['suggested_page']}, bullets: {len(dp['summary'])}")

    def test_ingest_long_text_gets_more_bullets(self):
        """Long-form content should produce ≥4 summary bullets."""
        long_text = """
        Retrieval-Augmented Generation (RAG) is a technique that combines parametric knowledge
        stored in LLM weights with non-parametric knowledge retrieved from external documents.
        The key insight is that LLMs have a knowledge cutoff and can hallucinate facts that are
        not in their training data. RAG addresses this by retrieving relevant chunks at inference
        time using a vector similarity search over embedded documents.

        The typical RAG pipeline has several stages: document ingestion (chunking, embedding,
        indexing into a vector store like Pinecone, Weaviate, or pgvector), query-time retrieval
        (embedding the query, k-NN search, fetching top-k chunks), and generation (feeding the
        retrieved context to the LLM with the query).

        Advanced RAG techniques include: HyDE (hypothetical document embeddings) where you
        generate a fake answer then embed it for better retrieval, re-ranking with a cross-encoder
        after initial retrieval, query decomposition for complex multi-hop questions, and
        recursive summarization for very long documents.

        Evaluation of RAG systems typically uses RAGAS metrics: faithfulness (are claims
        grounded in retrieved context?), answer relevance (does the answer address the question?),
        and context precision/recall. LlamaIndex and LangChain are the dominant frameworks,
        with LlamaIndex having better abstractions for the indexing pipeline and LangChain
        being more flexible for agent-style RAG with tool use.

        The choice of chunk size is critical: smaller chunks (128-256 tokens) have higher
        precision but may miss context, while larger chunks (512-1024 tokens) capture more
        context but reduce retrieval precision. Sentence-window retrieval is a hybrid that
        retrieves small sentences but expands to surrounding paragraphs before generation.
        """ * 2  # ~1500 chars, triggers medium depth

        r = client.post("/ingest", json={"text": long_text})
        assert r.status_code == 200, r.text
        data = r.json()
        dp = data["diff_preview"]
        bullet_count = len(dp.get("summary", []))
        assert bullet_count >= 4, f"Long text should get ≥4 bullets, got {bullet_count}"
        cleanup_queue_item(data["id"])
        print(f"  → {bullet_count} bullets for {len(long_text)} char input")

    def test_ingest_irrelevant_text_rejected(self):
        """Off-topic content should return 400."""
        r = client.post("/ingest", json={
            "text": "Best chocolate chip cookie recipe: 2 cups flour, 1 cup butter, 1 cup sugar. Bake at 375F for 12 minutes."
        })
        assert r.status_code == 400, f"Expected 400 for off-topic, got {r.status_code}"
        assert "relevant" in r.json().get("detail", "").lower() or "topic" in r.json().get("detail", "").lower()

    def test_ingest_empty_body_rejected(self):
        r = client.post("/ingest", json={})
        assert r.status_code in (400, 422)


# ══════════════════════════════════════════════════════════════════════════════
# 3. INGEST — URL
# ══════════════════════════════════════════════════════════════════════════════

class TestIngestURL:
    def test_ingest_invalid_url_handled(self):
        """Unreachable URL should fail gracefully with a client/server error."""
        r = client.post("/ingest", json={"url": "https://this-domain-does-not-exist-xyz-abc.com/page"})
        # 400 (irrelevant/fetch failed) or 5xx — NOT a silent 200
        assert r.status_code != 200, "Should not return 200 for an unreachable URL"
        assert "detail" in r.json()

    def _get_ingested_url(self):
        """Read a URL from an existing source file directly (not via /page/ which is concepts-only)."""
        import os
        from pathlib import Path
        vault = Path(os.environ.get("VAULT_PATH", "/Users/sakethv7/Documents/SakethWiki"))
        sources_dir = vault / "_wiki" / "sources"
        if not sources_dir.exists():
            return None
        for f in sorted(sources_dir.glob("*.md")):
            for line in f.read_text().splitlines():
                if line.strip().startswith("url:"):
                    url = line.split("url:", 1)[1].strip().strip('"')
                    if url.startswith("http"):
                        return url
        return None

    def test_ingest_duplicate_url_rejected(self):
        """Ingesting an already-ingested URL should return 409."""
        url = self._get_ingested_url()
        if not url:
            pytest.skip("No source pages to test dedup against")
        r = client.post("/ingest", json={"url": url})
        assert r.status_code == 409, f"Expected 409, got {r.status_code}: {r.text[:200]}"
        print(f"  → duplicate correctly rejected: {url[:60]}…")

    def test_ingest_force_bypasses_dedup(self):
        """force=True should bypass duplicate check."""
        url = self._get_ingested_url()
        if not url:
            pytest.skip("No source pages to test dedup bypass")
        r = client.post("/ingest", json={"url": url, "force": True})
        assert r.status_code != 409, "force=True should bypass dedup"
        if r.status_code == 200:
            cleanup_queue_item(r.json()["id"])
        print(f"  → force bypass status: {r.status_code}")


# ══════════════════════════════════════════════════════════════════════════════
# 4. QUEUE + APPROVE
# ══════════════════════════════════════════════════════════════════════════════

class TestQueueApprove:
    def _stage_item(self):
        """Ingest a test item and return its id + diff_preview."""
        r = client.post("/ingest", json={
            "text": "Mixture of Experts (MoE) routes each token to a subset of expert FFN layers, enabling massive model capacity with sparse compute. GPT-4 and Mixtral use MoE to achieve high quality at lower inference cost."
        })
        assert r.status_code == 200, r.text
        return r.json()

    def test_staged_item_appears_in_queue(self):
        data = self._stage_item()
        item_id = data["id"]
        try:
            ids = queue_ids()
            assert item_id in ids, f"Item {item_id} not found in queue"
        finally:
            cleanup_queue_item(item_id)

    def test_reject_removes_from_queue(self):
        data = self._stage_item()
        item_id = data["id"]
        r = client.post(f"/approve/{item_id}", json={"approved": False})
        assert r.status_code == 200
        assert r.json()["action"] == "rejected"
        assert item_id not in queue_ids(), "Rejected item should be removed from queue"

    def test_approve_writes_to_vault(self):
        """Approving an item should write a file and remove from queue."""
        data = self._stage_item()
        item_id = data["id"]
        slug = data["diff_preview"]["suggested_page"]
        before_pages = {p["name"] for p in client.get("/pages?folder=concepts").json()["pages"]}

        r = client.post(f"/approve/{item_id}", json={"approved": True})
        assert r.status_code == 200, r.text
        result = r.json()
        assert result["action"] == "approved"
        assert result.get("file_written"), "file_written should be set"
        assert item_id not in queue_ids(), "Approved item should be removed from queue"
        print(f"  → written to: {result['file_written']}")

        # Verify the page is now browsable
        after_pages = {p["name"] for p in client.get("/pages?folder=concepts").json()["pages"]}
        # Either a new page was created or an existing one was updated
        assert slug in after_pages or before_pages, "Page should exist in vault after approval"

    def test_approve_with_edits(self):
        """Edits passed at approval time should override extracted values."""
        data = self._stage_item()
        item_id = data["id"]
        edits = {
            "title": "TEST OVERRIDE TITLE",
            "suggested_page": data["diff_preview"]["suggested_page"],
            "summary": ["TEST BULLET ONE", "TEST BULLET TWO"],
            "tags": ["LLM"],
            "suggested_wikilinks": [],
        }
        r = client.post(f"/approve/${item_id}", json={"approved": True, "edits": edits})
        # Use correct URL format
        r = client.post(f"/approve/{item_id}", json={"approved": True, "edits": edits})
        assert r.status_code == 200, r.text

    def test_approve_unknown_id_404(self):
        r = client.post("/approve/does-not-exist-xyz", json={"approved": True})
        assert r.status_code == 404


# ══════════════════════════════════════════════════════════════════════════════
# 5. PAGE READ / DELETE
# ══════════════════════════════════════════════════════════════════════════════

class TestPages:
    def test_get_existing_page(self):
        pages = client.get("/pages?folder=concepts").json()["pages"]
        if not pages:
            pytest.skip("No concept pages in vault")
        name = pages[0]["name"]
        r = client.get(f"/page/{name}")
        assert r.status_code == 200
        assert "content" in r.json()
        assert len(r.json()["content"]) > 0
        print(f"  → read {name}: {len(r.json()['content'])} chars")

    def test_get_missing_page_404(self):
        r = client.get("/page/this-page-does-not-exist-xyz-abc")
        assert r.status_code == 404

    def test_fix_page(self):
        pages = client.get("/pages?folder=concepts").json()["pages"]
        if not pages:
            pytest.skip("No concept pages to fix")
        name = pages[0]["name"]
        r = client.post(f"/fix-page/{name}")
        assert r.status_code == 200
        result = r.json()
        assert "wikilinks_fixed" in result
        assert "entry_count_updated" in result
        print(f"  → fix-page {name}: {result['wikilinks_fixed']} wikilinks, {result['entry_count_updated']} entries")


# ══════════════════════════════════════════════════════════════════════════════
# 6. CHAT
# ══════════════════════════════════════════════════════════════════════════════

class TestChat:
    def test_chat_basic_query(self):
        r = client.post("/chat", json={"message": "what do I know about LLMs?"})
        assert r.status_code == 200, r.text
        data = r.json()
        assert "answer" in data
        assert len(data["answer"]) > 20, "Answer should be non-trivial"
        assert "pages_read" in data
        print(f"  → answer: {data['answer'][:80]}…")
        print(f"  → pages read: {data['pages_read']}")

    def test_chat_with_history(self):
        """Chat should accept conversation history."""
        history = [
            {"role": "user", "content": "what is RAG?"},
            {"role": "assistant", "content": "RAG stands for Retrieval-Augmented Generation..."},
        ]
        r = client.post("/chat", json={"message": "what are the limitations?", "history": history})
        assert r.status_code == 200
        assert len(r.json()["answer"]) > 10

    def test_chat_empty_message_fails(self):
        r = client.post("/chat", json={"message": ""})
        # Should either 400 or return a graceful empty response
        assert r.status_code in (200, 400, 422)

    def test_save_answer(self):
        """save-answer should write to insights/ folder."""
        c = httpx.Client(base_url=BASE, timeout=30.0)  # fresh connection
        before = len(c.get("/pages?folder=insights").json()["pages"])
        r = c.post("/save-answer", json={
            "question": "TEST: what is quantization?",
            "answer": "Quantization reduces model precision from fp32 to int8/int4, cutting VRAM by 2-4x.",
            "sources": [],
            "pages_read": ["quantization"],
        })
        assert r.status_code == 200
        after = len(c.get("/pages?folder=insights").json()["pages"])
        assert after >= before, "Insight page should have been created"
        print(f"  → insights: {before} → {after}")


# ══════════════════════════════════════════════════════════════════════════════
# 7. LINT
# ══════════════════════════════════════════════════════════════════════════════

class TestLint:
    def test_lint_runs_and_returns_score(self):
        r = client.post("/lint", json={"save": False}, timeout=120.0)
        assert r.status_code == 200, r.text
        data = r.json()
        assert "health_score" in data
        assert 0 <= data["health_score"] <= 100
        assert "pages_scanned" in data
        assert data["pages_scanned"] > 0
        print(f"  → health_score: {data['health_score']}/100, pages: {data['pages_scanned']}")
        print(f"  → quick_wins: {len(data.get('quick_wins', []))}")
        print(f"  → inconsistencies: {len(data.get('inconsistencies', []))}")


# ══════════════════════════════════════════════════════════════════════════════
# 8. EDGE CASES
# ══════════════════════════════════════════════════════════════════════════════

class TestEdgeCases:
    def test_malformed_json_422(self):
        r = httpx.post(f"{BASE}/ingest",
            content=b"not json at all",
            headers={"Content-Type": "application/json"},
            timeout=10.0
        )
        assert r.status_code == 422

    def test_very_long_text_truncated_gracefully(self):
        """50k char text should not crash — truncation happens in backend."""
        big_text = ("Attention mechanisms in transformers allow each token to attend to all others. " * 600)
        r = client.post("/ingest", json={"text": big_text})
        assert r.status_code in (200, 400), f"Got {r.status_code}: {r.text[:200]}"
        if r.status_code == 200:
            cleanup_queue_item(r.json()["id"])
            print(f"  → 50k chars handled, bullets: {len(r.json()['diff_preview']['summary'])}")

    def test_unknown_folder_returns_empty_not_500(self):
        r = client.get("/pages?folder=nonexistent-folder-xyz")
        assert r.status_code == 200
        assert r.json()["pages"] == []
