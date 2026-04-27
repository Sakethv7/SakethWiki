"""
Provider-routed LLM client.

Default behavior stays Anthropic-first. Set env vars to route selected tasks
to Qwen (or other OpenAI-compatible providers) without changing app code.
"""
import json
import os
from typing import Any, Optional

import httpx

CRITICAL_TASKS = {
    "INGEST_EXTRACT",
    "LINT_SCAN",
    "LINT_JSON_FIX",
    "CONSOLIDATE_PAGES",
    "KNOWLEDGE_GAPS",
}


def _task_key(task: str) -> str:
    return "".join(c if c.isalnum() else "_" for c in task).upper()


def _has_image_blocks(messages: list[dict[str, Any]]) -> bool:
    for msg in messages:
        content = msg.get("content")
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "image":
                    return True
    return False


def _provider_for_task(task: str) -> str:
    key = _task_key(task)
    return os.environ.get(f"LLM_PROVIDER_{key}", os.environ.get("LLM_PROVIDER", "anthropic")).strip().lower()


def _default_model_for_provider(provider: str, has_images: bool) -> str:
    if provider == "anthropic":
        if has_images:
            return os.environ.get("ANTHROPIC_MODEL_VISION", "claude-sonnet-4-6")
        return os.environ.get("ANTHROPIC_MODEL_TEXT", "claude-haiku-4-5-20251001")
    if provider == "qwen":
        if has_images:
            return os.environ.get("QWEN_MODEL_VISION", "qwen-vl-plus")
        return os.environ.get("QWEN_MODEL_TEXT", "qwen-plus")
    if provider == "ollama":
        if has_images:
            return os.environ.get("OLLAMA_MODEL_VISION", os.environ.get("OLLAMA_MODEL_TEXT", "qwen2.5:7b"))
        return os.environ.get("OLLAMA_MODEL_TEXT", "qwen2.5:7b")
    if provider == "gemma":
        if has_images:
            return os.environ.get("GEMMA_MODEL_VISION", os.environ.get("GEMMA_MODEL_TEXT", "gemma-3-27b-it"))
        return os.environ.get("GEMMA_MODEL_TEXT", "gemma-3-27b-it")
    if has_images:
        return os.environ.get("OPENAI_COMPAT_MODEL_VISION", os.environ.get("OPENAI_COMPAT_MODEL_TEXT", "gpt-4o-mini"))
    return os.environ.get("OPENAI_COMPAT_MODEL_TEXT", "gpt-4o-mini")


def _model_for_task(task: str, provider: str, suggested_model: Optional[str], has_images: bool) -> str:
    key = _task_key(task)
    override = os.environ.get(f"LLM_MODEL_{key}", "").strip()
    if override:
        return override

    is_claude = bool(suggested_model and str(suggested_model).startswith("claude-"))
    if provider == "anthropic" and suggested_model and is_claude:
        return suggested_model
    if provider != "anthropic" and suggested_model and not is_claude:
        return suggested_model

    return _default_model_for_provider(provider, has_images)


def _fallback_enabled(task: str) -> bool:
    key = _task_key(task)
    local = os.environ.get(f"LLM_FALLBACK_{key}", "").strip().lower()
    if local in {"1", "true", "yes", "on"}:
        return True
    if local in {"0", "false", "no", "off"}:
        return False

    global_toggle = os.environ.get("LLM_FALLBACK_TO_ANTHROPIC", "").strip().lower()
    if global_toggle in {"1", "true", "yes", "on"}:
        return True
    if global_toggle in {"0", "false", "no", "off"}:
        return False

    return key in CRITICAL_TASKS


def _valid_contract(text: str, expect_json: bool, required_keys: Optional[list[str]]) -> bool:
    if not text or not text.strip():
        return False
    if not expect_json:
        return True
    try:
        parsed = json.loads(text)
    except Exception:
        return False
    if not required_keys:
        return True
    if not isinstance(parsed, dict):
        return False
    return all(k in parsed for k in required_keys)


def _normalize_openai_content(content: Any) -> Any:
    if isinstance(content, str):
        return content

    if not isinstance(content, list):
        return str(content)

    blocks: list[dict[str, Any]] = []
    text_parts: list[str] = []
    has_non_text = False
    for block in content:
        if not isinstance(block, dict):
            text_parts.append(str(block))
            continue
        block_type = block.get("type")
        if block_type == "text":
            txt = block.get("text", "")
            blocks.append({"type": "text", "text": txt})
            text_parts.append(txt)
        elif block_type == "image":
            src = block.get("source", {})
            media_type = src.get("media_type", "image/png")
            data = src.get("data", "")
            if data:
                has_non_text = True
                blocks.append({"type": "image_url", "image_url": {"url": f"data:{media_type};base64,{data}"}})

    if not has_non_text:
        # Wider compatibility for text-only calls.
        return "\n\n".join(part for part in text_parts if part).strip()
    return blocks


