import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import queue_manager


@pytest.fixture(autouse=True)
def isolated_vault(monkeypatch, tmp_path_factory):
    """Point every test at a throwaway vault.

    Without this, any test that forgot to set VAULT_PATH wrote to the real
    vault: test_llm_retry logged fake 'contract failed' rows into
    ~/SakethVault/_wiki/meta/llm_call_logs.jsonl on every run. Tests that set
    their own VAULT_PATH still override this.
    """
    vault = tmp_path_factory.mktemp("vault")
    (vault / "_wiki" / "meta").mkdir(parents=True)
    monkeypatch.setenv("VAULT_PATH", str(vault))
    return vault


@pytest.fixture(autouse=True)
def isolated_queue(monkeypatch, tmp_path_factory):
    """Point queue_manager at a throwaway queue file and lock file.

    QUEUE_PATH and _LOCK_PATH are computed at import time, so the VAULT_PATH
    fixture does not cover them. Without this, test_e2e ingest tests enqueued
    real items into hitl_queue.json at the repo root.
    """
    queue_dir = tmp_path_factory.mktemp("queue")
    queue_path = queue_dir / "hitl_queue.json"
    monkeypatch.setattr(queue_manager, "QUEUE_PATH", queue_path)
    monkeypatch.setattr(queue_manager, "_LOCK_PATH", queue_path.with_suffix(".lock"))
    return queue_path
