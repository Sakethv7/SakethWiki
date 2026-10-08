"""POST /open-in-app: path guard, loopback guard, exact command. subprocess.run is patched."""
import subprocess

import pytest
from fastapi.testclient import TestClient

import main
import vault_reader

LOCAL = TestClient(main.app, client=("127.0.0.1", 50000))
REMOTE = TestClient(main.app, client=("192.168.1.20", 50000))


@pytest.fixture
def note(isolated_vault):
    f = isolated_vault / "_wiki" / "cs" / "attention.md"
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text("---\ntitle: Attention\n---\nbody\n", encoding="utf-8")
    return f


@pytest.fixture
def calls(monkeypatch):
    seen = []

    def fake_run(cmd, **kwargs):
        seen.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(main.subprocess, "run", fake_run)
    monkeypatch.setattr(main, "_app_installed", lambda key: True)
    return seen


def test_page_path_matches_read_page(note):
    assert vault_reader.page_path("attention") == "_wiki/cs/attention.md"
    assert vault_reader.page_path("missing-page") is None


def test_obsidian_uses_vault_name_and_path_without_extension(note, isolated_vault, calls):
    r = LOCAL.post("/open-in-app", json={"path": "_wiki/cs/attention.md", "app": "obsidian"})
    assert r.status_code == 200
    assert calls[0][0] == "open"
    assert calls[0][1].startswith(f"obsidian://open?vault={isolated_vault.name}&file=")
    assert calls[0][1].endswith("file=_wiki/cs/attention")


def test_vscode_and_default_commands(note, isolated_vault, calls):
    LOCAL.post("/open-in-app", json={"path": "_wiki/cs/attention.md", "app": "vscode"})
    LOCAL.post("/open-in-app", json={"path": "_wiki/cs/attention.md", "app": "default"})
    target = str((isolated_vault / "_wiki/cs/attention.md").resolve())
    assert calls == [["open", "-a", "Visual Studio Code", target], ["open", target]]


def test_non_markdown_keeps_extension_for_obsidian(isolated_vault, calls):
    f = isolated_vault / "_wiki" / "visuals" / "flow.html"
    f.parent.mkdir(parents=True)
    f.write_text("<html></html>")
    assert LOCAL.post("/open-in-app", json={"path": "_wiki/visuals/flow.html", "app": "obsidian"}).status_code == 200
    assert calls[0][1].endswith("file=_wiki/visuals/flow.html")


def test_rejects_path_outside_vault_and_symlink_escape(note, isolated_vault, calls, tmp_path):
    outside = tmp_path / "secret.md"
    outside.write_text("x")
    (isolated_vault / "link.md").symlink_to(outside)
    for bad in ["../secret.md", "_wiki/../../etc/hosts", "link.md"]:
        r = LOCAL.post("/open-in-app", json={"path": bad, "app": "default"})
        assert r.status_code == 400, bad
    assert calls == []


def test_missing_file_unknown_app_and_remote(note, calls):
    assert LOCAL.post("/open-in-app", json={"path": "_wiki/cs/nope.md", "app": "default"}).status_code == 404
    assert LOCAL.post("/open-in-app", json={"path": "_wiki/cs/attention.md", "app": "rm"}).status_code == 400
    assert REMOTE.post("/open-in-app", json={"path": "_wiki/cs/attention.md", "app": "default"}).status_code == 403
    assert REMOTE.get("/open-in-app/apps").status_code == 403
    assert calls == []


def test_app_not_installed_and_open_failure(note, monkeypatch, calls):
    monkeypatch.setattr(main, "_app_installed", lambda key: False)
    assert LOCAL.post("/open-in-app", json={"path": "_wiki/cs/attention.md", "app": "vscode"}).status_code == 409
    monkeypatch.setattr(main, "_app_installed", lambda key: True)
    monkeypatch.setattr(main.subprocess, "run",
                        lambda cmd, **kw: subprocess.CompletedProcess(cmd, 1, "", "boom\nmore"))
    r = LOCAL.post("/open-in-app", json={"path": "_wiki/cs/attention.md", "app": "default"})
    assert r.status_code == 500 and r.json()["detail"] == "boom"


def test_apps_list_always_ends_with_default(monkeypatch):
    monkeypatch.setattr(main, "_app_installed", lambda key: key == "obsidian")
    apps = LOCAL.get("/open-in-app/apps").json()["apps"]
    assert [a["key"] for a in apps] == ["obsidian", "default"]