def _to_openai_messages(messages: list[dict[str, Any]], system: Optional[Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    if system:
        if isinstance(system, str):
            sys_text = system
        elif isinstance(system, list):
            sys_text = "\n".join(
                block.get("text", "")
                for block in system
                if isinstance(block, dict) and block.get("type") == "text"
            ).strip()
        else:
            sys_text = str(system)
        if sys_text:
            out.append({"role": "system", "content": sys_text})

    for msg in messages:
        role = msg.get("role", "user")
        out.append({"role": role, "content": _normalize_openai_content(msg.get("content", ""))})
    return out


def _anthropic_complete(
    *,
    model: str,
    max_tokens: int,
    messages: list[dict[str, Any]],
    system: Optional[Any],
    api_key: Optional[str],
) -> str:
    import anthropic

    client = anthropic.Anthropic(api_key=api_key or os.environ.get("ANTHROPIC_API_KEY"))
    kwargs: dict[str, Any] = {
        "model": model,
        "max_tokens": max_tokens,
        "messages": messages,
    }
    if system:
        kwargs["system"] = system
    resp = client.messages.create(**kwargs)
    text_parts: list[str] = []
    for block in getattr(resp, "content", []):
        if getattr(block, "type", "") == "text":
            text_parts.append(getattr(block, "text", ""))
    return "".join(text_parts).strip()


def _openai_compat_complete(
    *,
    provider: str,
    model: str,
    max_tokens: int,
    messages: list[dict[str, Any]],
    system: Optional[Any],
    api_key: Optional[str],
) -> str:
    if provider == "qwen":
        base_url = os.environ.get("QWEN_BASE_URL", "https://dashscope-intl.aliyuncs.com/compatible-mode/v1")
        key = api_key or os.environ.get("QWEN_API_KEY")
    elif provider == "ollama":
        base_url = os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434/v1")
        key = api_key or os.environ.get("OLLAMA_API_KEY", "")
    elif provider == "gemma":
        base_url = os.environ.get("GEMMA_OPENAI_BASE_URL", "")
        key = api_key or os.environ.get("GEMMA_API_KEY") or os.environ.get("GEMINI_API_KEY")
    else:
        base_url = os.environ.get("OPENAI_COMPAT_BASE_URL", "")
        key = api_key or os.environ.get("OPENAI_COMPAT_API_KEY")

    if not base_url:
        raise RuntimeError(f"{provider} provider is enabled but base URL is not configured")
    if provider != "ollama" and not key:
        raise RuntimeError(f"{provider} provider is enabled but API key is not configured")

    payload: dict[str, Any] = {
        "model": model,
        "max_tokens": max_tokens,
        "messages": _to_openai_messages(messages, system),
    }
    headers = {"Content-Type": "application/json"}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    url = f"{base_url.rstrip('/')}/chat/completions"
    with httpx.Client(timeout=120.0) as client:
        resp = client.post(url, headers=headers, json=payload)
        resp.raise_for_status()
        data = resp.json()

    msg = (((data.get("choices") or [{}])[0]).get("message") or {})
    content = msg.get("content", "")
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(block.get("text", ""))
        return "".join(parts).strip()
    return str(content).strip()


def complete(
    *,
    task: str,
    model: Optional[str],
    max_tokens: int,
    messages: list[dict[str, Any]],
    system: Optional[Any] = None,
    api_key: Optional[str] = None,
    expect_json: bool = False,
    required_json_keys: Optional[list[str]] = None,
) -> str:
    """
    Execute one completion call for the given task.
    Task controls provider/model routing via env vars.
    """
    has_images = _has_image_blocks(messages)
    key = _task_key(task)
    provider = _provider_for_task(task)
    resolved_model = _model_for_task(task, provider, model, has_images)
    primary_err: Optional[Exception] = None
    primary_text = ""

    try:
        if provider == "anthropic":
            primary_text = _anthropic_complete(
                model=resolved_model,
                max_tokens=max_tokens,
                messages=messages,
                system=system,
                api_key=api_key,
            )
        else:
            primary_text = _openai_compat_complete(
                provider=provider,
                model=resolved_model,
                max_tokens=max_tokens,
                messages=messages,
                system=system,
                api_key=api_key,
            )
    except Exception as e:
        primary_err = e

    if primary_text and _valid_contract(primary_text, expect_json, required_json_keys):
        return primary_text

    if provider == "anthropic" or not _fallback_enabled(task):
        if primary_err:
            raise primary_err
        raise RuntimeError(f"LLM contract failed for task={key} provider={provider} model={resolved_model}")

    fallback_model = os.environ.get(f"LLM_FALLBACK_MODEL_{key}", "").strip() or _model_for_task(
        task, "anthropic", model, has_images
    )
    fallback_text = _anthropic_complete(
        model=fallback_model,
        max_tokens=max_tokens,
        messages=messages,
        system=system,
        api_key=api_key,
    )
    if _valid_contract(fallback_text, expect_json, required_json_keys):
        return fallback_text
    raise RuntimeError(
        f"LLM fallback contract failed for task={key} provider={provider}→anthropic model={resolved_model}→{fallback_model}"
    )
