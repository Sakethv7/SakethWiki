"""Non-loopback callers reach only the phone upload page and POST /ingest."""
from fastapi.testclient import TestClient

import main

LOCAL = TestClient(main.app, client=("127.0.0.1", 50000))
PHONE = TestClient(main.app, client=("192.168.1.20", 50000))


def test_phone_blocked_from_desktop_routes():
    for method, path in [("GET", "/pages"), ("GET", "/queue"), ("POST", "/approve/x"),
                         ("DELETE", "/page/x"), ("GET", "/qr-code"), ("GET", "/page/x")]:
        r = PHONE.request(method, path)
        assert r.status_code == 403, (method, path)
        assert r.json() == {"detail": "local requests only"}


def test_phone_reaches_upload_page_and_ingest():
    assert PHONE.get("/mobile").status_code == 200
    # empty body passes the guard and fails validation (400), not the guard (403)
    assert PHONE.post("/ingest", json={}).status_code == 400


def test_phone_cannot_use_other_methods_on_allowed_paths():
    assert PHONE.get("/ingest").status_code == 403
    assert PHONE.post("/mobile").status_code == 403


def test_local_caller_unaffected():
    assert LOCAL.get("/health").status_code == 200
