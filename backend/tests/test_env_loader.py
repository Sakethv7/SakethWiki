import importlib
import sys

import pytest

import wiki_writer


@pytest.fixture
def fake_root_env(monkeypatch, tmp_path):
    """Run the loader against a .env in a temp dir that sets VAULT_PATH to a decoy."""
    (tmp_path / ".env").write_text("VAULT_PATH=/decoy/real/vault\nOTHER_KEY_FOR_TEST=from_env_file\n")
    monkeypatch.delenv("OTHER_KEY_FOR_TEST", raising=False)
    # Loader reads Path(__file__).parent.parent / ".env"; point __file__ at tmp_path/backend/x.py.
    (tmp_path / "backend").mkdir()
    src = (importlib.util.find_spec("wiki_writer").origin)
    return tmp_path, src


def _reload_with_env_root(monkeypatch, root, src):
    spec = importlib.util.spec_from_file_location("wiki_writer_envtest", src)
    mod = importlib.util.module_from_spec(spec)
    mod.__file__ = str(root / "backend" / "wiki_writer.py")
    sys.modules["wiki_writer_envtest"] = mod
    spec.loader.exec_module(mod)
    return mod


def test_env_file_does_not_override_existing_vault_path(monkeypatch, tmp_path, fake_root_env):
    root, src = fake_root_env
    monkeypatch.setenv("VAULT_PATH", str(tmp_path / "isolated"))
    _reload_with_env_root(monkeypatch, root, src)
    import os
    assert os.environ["VAULT_PATH"] == str(tmp_path / "isolated")
    assert os.environ["OTHER_KEY_FOR_TEST"] == "from_env_file"  # unset keys still load
    monkeypatch.delenv("OTHER_KEY_FOR_TEST", raising=False)


def test_plain_reload_keeps_vault_path(monkeypatch, tmp_path):
    monkeypatch.setenv("VAULT_PATH", str(tmp_path))
    importlib.reload(wiki_writer)
    import os
    assert os.environ["VAULT_PATH"] == str(tmp_path)
