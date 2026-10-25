"""End-to-end checks: workflow rules + full happy path through HTTP + AI prefill.

Runs on SQLite by default, or on PostgreSQL when TEST_DATABASE_URL is set:
    $env:TEST_DATABASE_URL = "postgresql://postgres:postgres@127.0.0.1:5432/procurement_test"
"""
import json
import os
import tempfile

TEST_PG = os.getenv("TEST_DATABASE_URL", "").strip()
if TEST_PG:
    os.environ["DATABASE_URL"] = TEST_PG
else:
    os.environ["PROCUREMENT_DB"] = os.path.join(tempfile.mkdtemp(), "test.db")

from fastapi.testclient import TestClient  # noqa: E402

import app  # noqa: E402
import db  # noqa: E402
import workflow  # noqa: E402

TABLES = ("ai_settings", "goods_receipts", "purchase_orders", "quotations",
          "request_history", "request_items", "requests", "sessions",
          "vendors", "users")


def _reset_pg() -> None:
    """Tests assume an empty database; PostgreSQL persists between runs."""
    db.init_schema()
    with db.connect() as con:
        con.execute("TRUNCATE " + ", ".join(TABLES) + " RESTART IDENTITY CASCADE")


if TEST_PG:
    _reset_pg()


def login(client, email):
    r = client.post("/login", data={"email": email, "password": "demo123"}, follow_redirects=False)
    assert r.status_code == 303, r.text


def test_workflow_rules():
    assert workflow.allowed("submit", "requester", "draft", is_owner=True)
    assert not workflow.allowed("submit", "requester", "draft", is_owner=False)
    assert not workflow.allowed("submit", "procurement", "draft", is_owner=True)
    assert not workflow.allowed("verify", "manager", "submitted", is_owner=False)
    assert not workflow.allowed("approve", "manager", "submitted", is_owner=False)
    assert workflow.allowed("approve", "manager", "verified", is_owner=False)
    assert not workflow.allowed("receive", "procurement", "sourcing", is_owner=False)
    assert not workflow.allowed("receive", "procurement", "approved", is_owner=False)
    assert workflow.allowed("revise", "procurement", "submitted", is_owner=False)
    assert not workflow.allowed("revise", "requester", "submitted", is_owner=True)
    assert not workflow.allowed("bogus", "admin", "draft", is_owner=True)


def test_parse_items():
    raw = "Laptop | 5 | unit | 14.500.000\n   \nRusak baris ini\nKertas A4 | 20 | rim"
    items = app.parse_items(raw)
    assert items == [
        ("Laptop", 5.0, "unit", 14_500_000.0),
        ("Kertas A4", 20.0, "rim", 0.0),
    ]


def test_validation_messages():
    with TestClient(app.app) as c:
        login(c, "budi@demo.test")
        assert c.post("/requests", data={"title": "", "items": "Kursi | 1 | unit | 1"}).status_code == 400
        assert c.post("/requests", data={"title": "x", "items": "baris sampah"}).status_code == 400
        # procurement may create nothing
        with TestClient(app.app) as staff:
            login(staff, "sari@demo.test")
            assert staff.post("/requests", data={
                "title": "x", "items": "Kursi | 1 | unit | 1"}).status_code == 403


def test_login_errors_are_explained_and_password_is_not_echoed():
    """Empty fields must not 422, and a failed login must not resend the password."""
    import re

    with TestClient(app.app) as c:
        html = c.post("/login", data={"email": "", "password": ""}).text
        assert c.post("/login", data={"email": "", "password": ""}).status_code == 200
        assert "Email atau password salah" in html

        html = c.post("/login", data={"email": "budi@demo.test", "password": "rahasia123"}).text
        password_input = re.search(r'<input[^>]*name="password"[^>]*>', html).group(0)
        assert "rahasia123" not in password_input
        assert 'value="budi@demo.test"' in html   # email is kept for convenience


