import httpx
import pytest

import llm_client


class ProviderStatusError(Exception):
    def __init__(self, status_code, retry_after=None):
        super().__init__(f"provider status {status_code}")
        self.status_code = status_code
        headers = {} if retry_after is None else {"retry-after": retry_after}
        self.response = type("Response", (), {"headers": headers, "status_code": status_code})()


def _configure_fast_retry(monkeypatch, attempts="3"):
    monkeypatch.setenv("LLM_MAX_ATTEMPTS", attempts)
    monkeypatch.setenv("LLM_RETRY_BASE_SECONDS", "0")
    monkeypatch.setenv("LLM_RETRY_MAX_SECONDS", "0")


def test_retries_529_then_returns_success(monkeypatch):
    _configure_fast_retry(monkeypatch)
    calls = []

    def call():
        calls.append(1)
        if len(calls) < 3:
            raise ProviderStatusError(529)
        return llm_client.CompletionResult("ok")

    result, attempts = llm_client._call_with_retry(
        call, provider="anthropic", model="test-model"
    )

    assert result.text == "ok"
    assert attempts == 3
    assert len(calls) == 3


def test_exhausts_retry_budget_and_preserves_last_error(monkeypatch):
    _configure_fast_retry(monkeypatch)
    calls = []

    def call():
        calls.append(1)
        raise ProviderStatusError(529)

    with pytest.raises(ProviderStatusError) as raised:
        llm_client._call_with_retry(call, provider="anthropic", model="test-model")

    assert len(calls) == 3
    assert raised.value.llm_attempts == 3


def test_does_not_retry_authentication_failure(monkeypatch):
    _configure_fast_retry(monkeypatch)
    calls = []

    def call():
        calls.append(1)
        raise ProviderStatusError(401)

    with pytest.raises(ProviderStatusError) as raised:
        llm_client._call_with_retry(call, provider="anthropic", model="test-model")

    assert len(calls) == 1
    assert raised.value.llm_attempts == 1


@pytest.mark.parametrize("error", [httpx.ConnectError("offline"), httpx.ReadTimeout("slow")])
def test_retries_network_failures(monkeypatch, error):
    _configure_fast_retry(monkeypatch)
    calls = []

    def call():
        calls.append(1)
        if len(calls) == 1:
            raise error
        return llm_client.CompletionResult("recovered")

    result, attempts = llm_client._call_with_retry(
        call, provider="openai_compat", model="test-model"
    )

    assert result.text == "recovered"
    assert attempts == 2


def test_retry_after_is_capped(monkeypatch):
    monkeypatch.setenv("LLM_MAX_ATTEMPTS", "2")
    monkeypatch.setenv("LLM_RETRY_BASE_SECONDS", "1")
    monkeypatch.setenv("LLM_RETRY_MAX_SECONDS", "3")
    sleeps = []
    monkeypatch.setattr(llm_client.time, "sleep", sleeps.append)
    monkeypatch.setattr(llm_client.random, "uniform", lambda _low, _high: 1.0)
    calls = []

    def call():
        calls.append(1)
        if len(calls) == 1:
            raise ProviderStatusError(529, retry_after="60")
        return llm_client.CompletionResult("ok")

    llm_client._call_with_retry(call, provider="anthropic", model="test-model")

    assert sleeps == [3.0]


def test_complete_logs_primary_attempt_count(monkeypatch):
    _configure_fast_retry(monkeypatch)
    monkeypatch.setenv("LLM_PROVIDER", "anthropic")
    calls = []
    events = []

    def provider_call(**_kwargs):
        calls.append(1)
        if len(calls) == 1:
            raise ProviderStatusError(529)
        return llm_client.CompletionResult("ok", {"input_tokens": 1, "output_tokens": 1})

    monkeypatch.setattr(llm_client, "_anthropic_complete", provider_call)
    monkeypatch.setattr(llm_client.telemetry, "log_llm_call", events.append)

    result = llm_client.complete(
        task="chat_answer",
        model=None,
        max_tokens=10,
        messages=[{"role": "user", "content": "ping"}],
    )

    assert result == "ok"
    assert events[-1]["primary_attempts"] == 2
    assert events[-1]["fallback_attempts"] == 0


def test_ingest_contract_failure_gets_one_compact_repair(monkeypatch):
    _configure_fast_retry(monkeypatch, attempts="1")
    monkeypatch.setenv("LLM_PROVIDER", "anthropic")
    calls = []
    events = []
    required = ["title", "summary"]

    def provider_call(**kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            return llm_client.CompletionResult('{"title":"truncated"')
        return llm_client.CompletionResult('{"title":"fixed","summary":["one"]}')

    monkeypatch.setattr(llm_client, "_anthropic_complete", provider_call)
    monkeypatch.setattr(llm_client.telemetry, "log_llm_call", events.append)

    result = llm_client.complete(
        task="ingest_extract",
        model=None,
        max_tokens=1200,
        messages=[{"role": "user", "content": "source text"}],
        expect_json=True,
        required_json_keys=required,
    )

    assert result == '{"title":"fixed","summary":["one"]}'
    assert len(calls) == 2
    assert calls[1]["max_tokens"] == 1600
    assert "previous response failed" in calls[1]["messages"][-1]["content"]
    assert events[-1]["contract_repair_attempts"] == 1


def test_non_ingest_contract_failure_does_not_add_repair_attempt(monkeypatch):
    _configure_fast_retry(monkeypatch, attempts="1")
    monkeypatch.setenv("LLM_PROVIDER", "anthropic")
    calls = []

    def provider_call(**_kwargs):
        calls.append(1)
        return llm_client.CompletionResult("not json")

    monkeypatch.setattr(llm_client, "_anthropic_complete", provider_call)

    with pytest.raises(RuntimeError, match="LLM contract failed"):
        llm_client.complete(
            task="knowledge_gaps",
            model=None,
            max_tokens=100,
            messages=[{"role": "user", "content": "source text"}],
            expect_json=True,
            required_json_keys=["title"],
        )

    assert len(calls) == 1


def test_ingest_contract_repair_can_be_disabled(monkeypatch):
    _configure_fast_retry(monkeypatch, attempts="1")
    monkeypatch.setenv("LLM_PROVIDER", "anthropic")
    monkeypatch.setenv("LLM_INGEST_CONTRACT_REPAIR", "false")
    calls = []

    def provider_call(**_kwargs):
        calls.append(1)
        return llm_client.CompletionResult("not json")

    monkeypatch.setattr(llm_client, "_anthropic_complete", provider_call)

    with pytest.raises(RuntimeError, match="LLM contract failed"):
        llm_client.complete(
            task="ingest_extract",
            model=None,
            max_tokens=100,
            messages=[{"role": "user", "content": "source text"}],
            expect_json=True,
            required_json_keys=["title"],
        )

    assert len(calls) == 1


def test_slice_contract_failure_gets_one_compact_repair(monkeypatch):
    _configure_fast_retry(monkeypatch, attempts="1")
    monkeypatch.setenv("LLM_PROVIDER", "anthropic")
    calls = []

    def provider_call(**kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            return llm_client.CompletionResult("[truncated")
        return llm_client.CompletionResult('[{"title":"one","paragraphs":[0],"concept_hint":""}]')

    monkeypatch.setattr(llm_client, "_anthropic_complete", provider_call)

    result = llm_client.complete(
        task="slice_content",
        model=None,
        max_tokens=700,
        messages=[{"role": "user", "content": "paragraphs"}],
        expect_json=True,
    )

    assert result.startswith("[")
    assert len(calls) == 2
    assert calls[1]["max_tokens"] == 1000
