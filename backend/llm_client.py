"""
Provider-routed LLM client.

Default behavior stays Anthropic-first. Set env vars to route selected tasks
to Qwen (or other OpenAI-compatible providers) without changing app code.
"""
import json
import logging
import os
import random
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Optional

import httpx

import telemetry

logger = logging.getLogger(__name__)

CRITICAL_TASKS = {
    "INGEST_EXTRACT",
    "ANALYZE_TRACES",
    "LINT_SCAN",
    "LINT_JSON_FIX",
    "CONSOLIDATE_PAGES",
    "KNOWLEDGE_GAPS",
    "EVOLUTION_CLASSIFY",
}

RETRYABLE_HTTP_STATUSES = {408, 409, 429, 500, 502, 503, 504, 529}


@dataclass
class CompletionResult:
    text: str
    usage: dict[str, Any] = field(default_factory=dict)


def _env_number(name: str, default: float, minimum: float, maximum: float) -> float:
    try:
        value = float(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        value = default
    return min(maximum, max(minimum, value))


def _retry_config() -> tuple[int, float, float]:
    attempts = int(_env_number("LLM_MAX_ATTEMPTS", 3, 1, 5))
    base_seconds = _env_number("LLM_RETRY_BASE_SECONDS", 1, 0, 10)
    max_seconds = _env_number("LLM_RETRY_MAX_SECONDS", 8, 0, 30)
    return attempts, base_seconds, max_seconds


def _exception_status(exc: Exception) -> Optional[int]:
    status = getattr(exc, "status_code", None)
    if status is None:
        status = getattr(getattr(exc, "response", None), "status_code", None)
    try:
        return int(status) if status is not None else None
    except (TypeError, ValueError):
        return None


def _is_retryable_exception(exc: Exception) -> bool:
    status = _exception_status(exc)
    if status is not None:
        return status in RETRYABLE_HTTP_STATUSES
    if isinstance(exc, (httpx.TimeoutException, httpx.NetworkError)):
        return True
    # Anthropic wraps httpx failures without always preserving an HTTP status.
    return type(exc).__name__ in {"APIConnectionError", "APITimeoutError"}


def _retry_after_seconds(exc: Exception) -> Optional[float]:
    response = getattr(exc, "response", None)
    headers = getattr(response, "headers", None)
    if not headers:
        return None
    raw = headers.get("retry-after")
    if not raw:
        return None
    try:
        return max(0.0, float(raw))
    except (TypeError, ValueError):
        try:
            retry_at = parsedate_to_datetime(str(raw))
            if retry_at.tzinfo is None:
                retry_at = retry_at.replace(tzinfo=timezone.utc)
            return max(0.0, (retry_at - datetime.now(timezone.utc)).total_seconds())
        except (TypeError, ValueError, OverflowError):
            return None


def _call_with_retry(call, *, provider: str, model: str) -> tuple[CompletionResult, int]:
    max_attempts, base_seconds, max_seconds = _retry_config()
    for attempt in range(1, max_attempts + 1):
        try:
            return call(), attempt
        except Exception as exc:
            if attempt >= max_attempts or not _is_retryable_exception(exc):
                setattr(exc, "llm_attempts", attempt)
                raise
            exponential = base_seconds * (2 ** (attempt - 1))
            jittered = exponential * random.uniform(0.75, 1.25)
            retry_after = _retry_after_seconds(exc) or 0.0
            delay = min(max_seconds, max(jittered, retry_after))
            logger.warning(
                "LLM transient failure provider=%s model=%s attempt=%s/%s; "
                "retrying in %.2fs: %s",
                provider,
                model,
                attempt,
                max_attempts,
                delay,
                type(exc).__name__,
            )
            time.sleep(delay)

    raise RuntimeError("unreachable retry state")


# ── Langfuse observability (optional) ─────────────────────────────────────────
# One generation observation per completed task. Entirely no-op unless
# LANGFUSE_PUBLIC_KEY / LANGFUSE_SECRET_KEY are set and the SDK is installed.
# A Langfuse failure must never surface to the caller — everything is swallowed.

_LF_CLIENT = None
_LF_RESOLVED = False


def _langfuse_client():
    global _LF_CLIENT, _LF_RESOLVED
    if _LF_RESOLVED:
        return _LF_CLIENT
    _LF_RESOLVED = True
    if not (os.environ.get("LANGFUSE_PUBLIC_KEY") and os.environ.get("LANGFUSE_SECRET_KEY")):
        return None
    try:
        from langfuse import Langfuse  # reads LANGFUSE_* from env
        _LF_CLIENT = Langfuse()
    except Exception as e:  # ImportError, bad config, version drift
        logger.warning("Langfuse disabled (init failed): %s", e)
        _LF_CLIENT = None
    return _LF_CLIENT


def _lf_emit(*, task, model, input_payload, output_text, usage, metadata, error):
    client = _langfuse_client()
    if client is None:
        return
    kwargs = dict(
        name=str(task),
        model=str(model),
        input=input_payload,
        output=(output_text or None),
        metadata=metadata,
    )
    if error:
        kwargs["level"] = "ERROR"
        kwargs["status_message"] = str(error)[:1000]
    if usage:
        usage_details = {
            "input": int(usage.get("input_tokens", 0) or 0),
            "output": int(usage.get("output_tokens", 0) or 0),
        }
    else:
        usage_details = None
    try:
        if hasattr(client, "start_generation"):          # langfuse v3 low-level SDK
            client.start_generation(usage_details=usage_details, **kwargs).end()
        elif hasattr(client, "generation"):              # langfuse v2
            if usage_details:
                kwargs["usage"] = usage_details
            client.generation(**kwargs)
    except Exception as e:
        logger.warning("Langfuse emit failed for task=%s: %s", task, e)


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
    explicit = os.environ.get(f"LLM_PROVIDER_{key}", "").strip().lower()
    if explicit:
        return explicit
    try:
        import system_loop
        overrides = system_loop.load_runtime_overrides()
        route = (overrides.get("routes") or {}).get(key, {})
        if overrides.get("enabled", True) and isinstance(route, dict):
            provider = str(route.get("provider", "")).strip().lower()
            if provider:
                return provider
    except Exception:
        pass
    return os.environ.get("LLM_PROVIDER", "anthropic").strip().lower()


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


def _strip_fences(text: str) -> str:
    t = text.strip()
    if t.startswith("```"):
        lines = t.split("\n", 1)
        rest = lines[1] if len(lines) > 1 else ""
        # Cut at the closing fence rather than requiring it to be the very
        # last thing in the string — models sometimes add prose (e.g. a
        # "Reasoning:" section) after the fenced block, which would otherwise
        # leave that trailing text in place and break JSON parsing.
        close_idx = rest.find("```")
        t = rest[:close_idx] if close_idx != -1 else rest
    return t.strip()


def _valid_contract(text: str, expect_json: bool, required_keys: Optional[list[str]]) -> bool:
    if not text or not text.strip():
        return False
    if not expect_json:
        return True
    try:
        parsed = json.loads(_strip_fences(text))
    except Exception:
        return False
    if not required_keys:
        return True
    if not isinstance(parsed, dict):
        return False
    return all(k in parsed for k in required_keys)


def _estimate_tokens_from_chars(chars: int) -> int:
    return max(1, int(round(max(chars, 0) / 4)))


def _price_env_key(provider: str, model: str, direction: str) -> str:
    safe_model = "".join(c if c.isalnum() else "_" for c in model).upper()
    safe_provider = "".join(c if c.isalnum() else "_" for c in provider).upper()
    return f"LLM_PRICE_{safe_provider}_{safe_model}_{direction.upper()}_PER_1M"


def _default_price_per_1m(provider: str, model: str, direction: str) -> float:
    m = model.lower()
    p = provider.lower()
    if p == "ollama":
        return 0.0
    if "fable-5" in m or "fable 5" in m:
        return 10.0 if direction == "input" else 50.0
    if "gpt-5.5" in m or "gpt-5_5" in m:
        return 5.0 if direction == "input" else 30.0
    if "haiku" in m:
        return 1.0 if direction == "input" else 5.0
    if "sonnet-5" in m or "sonnet 5" in m:
        return 2.0 if direction == "input" else 10.0
    if "sonnet" in m:
        return 3.0 if direction == "input" else 15.0
    if "gpt-4o-mini" in m:
        return 0.15 if direction == "input" else 0.60
    if "gemini-2.5-flash" in m or "gemini-1.5-flash" in m:
        return 0.30 if direction == "input" else 2.50
    if "qwen-vl-plus" in m or "qwen-plus" in m:
        return 0.40 if direction == "input" else 1.20
    return 0.0


def _price_per_1m(provider: str, model: str, direction: str) -> float:
    specific = os.environ.get(_price_env_key(provider, model, direction), "").strip()
    generic = os.environ.get(f"LLM_PRICE_{direction.upper()}_PER_1M", "").strip()
    for raw in (specific, generic):
        if not raw:
            continue
        try:
            return max(0.0, float(raw))
        except ValueError:
            continue
    return _default_price_per_1m(provider, model, direction)


def _usage_with_estimates(provider: str, model: str, usage: dict[str, Any], input_chars: int, output_chars: int) -> dict[str, Any]:
    input_tokens = int(usage.get("input_tokens", 0) or 0)
    output_tokens = int(usage.get("output_tokens", 0) or 0)
    cache_creation_tokens = int(usage.get("cache_creation_input_tokens", 0) or 0)
    cache_read_tokens = int(usage.get("cache_read_input_tokens", 0) or 0)
    estimated = False
    if input_tokens <= 0:
        input_tokens = _estimate_tokens_from_chars(input_chars)
        estimated = True
    if output_tokens <= 0:
        output_tokens = _estimate_tokens_from_chars(output_chars)
        estimated = True

    input_price = _price_per_1m(provider, model, "input")
    output_price = _price_per_1m(provider, model, "output")
    # Treat cache read as 10% input cost when providers expose it. This matches
    # Anthropic prompt-cache economics closely enough for local observability.
    billable_input_tokens = input_tokens + cache_creation_tokens + (cache_read_tokens * 0.10)
    input_usd = (billable_input_tokens / 1_000_000) * input_price
    output_usd = (output_tokens / 1_000_000) * output_price
    total_usd = input_usd + output_usd
    total_tokens = input_tokens + output_tokens + cache_creation_tokens + cache_read_tokens
    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cache_creation_input_tokens": cache_creation_tokens,
        "cache_read_input_tokens": cache_read_tokens,
        "total_tokens": total_tokens,
        "billable_input_tokens": round(billable_input_tokens, 1),
        "input_usd": round(input_usd, 8),
        "output_usd": round(output_usd, 8),
        "cost_usd": round(total_usd, 8),
        "input_price_per_1m": input_price,
        "output_price_per_1m": output_price,
        "cost_per_token_usd": round(total_usd / total_tokens, 10) if total_tokens else 0,
        "usage_source": usage.get("source", "provider") if not estimated else "estimated",
        "cost_estimated": estimated or ((input_price == 0.0 and output_price == 0.0) and provider.lower() != "ollama"),
    }


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
) -> CompletionResult:
    import anthropic

    # Retry centrally in _call_with_retry so the configured attempt budget and
    # telemetry reflect actual provider requests instead of nested SDK retries.
    client = anthropic.Anthropic(
        api_key=api_key or os.environ.get("ANTHROPIC_API_KEY"),
        max_retries=0,
    )

    # Add prompt caching to system prompt — stable instructions cached at 10% input cost after first hit
    system_param = system
    if system:
        if isinstance(system, str) and len(system) > 200:
            system_param = [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}]
        elif isinstance(system, list):
            # Mark the last text block as cacheable
            system_param = []
            for i, block in enumerate(system):
                if isinstance(block, dict) and block.get("type") == "text" and i == len(system) - 1:
                    system_param.append({**block, "cache_control": {"type": "ephemeral"}})
                else:
                    system_param.append(block)

    kwargs: dict[str, Any] = {
        "model": model,
        "max_tokens": max_tokens,
        "messages": messages,
    }
    if system_param:
        kwargs["system"] = system_param
    resp = client.messages.create(**kwargs)
    text_parts: list[str] = []
    for block in getattr(resp, "content", []):
        if getattr(block, "type", "") == "text":
            text_parts.append(getattr(block, "text", ""))
    usage_obj = getattr(resp, "usage", None)
    usage = {
        "input_tokens": int(getattr(usage_obj, "input_tokens", 0) or 0),
        "output_tokens": int(getattr(usage_obj, "output_tokens", 0) or 0),
        "cache_creation_input_tokens": int(getattr(usage_obj, "cache_creation_input_tokens", 0) or 0),
        "cache_read_input_tokens": int(getattr(usage_obj, "cache_read_input_tokens", 0) or 0),
        "source": "provider",
    }
    return CompletionResult("".join(text_parts).strip(), usage)