def test_full_happy_path():
    with TestClient(app.app) as requester:
        login(requester, "budi@demo.test")

        r = requester.post("/requests", data={
            "title": "Laptop QA", "department": "IT", "justification": "tim baru",
            "needed_date": "2026-11-01",
            "items": "Laptop ThinkPad | 2 | unit | 14.500.000\nMonitor 24in | 2 | unit | 2.000.000",
        }, follow_redirects=False)
        assert r.status_code == 303
        loc = r.headers["location"]
        rid = int(loc.rsplit("/", 1)[1])

        rid_html = requester.get(loc).text
        assert "PR-" in rid_html and "Laptop ThinkPad" in rid_html
        assert "33.000.000" in rid_html  # 2 * 14.5M + 2 * 2M

        # requester cannot verify, and cannot act on other people's requests
        assert requester.post(f"/requests/{rid}/action", data={"action": "verify"}).status_code == 403
        assert requester.post("/requests/9999/action", data={"action": "submit"}).status_code == 404

        r = requester.post(f"/requests/{rid}/action", data={"action": "submit"}, follow_redirects=False)
        assert r.status_code == 303

    with TestClient(app.app) as staff:
        login(staff, "sari@demo.test")
        for action in ("verify",):
            assert staff.post(f"/requests/{rid}/action", data={"action": action},
                              follow_redirects=False).status_code == 303
        # procurement can't approve (manager-only)
        assert staff.post(f"/requests/{rid}/action", data={"action": "approve"}).status_code == 403

    with TestClient(app.app) as manager:
        login(manager, "andi@demo.test")
        assert manager.post(f"/requests/{rid}/action", data={"action": "approve"},
                            follow_redirects=False).status_code == 303

    with TestClient(app.app) as staff:
        login(staff, "sari@demo.test")
        # can't add quotation before approval -> but this IS approved now, so it works
        assert staff.post(f"/requests/{rid}/quotations", data={
            "vendor_id": 1, "total": 30_000_000, "lead_time_days": 5, "note": "termurah",
        }, follow_redirects=False).status_code == 303

        page = staff.get(f"/requests/{rid}").text
        assert "CV Teknologi Nusantara" in page
        assert "Terbitkan PO" in page

        with db.connect() as con:
            qid = con.execute("SELECT id FROM quotations WHERE request_id = ?", (rid,)).fetchone()[0]

        assert staff.post(f"/requests/{rid}/po", data={"quotation_id": qid},
                          follow_redirects=False).status_code == 303
        page = staff.get(f"/requests/{rid}").text
        assert "PO-" in page

        assert staff.post(f"/requests/{rid}/receive", data={"note": "2 unit diterima"},
                          follow_redirects=False).status_code == 303
        page = staff.get(f"/requests/{rid}").text
        assert "Barang diterima" in page and "2 unit diterima" in page

    with db.connect() as con:
        req = con.execute("SELECT status FROM requests WHERE id = ?", (rid,)).fetchone()
        po = con.execute("SELECT status FROM purchase_orders WHERE request_id = ?", (rid,)).fetchone()
        hist = con.execute("SELECT COUNT(*) FROM request_history WHERE request_id = ?", (rid,)).fetchone()[0]
    assert req["status"] == "received"
    assert po["status"] == "received"
    assert hist == 7, f"history rows: {hist}"


def test_revise_and_edit_draft():
    with TestClient(app.app) as requester:
        login(requester, "budi@demo.test")
        r = requester.post("/requests", data={
            "title": "Kursi kantor", "items": "Kursi | 3 | unit | 1.000.000",
        }, follow_redirects=False)
        rid = int(r.headers["location"].rsplit("/", 1)[1])

        # edit while draft works
        assert requester.post(f"/requests/{rid}/edit", data={
            "title": "Kursi kantor ergonomis", "items": "Kursi ergo | 3 | unit | 1.500.000",
        }, follow_redirects=False).status_code == 303
        page = requester.get(f"/requests/{rid}").text
        assert "Kursi ergo" in page and "4.500.000" in page

        requester.post(f"/requests/{rid}/action", data={"action": "submit"})
        # edit after submit is refused
        assert requester.post(f"/requests/{rid}/edit", data={
            "title": "x", "items": "Kursi | 1 | unit | 1",
        }).status_code == 403

    with TestClient(app.app) as staff:
        login(staff, "sari@demo.test")
        assert staff.post(f"/requests/{rid}/action", data={"action": "revise"},
                          follow_redirects=False).status_code == 303

    with TestClient(app.app) as requester:
        login(requester, "budi@demo.test")
        page = requester.get(f"/requests/{rid}").text
        assert "Ubah draft" in page  # back to draft, editable again
        assert requester.post(f"/requests/{rid}/action", data={"action": "submit"},
                              follow_redirects=False).status_code == 303


