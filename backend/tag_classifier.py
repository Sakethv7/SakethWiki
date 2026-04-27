"""
Tag → semantic group → color.
Groups are defined here; Haiku classifies any tag not in the seed list.
Frontend maps group name → Tailwind color classes (classes stay in JSX source).
"""
import json
import re
import os
from pathlib import Path
from typing import Optional

import llm_client

TAG_GROUPS_FILE = Path(__file__).parent / "tag_groups.json"

# Semantic groups. Frontend maps these names to Tailwind color strings.
GROUPS = [
    "models",       # LLMs, neural nets, language model architectures
    "training",     # fine-tuning, optimization, gradients, loss functions
    "attention",    # attention mechanisms, transformers, positional encoding
    "inference",    # inference serving, deployment, throughput, latency
    "memory",       # KV cache, context windows, quantization/compression
    "data",         # embeddings, vectors, VectorDB, RAG, NLP, semantic search
    "agents",       # agents, agentic AI, tool use, MCP, LangGraph, multi-agent
    "ops",          # MLOps, evals, monitoring, cost optimization
    "meta",         # learning roadmaps, indexes, meta/admin tags
]

# Seed mapping — covers all tags that ship with the vault
_SEED: dict[str, str] = {
    # models
    "LLM": "models", "llm": "models",
    "neural-networks": "models", "machine-learning": "models",
    "deep-learning": "models", "softmax": "models",
    # training
    "FineTuning": "training", "finetuning": "training",
    "supervised-learning": "training", "gradient-descent": "training",
    "linear-regression": "training", "logistic-regression": "training",
    "optimization": "training", "loss-functions": "training",
    "transfer-learning": "training",
    # attention
    "Attention": "attention",
    "attention-mechanisms": "attention", "self-attention": "attention",
    "multi-head-attention": "attention", "positional-encoding": "attention",
    "transformers": "attention",
    # inference
    "Inference": "inference", "Serving": "inference",
    "inference-optimization": "inference", "serving": "inference",
    # memory
    "KVCache": "memory", "kv-caching": "memory", "kv-cache": "memory",
    "Quantization": "memory", "quantization": "memory",
    # data
    "Embeddings": "data", "embeddings": "data",
    "VectorDB": "data", "word2vec": "data", "nlp": "data",
    "cosine-similarity": "data", "semantic-representation": "data",
    "semantic-search": "data", "RAG": "data", "rag": "data",
    "retrieval": "data",
    # agents
    "Agents": "agents", "Agentic": "agents",
    "agentic-ai": "agents", "llm-agents": "agents",
    "reflection-pattern": "agents", "tool-use": "agents",
    "multi-agent-systems": "agents", "langgraph": "agents",
    "autonomy": "agents", "mcp": "agents",
    # ops
    "MLOps": "ops", "mlops": "ops", "evals": "ops",
    "monitoring": "ops", "cost": "ops", "routing": "ops",
    # meta
    "learning-roadmap": "meta", "index": "meta", "meta": "meta",
}


def load() -> dict[str, str]:
    """Load tag→group mapping from disk, merged with seed."""
    stored: dict[str, str] = {}
    if TAG_GROUPS_FILE.exists():
        try:
            stored = json.loads(TAG_GROUPS_FILE.read_text())
        except Exception:
            pass
    return {**_SEED, **stored}


def _save(mapping: dict[str, str]) -> None:
    # Only persist tags that aren't in the seed (seed is always implicit)
    extra = {k: v for k, v in mapping.items() if k not in _SEED}
    TAG_GROUPS_FILE.write_text(json.dumps(extra, indent=2, sort_keys=True))


def classify_new_tags(tags: list[str], api_key: Optional[str] = None) -> dict[str, str]:
    """
    Given a list of tags, return the full mapping (including any new ones).
    New tags are classified by Haiku and cached to disk.
    """
    current = load()
    unknown = [t for t in tags if t not in current]
    if not unknown:
        return current

    provider = os.environ.get("LLM_PROVIDER_TAG_CLASSIFY", os.environ.get("LLM_PROVIDER", "anthropic")).lower()
    if provider == "anthropic":
        key = api_key or os.environ.get("ANTHROPIC_API_KEY", "")
    elif provider == "qwen":
        key = api_key or os.environ.get("QWEN_API_KEY", "")
    elif provider == "gemma":
        key = api_key or os.environ.get("GEMMA_API_KEY", "") or os.environ.get("GEMINI_API_KEY", "")
    else:
        key = api_key or os.environ.get("OPENAI_COMPAT_API_KEY", "")

    if not key:
        # No API key — assign "meta" as safe fallback
        for t in unknown:
            current[t] = _fallback_group(t)
        _save(current)
        return current

    try:
        groups_desc = (
            "models (LLMs/neural nets), training (fine-tuning/optimization), "
            "attention (transformers/self-attention), inference (serving/deployment), "
            "memory (KV cache/quantization/compression), data (embeddings/RAG/VectorDB), "
            "agents (agentic AI/tool-use/MCP), ops (MLOps/evals/monitoring), "
            "meta (roadmaps/indexes/admin)"
        )
        prompt = (
            f"Classify each ML/AI tag into one group.\n"
            f"Groups: {groups_desc}\n"
            f"Tags: {', '.join(unknown)}\n"
            f'Reply with JSON only, no prose: {{"tag": "group", ...}}'
        )
        raw = llm_client.complete(
            task="tag_classify",
            model="claude-haiku-4-5-20251001",
            max_tokens=300,
            messages=[{"role": "user", "content": prompt}],
            api_key=key,
            expect_json=True,
        ).strip()
        # Extract JSON even if wrapped in ```
        m = re.search(r"\{.*\}", raw, re.DOTALL)
        if m:
            classified = json.loads(m.group())
            for tag, group in classified.items():
                if group in GROUPS:
                    current[tag] = group
    except Exception:
        pass

    # Fallback for any still-unknown
    for t in unknown:
        if t not in current:
            current[t] = _fallback_group(t)

    _save(current)
    return current


def _fallback_group(tag: str) -> str:
    """Deterministic fallback: hash tag to a non-meta group."""
    non_meta = [g for g in GROUPS if g != "meta"]
    return non_meta[hash(tag) % len(non_meta)]