def _openai_compat_complete(
    *,
    provider: str,
    model: str,
    max_tokens: int,
    messages: list[dict[str, Any]],
    system: Optional[Any],
    api_key: Optional[str],
) -> CompletionResult:
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
    usage_obj = data.get("usage") or {}
    usage = {
        "input_tokens": int(usage_obj.get("prompt_tokens", 0) or usage_obj.get("input_tokens", 0) or 0),
        "output_tokens": int(usage_obj.get("completion_tokens", 0) or usage_obj.get("output_tokens", 0) or 0),
        "total_tokens": int(usage_obj.get("total_tokens", 0) or 0),
        "source": "provider" if usage_obj else "missing",
    }
    if isinstance(content, str):
        return CompletionResult(content.strip(), usage)
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(block.get("text", ""))
        return CompletionResult("".join(parts).strip(), usage)
    return CompletionResult(str(content).strip(), usage)


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
    started = time.perf_counter()
    has_images = _has_image_blocks(messages)
    key = _task_key(task)
    provider = _provider_for_task(task)
    requested_provider = provider

    # Ollama can't handle images — route vision tasks directly to Anthropic
    # Cloud providers (openai_compat covers Gemini/OpenAI/DeepSeek) handle vision themselves
    if has_images and provider == "ollama":
        provider = "anthropic"

    # A caller naming an explicit Claude model (e.g. "structured extraction,
    # no deep reasoning" callsites in main.py) wants that model specifically,
    # not the default provider's model. Without this, _model_for_task below
    # silently discards the request whenever the configured default provider
    # isn't anthropic and substitutes that provider's own default model.
    if model and str(model).startswith("claude-") and provider != "anthropic":
        provider = "anthropic"

    resolved_model = _model_for_task(task, provider, model, has_images)
    primary_err: Optional[Exception] = None
    primary_text = ""
    primary_usage: dict[str, Any] = {}
    primary_attempts = 0
    fallback_attempts = 0
    input_chars = telemetry.estimate_chars({"system": system, "messages": messages})

    def _log(*, text: str = "", usage: Optional[dict[str, Any]] = None, fallback_used: bool = False, fallback_model: str = "", contract_ok: bool = False, error: str = "") -> None:
        effective_provider = "anthropic" if fallback_model else provider
        effective_model = fallback_model or resolved_model
        cost = _usage_with_estimates(effective_provider, effective_model, usage or {}, input_chars, len(text or ""))
        telemetry.log_llm_call(
            {
                "task": key,
                "provider": provider,
                "requested_provider": requested_provider,
                "model": resolved_model,
                "fallback_model": fallback_model,
                "effective_provider": effective_provider,
                "effective_model": effective_model,
                "duration_ms": round((time.perf_counter() - started) * 1000, 1),
                "input_chars": input_chars,
                "output_chars": len(text or ""),
                **cost,
                "max_tokens": max_tokens,
                "expect_json": bool(expect_json),
                "required_json_keys": required_json_keys or [],
                "has_images": has_images,
                "contract_ok": contract_ok,
                "fallback_used": fallback_used,
                "primary_attempts": primary_attempts,
                "fallback_attempts": fallback_attempts,
                "error": error,
            }
        )
        _lf_emit(
            task=key,
            model=effective_model,
            input_payload=(
                f"<{len(messages)} message(s), image blocks omitted>"
                if has_images else {"system": system, "messages": messages}
            ),
            output_text=text,
            usage=usage,
            metadata={
                "provider": effective_provider,
                "requested_provider": requested_provider,
                "fallback_used": fallback_used,
                "fallback_model": fallback_model,
                "contract_ok": contract_ok,
                "expect_json": bool(expect_json),
                "has_images": has_images,
                "max_tokens": max_tokens,
                "duration_ms": round((time.perf_counter() - started) * 1000, 1),
                "primary_attempts": primary_attempts,
                "fallback_attempts": fallback_attempts,
                **{k: v for k, v in cost.items() if "cost" in k or "token" in k},
            },
            error=error or None,
        )

    try:
        if provider == "anthropic":
            primary_result, primary_attempts = _call_with_retry(
                lambda: _anthropic_complete(
                    model=resolved_model,
                    max_tokens=max_tokens,
                    messages=messages,
                    system=system,
                    api_key=api_key,
                ),
                provider=provider,
                model=resolved_model,
            )
        else:
            primary_result, primary_attempts = _call_with_retry(
                lambda: _openai_compat_complete(
                    provider=provider,
                    model=resolved_model,
                    max_tokens=max_tokens,
                    messages=messages,
                    system=system,
                    api_key=api_key,
                ),
                provider=provider,
                model=resolved_model,
            )
        primary_text = primary_result.text
        primary_usage = primary_result.usage
    except Exception as e:
        primary_err = e
        primary_attempts = int(getattr(e, "llm_attempts", primary_attempts or 1))

    if primary_text and _valid_contract(primary_text, expect_json, required_json_keys):
        _log(text=primary_text, usage=primary_usage, contract_ok=True)
        return primary_text

    if provider == "anthropic" or not _fallback_enabled(task):
        if primary_err:
            _log(text=primary_text, usage=primary_usage, contract_ok=False, error=f"{type(primary_err).__name__}: {primary_err}")
            raise primary_err
        _log(text=primary_text, usage=primary_usage, contract_ok=False, error="LLM contract failed")
        raise RuntimeError(f"LLM contract failed for task={key} provider={provider} model={resolved_model}")

    fallback_model = os.environ.get(f"LLM_FALLBACK_MODEL_{key}", "").strip() or _model_for_task(
        task, "anthropic", model, has_images
    )
    try:
        fallback_result, fallback_attempts = _call_with_retry(
            lambda: _anthropic_complete(
                model=fallback_model,
                max_tokens=max_tokens,
                messages=messages,
                system=system,
                api_key=api_key,
            ),
            provider="anthropic",
            model=fallback_model,
        )
        fallback_text = fallback_result.text
        fallback_usage = fallback_result.usage
    except Exception as fallback_err:
        fallback_attempts = int(getattr(fallback_err, "llm_attempts", fallback_attempts or 1))
        err = f"{type(fallback_err).__name__}: {fallback_err}"
        _log(text=primary_text, usage=primary_usage, fallback_used=True, fallback_model=fallback_model, contract_ok=False, error=err)
        raise
    if _valid_contract(fallback_text, expect_json, required_json_keys):
        _log(text=fallback_text, usage=fallback_usage, fallback_used=True, fallback_model=fallback_model, contract_ok=True)
        return fallback_text
    _log(text=fallback_text, usage=fallback_usage, fallback_used=True, fallback_model=fallback_model, contract_ok=False, error="LLM fallback contract failed")
    raise RuntimeError(
        f"LLM fallback contract failed for task={key} provider={provider}→anthropic model={resolved_model}→{fallback_model}"
    )


def get_openai_api_key() -> str:
    """Return OpenAI key for embeddings/reranking flows."""
    return (
        os.environ.get("OPENAI_API_KEY", "").strip()
        or os.environ.get("OPENAI_COMPAT_API_KEY", "").strip()
    )


def get_embedding_config() -> dict[str, Any]:
    """Read embedding settings from environment."""
    provider = os.environ.get("EMBED_PROVIDER", "openai").strip().lower()
    model = os.environ.get("EMBED_MODEL", "text-embedding-3-small").strip()
    batch_size_raw = os.environ.get("EMBED_BATCH_SIZE", "64").strip()
    try:
        batch_size = max(1, int(batch_size_raw))
    except ValueError:
        batch_size = 64
    return {
        "provider": provider,
        "model": model,
        "batch_size": batch_size,
        "api_key": get_openai_api_key(),
    }