def test_auth_required_and_extract():
    with TestClient(app.app) as anon:
        assert anon.get("/", follow_redirects=False).status_code == 401
        assert anon.post("/api/extract", json={"text": "Tinta | 4 | pcs | 120.000"}).status_code == 401

    with TestClient(app.app) as requester:
        login(requester, "budi@demo.test")
        # no key configured -> local parser
        r = requester.post("/api/extract", json={"text": "Tinta | 4 | pcs | 120.000"})
        assert r.status_code == 200
        assert r.json() == {"items": [{"name": "Tinta", "qty": 4.0, "unit": "pcs",
                                       "est_price": 120000.0}], "source": "parser"}
        # image without a key is a clear error, not a silent empty list
        r = requester.post("/api/extract", json={"image": "data:image/png;base64,AAA"})
        assert r.status_code == 400 and "settings" in r.json()["detail"]

        # settings validation
        assert requester.post("/settings", data={"base_url": "ftp://x", "model": "m",
                                                 "api_key": "k"}).status_code == 400
        assert requester.post("/settings", data={"base_url": "https://api.x/v1", "model": "",
                                                 "api_key": "k"}).status_code == 400
        assert requester.post("/settings", data={"base_url": "https://api.x/v1", "model": "m",
                                                 "api_key": ""}).status_code == 400

        # save, then "keep existing key" by sending an empty api_key
        assert requester.post("/settings", data={
            "base_url": "https://api.x/v1/", "model": "m1", "api_key": "sk-secret-abcd",
        }, follow_redirects=False).status_code == 303
        assert requester.post("/settings", data={
            "base_url": "https://api.y/v1", "model": "m2", "api_key": "",
        }, follow_redirects=False).status_code == 303

        with db.connect() as con:
            row = con.execute("SELECT * FROM ai_settings").fetchone()
        assert row["base_url"] == "https://api.y/v1" and row["model"] == "m2"
        assert row["api_key"] == "sk-secret-abcd", "empty key must keep the stored one"

        # key never rendered back in full
        page = requester.get("/settings").text
        assert "sk-secret-abcd" not in page and "••••abcd" in page


def test_model_response_is_validated(monkeypatch):
    """Provider output is untrusted: bad rows dropped, strings coerced, JSON fenced."""
    fake = {"choices": [{"message": {"content":
        '```json\n{"items":[{"name":"Laptop","qty":"2","unit":"unit","est_price":"15000000"},'
        '{"name":"","qty":1},{"name":"Rusak","qty":"abc"},{"name":"Monitor","qty":1}]}\n```'}}]}

    class Resp:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self): return json.dumps(fake).encode()

    monkeypatch.setattr(app.urlrequest, "urlopen", lambda *a, **k: Resp())
    items = app.call_model({"base_url": "https://api.x/v1", "model": "m", "api_key": "k"},
                           "apa saja", None)
    assert items == [
        {"name": "Laptop", "qty": 2.0, "unit": "unit", "est_price": 15000000.0},
        {"name": "Monitor", "qty": 1.0, "unit": "pcs", "est_price": 0.0},
    ]


def test_provider_error_surfaces(monkeypatch):
    def boom(*a, **k):
        raise app.urlerror.HTTPError("https://api.x/v1", 401, "Unauthorized", {}, None)

    monkeypatch.setattr(app.urlrequest, "urlopen", boom)
    try:
        app.call_model({"base_url": "https://api.x/v1", "model": "m", "api_key": "bad"}, "x", None)
    except app.HTTPException as e:
        assert e.status_code == 502 and "401" in e.detail
    else:
        raise AssertionError("expected HTTPException")
