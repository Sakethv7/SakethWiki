import pytest


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
