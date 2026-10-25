"""Procurement app: FastAPI + SQLite/PostgreSQL, with BYOK AI form prefill."""
import json
import re
import secrets
from contextlib import asynccontextmanager
from pathlib import Path
from urllib import error as urlerror
from urllib import request as urlrequest

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

import db
import workflow


@asynccontextmanager
async def lifespan(_app: FastAPI):
    db.init_db()
    yield


app = FastAPI(title="Procurement", lifespan=lifespan)
templates = Jinja2Templates(directory=str(Path(__file__).with_name("templates")))
app.mount("/static", StaticFiles(directory=str(Path(__file__).with_name("static"))), name="static")

STAFF = ("procurement", "admin")


# ---------------------------------------------------------------- auth
def user_from_token(token: str | None) -> dict | None:
    if not token:
        return None
    with db.connect() as con:
        row = con.execute(
            "SELECT u.* FROM sessions s JOIN users u ON u.id = s.user_id WHERE s.token = ?",
            (token,),
        ).fetchone()
    return dict(row) if row else None


def current_user(request: Request) -> dict:
    user = user_from_token(request.cookies.get("sid"))
    if not user:
        raise HTTPException(status_code=401, detail="Silakan login")
    return user


def require_staff(user: dict) -> None:
    if user["role"] not in STAFF:
        raise HTTPException(status_code=403, detail="Khusus tim procurement")


def render(request: Request, name: str, user: dict | None = None, **ctx) -> HTMLResponse:
    return templates.TemplateResponse(
        request, name, {"user": user, "STATUS_LABEL": workflow.STATUS_LABEL, **ctx}
    )


@app.get("/healthz")
def healthz():
    return {"ok": True}


@app.get("/login", response_class=HTMLResponse)
def login_form(request: Request):
    return render(request, "login.html")


@app.post("/login")
def login(request: Request, email: str = Form(""), password: str = Form("")):
    # Optional fields: empty strings on Form(...) would 422 before we can explain.
    with db.connect() as con:
        row = (con.execute("SELECT * FROM users WHERE email = ?", (email.strip().lower(),)).fetchone()
               if email.strip() else None)
        if not row or not db.verify_password(password, row["password_hash"]):
            # Never echo the password back to the browser.
            return render(request, "login.html", error="Email atau password salah", email=email.strip())
        token = secrets.token_urlsafe(32)
        con.execute(
            "INSERT INTO sessions (token, user_id, created_at) VALUES (?,?,?)",
            (token, row["id"], db.now()),
        )
    resp = RedirectResponse("/", status_code=303)
    resp.set_cookie("sid", token, httponly=True, samesite="lax", max_age=7 * 86400)
    return resp


@app.post("/logout")
def logout(request: Request):
    token = request.cookies.get("sid")
    if token:
        with db.connect() as con:
            con.execute("DELETE FROM sessions WHERE token = ?", (token,))
    resp = RedirectResponse("/login", status_code=303)
    resp.delete_cookie("sid")
    return resp


# ------------------------------------------------------------ dashboard
@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request, user: dict = Depends(current_user)):
    with db.connect() as con:
        if user["role"] == "requester":
            rows = con.execute(
                """SELECT r.*, u.name AS requester_name,
                          (SELECT SUM(qty * est_price) FROM request_items i WHERE i.request_id = r.id) AS total
                   FROM requests r JOIN users u ON u.id = r.requester_id
                   WHERE r.requester_id = ? ORDER BY r.id DESC""",
                (user["id"],),
            ).fetchall()
        elif user["role"] == "manager":
            rows = con.execute(
                """SELECT r.*, u.name AS requester_name,
                          (SELECT SUM(qty * est_price) FROM request_items i WHERE i.request_id = r.id) AS total
                   FROM requests r JOIN users u ON u.id = r.requester_id
                   WHERE r.status IN ('verified','approved','po_issued','received','closed','rejected')
                   ORDER BY r.id DESC"""
            ).fetchall()
        else:
            rows = con.execute(
                """SELECT r.*, u.name AS requester_name,
                          (SELECT SUM(qty * est_price) FROM request_items i WHERE i.request_id = r.id) AS total
                   FROM requests r JOIN users u ON u.id = r.requester_id
                   ORDER BY r.id DESC"""
            ).fetchall()
        vendors = con.execute("SELECT * FROM vendors ORDER BY name").fetchall()
    return render(request, "dashboard.html", user, requests=rows, vendors=vendors)


# ------------------------------------------------------------- requests
# `nama | qty | satuan | harga` — unit and price are optional: "Kursi | 2" is valid.
ITEM_LINE = re.compile(
    r"""
    ^\s*
    (?P<name>.+?)                          # item name (anything before the first |)
    \s*\|\s*
    (?P<qty>\d+(?:[.,]\d+)?)               # quantity, 1.5 or 1,5
    (?:\s*\|\s*(?P<unit>[A-Za-z]+))?       # optional unit
    (?:\s*\|\s*(?P<price>[\d.,]+))?        # optional unit price
    \s*$
    """,
    re.VERBOSE,
)


def parse_items(raw: str) -> list[tuple[str, float, str, float]]:
    """`name | qty | unit | price` per line. Blank/invalid lines are skipped."""
    items = []
    for line in raw.splitlines():
        if not line.strip():
            continue
        m = ITEM_LINE.match(line)
        if not m:
            continue
        price = (m["price"] or "0").replace(".", "").replace(",", ".")
        try:
            qty = float((m["qty"] or "1").replace(",", "."))
            price = float(price or 0)
        except ValueError:
            continue
        if qty > 0:
            items.append((m["name"].strip(), qty, (m["unit"] or "pcs").strip(), max(price, 0)))
    return items


def _validated_items(title: str, raw_items: str) -> list[tuple[str, float, str, float]]:
    """Parse the item textarea and check the form is usable. Raises 400 otherwise."""
    parsed = parse_items(raw_items)
    if not title.strip():
        raise HTTPException(400, "Judul wajib diisi")
    if not parsed:
        raise HTTPException(400, "Minimal satu item valid (format: nama | qty | unit | harga)")
    return parsed


@app.post("/requests")
def create_request(
    user: dict = Depends(current_user),
    title: str = Form(""),
    department: str = Form(""),
    justification: str = Form(""),
    needed_date: str = Form(""),
    items: str = Form(""),
):
    # Form fields are optional: this FastAPI version rejects empty strings on
    # required Form(...) with a 422 before our messages can be shown.
    if user["role"] not in ("requester", "admin"):
        raise HTTPException(403, "Hanya requester yang dapat membuat PR")
    parsed = _validated_items(title, items)
    stamp = db.now()
    with db.connect() as con:
        count = con.execute("SELECT COUNT(*) FROM requests").fetchone()[0] + 1
        code = f"PR-{stamp[:7].replace('-', '')}-{count:03d}"
        cur = con.execute(
            """INSERT INTO requests (code, requester_id, title, department, justification,
                                     needed_date, status, created_at, updated_at)
               VALUES (?,?,?,?,?,?,'draft',?,?)
               RETURNING id""",
            (code, user["id"], title.strip(), department.strip() or user["department"] or "-",
             justification.strip(), needed_date, stamp, stamp),
        )
        rid = cur.fetchone()[0]
        con.executemany(
            "INSERT INTO request_items (request_id, name, qty, unit, est_price) VALUES (?,?,?,?,?)",
            [(rid, *item) for item in parsed],
        )
        con.execute(
            "INSERT INTO request_history (request_id, actor_id, action, to_status, created_at) VALUES (?,?,?,?,?)",
            (rid, user["id"], "created", "draft", stamp),
        )
    return RedirectResponse(f"/requests/{rid}", status_code=303)


@app.post("/requests/{rid}/edit")
def edit_request(
    rid: int,
    user: dict = Depends(current_user),
    title: str = Form(""),
    department: str = Form(""),
    justification: str = Form(""),
    needed_date: str = Form(""),
    items: str = Form(""),
):
    """Only the owner can edit, and only while the request is a draft."""
    parsed = _validated_items(title, items)
    with db.connect() as con:
        req = con.execute("SELECT * FROM requests WHERE id = ?", (rid,)).fetchone()
        if not req:
            raise HTTPException(404, "Request tidak ditemukan")
        if req["requester_id"] != user["id"] or req["status"] != "draft":
            raise HTTPException(403, "Hanya draft milik sendiri yang bisa diubah")
        con.execute(
            """UPDATE requests SET title = ?, department = ?, justification = ?, needed_date = ?,
               updated_at = ? WHERE id = ?""",
            (title.strip(), department.strip() or "-", justification.strip(), needed_date, db.now(), rid),
        )
        con.execute("DELETE FROM request_items WHERE request_id = ?", (rid,))
        con.executemany(
            "INSERT INTO request_items (request_id, name, qty, unit, est_price) VALUES (?,?,?,?,?)",
            [(rid, *item) for item in parsed],
        )
    return RedirectResponse(f"/requests/{rid}", status_code=303)


@app.get("/requests/{rid}", response_class=HTMLResponse)
def request_detail(request: Request, rid: int, user: dict = Depends(current_user)):
    with db.connect() as con:
        req = con.execute(
            "SELECT r.*, u.name AS requester_name FROM requests r JOIN users u ON u.id = r.requester_id WHERE r.id = ?",
            (rid,),
        ).fetchone()
        if not req:
            raise HTTPException(404, "Request tidak ditemukan")
        items = con.execute("SELECT * FROM request_items WHERE request_id = ?", (rid,)).fetchall()
        history = con.execute(
            """SELECT h.*, u.name AS actor_name FROM request_history h
               LEFT JOIN users u ON u.id = h.actor_id WHERE h.request_id = ? ORDER BY h.id""",
            (rid,),
        ).fetchall()
        quotations = con.execute(
            """SELECT q.*, v.name AS vendor_name FROM quotations q
               JOIN vendors v ON v.id = q.vendor_id WHERE q.request_id = ? ORDER BY q.total""",
            (rid,),
        ).fetchall()
        po = con.execute(
            """SELECT p.*, v.name AS vendor_name
               FROM purchase_orders p JOIN vendors v ON v.id = p.vendor_id
               WHERE p.request_id = ?""",
            (rid,),
        ).fetchone()
        grn = con.execute("SELECT * FROM goods_receipts WHERE po_id = ?", (po["id"],)).fetchone() if po else None
        vendors = con.execute("SELECT * FROM vendors ORDER BY name").fetchall()

    is_owner = req["requester_id"] == user["id"]
    actions = [
        a for a in workflow.TRANSITIONS
        if workflow.allowed(a, user["role"], req["status"], is_owner=is_owner)
    ]
    can_quote = user["role"] in STAFF and req["status"] in ("approved", "sourcing")
    total_est = sum(i["qty"] * i["est_price"] for i in items)
    return render(
        request, "request.html", user,
        req=req, items=items, history=history, quotations=quotations, po=po, grn=grn,
        vendors=vendors, actions=actions, can_quote=can_quote, is_owner=is_owner,
        total_est=total_est,
    )


def _load(con, rid: int) -> dict:
    """Load a request inside an open connection, or 404."""
    row = con.execute("SELECT * FROM requests WHERE id = ?", (rid,)).fetchone()
    if not row:
        raise HTTPException(404, "Request tidak ditemukan")
    return row


@app.post("/requests/{rid}/action")
def act(rid: int, action: str = Form(""), note: str = Form(""), user: dict = Depends(current_user)):
    with db.connect() as con:
        req = _load(con, rid)
        is_owner = req["requester_id"] == user["id"]
        if not workflow.allowed(action, user["role"], req["status"], is_owner=is_owner):
            raise HTTPException(403, f"Aksi '{action or 'kosong'}' tidak diizinkan dari status '{req['status']}'")
        target = workflow.TRANSITIONS[action][1]
        _write_status(con, rid, user["id"], action, req["status"], target, note)
    return RedirectResponse(f"/requests/{rid}", status_code=303)


def _write_status(con, rid: int, actor_id: int, action: str, frm: str, to: str, note: str = "") -> None:
    stamp = db.now()
    con.execute("UPDATE requests SET status = ?, updated_at = ? WHERE id = ?", (to, stamp, rid))
    con.execute(
        """INSERT INTO request_history (request_id, actor_id, action, from_status, to_status, note, created_at)
           VALUES (?,?,?,?,?,?,?)""",
        (rid, actor_id, action, frm, to, note.strip(), stamp),
    )


@app.post("/requests/{rid}/quotations")
def add_quotation(
    rid: int,
    vendor_id: int = Form(...),
    total: float = Form(...),
    lead_time_days: int = Form(0),
    note: str = Form(""),
    user: dict = Depends(current_user),
):
    require_staff(user)
    if total < 0:
        raise HTTPException(400, "Total tidak boleh negatif")
    with db.connect() as con:
        req = _load(con, rid)
        if req["status"] not in ("approved", "sourcing"):
            raise HTTPException(400, "Quotation hanya bisa ditambahkan setelah request disetujui")
        con.execute(
            """INSERT INTO quotations (request_id, vendor_id, total, lead_time_days, note, created_by, created_at)
               VALUES (?,?,?,?,?,?,?)""",
            (rid, vendor_id, total, lead_time_days, note.strip(), user["id"], db.now()),
        )
        if req["status"] == "approved":
            _write_status(con, rid, user["id"], "source", "approved", "sourcing")
    return RedirectResponse(f"/requests/{rid}", status_code=303)


@app.post("/requests/{rid}/po")
def issue_po(rid: int, quotation_id: int = Form(...), user: dict = Depends(current_user)):
    require_staff(user)
    stamp = db.now()
    with db.connect() as con:
        req = _load(con, rid)
        if not workflow.allowed("issue_po", user["role"], req["status"], is_owner=False):
            raise HTTPException(403, "PO hanya bisa diterbitkan saat request dalam status sourcing")
        quote = con.execute(
            "SELECT * FROM quotations WHERE id = ? AND request_id = ?", (quotation_id, rid)
        ).fetchone()
        if not quote:
            raise HTTPException(404, "Quotation tidak ditemukan untuk request ini")
        code = f"PO-{stamp[:7].replace('-', '')}-{rid:03d}"
        vendor_name = con.execute("SELECT name FROM vendors WHERE id = ?", (quote["vendor_id"],)).fetchone()["name"]
        con.execute(
            """INSERT INTO purchase_orders (code, request_id, vendor_id, total, created_by, created_at)
               VALUES (?,?,?,?,?,?)""",
            (code, rid, quote["vendor_id"], quote["total"], user["id"], stamp),
        )
        # CASE … 1/0 keeps this valid on both engines (PostgreSQL types `id = ?` as boolean).
        con.execute(
            "UPDATE quotations SET selected = CASE WHEN id = ? THEN 1 ELSE 0 END WHERE request_id = ?",
            (quotation_id, rid),
        )
        _write_status(con, rid, user["id"], "issue_po", req["status"], "po_issued",
                      f"{code} untuk {vendor_name} senilai Rp {quote['total']:,.0f}")
    return RedirectResponse(f"/requests/{rid}", status_code=303)


@app.post("/requests/{rid}/receive")
def receive(rid: int, note: str = Form(""), user: dict = Depends(current_user)):
    require_staff(user)
    stamp = db.now()
    with db.connect() as con:
        req = _load(con, rid)
        if not workflow.allowed("receive", user["role"], req["status"], is_owner=False):
            raise HTTPException(403, "Barang hanya bisa diterima setelah PO terbit")
        po = con.execute("SELECT * FROM purchase_orders WHERE request_id = ?", (rid,)).fetchone()
        if not po:
            raise HTTPException(400, "PO belum ada")
        con.execute(
            "INSERT INTO goods_receipts (po_id, received_at, note, received_by) VALUES (?,?,?,?)",
            (po["id"], stamp, note.strip(), user["id"]),
        )
        con.execute("UPDATE purchase_orders SET status = 'received' WHERE id = ?", (po["id"],))
        _write_status(con, rid, user["id"], "receive", req["status"], "received", note)
    return RedirectResponse(f"/requests/{rid}", status_code=303)


# ------------------------------------------------------- AI form prefill
# Bring-your-own key. Any OpenAI-compatible /chat/completions endpoint works
# (OpenAI, OpenRouter, Groq, DeepSeek, Together, Ollama, vLLM, ...).
PROMPT = (
    "Kamu mengubah dokumen pengadaan (quotation, PO, invoice, foto nota) menjadi item. "
    "Balas HANYA JSON: {\"items\":[{\"name\":str,\"qty\":number,\"unit\":str,\"est_price\":number}]}. "
    "est_price = harga satuan tanpa pemisah ribuan, tanpa mata uang. "
    "Jika tidak ada item, balas {\"items\":[]}."
)


def ai_settings(user_id: int) -> dict | None:
    with db.connect() as con:
        row = con.execute("SELECT * FROM ai_settings WHERE user_id = ?", (user_id,)).fetchone()
    return dict(row) if row else None


def ai_extract(text: str) -> list[dict]:
    """Fallback: parse `nama | qty | satuan | harga` lines without any model."""
    items = []
    for line in text.splitlines():
        parsed = parse_items(line)
        for name, qty, unit, price in parsed:
            items.append({"name": name, "qty": qty, "unit": unit, "est_price": price})
    return items


def _norm_items(raw) -> list[dict]:
    """Validate/normalise whatever shape the model returned."""
    out = []
    for it in (raw if isinstance(raw, list) else [])[:200]:
        if not isinstance(it, dict):
            continue
        name = str(it.get("name", "")).strip()[:200]
        try:
            qty = float(it.get("qty", 0) or 0)
            price = float(it.get("est_price", 0) or 0)
        except (TypeError, ValueError):
            continue
        if name and qty > 0:
            out.append({"name": name, "qty": qty,
                        "unit": str(it.get("unit") or "pcs").strip()[:20] or "pcs",
                        "est_price": max(price, 0)})
    return out


def call_model(cfg: dict, text: str, image_data_url: str | None) -> list[dict]:
    """POST to an OpenAI-compatible endpoint. Raises HTTPException on failure."""
    content = [{"type": "text", "text": text or "Ekstrak item dari dokumen ini."}]
    if image_data_url:
        content.append({"type": "image_url", "image_url": {"url": image_data_url}})
    body = json.dumps({
        "model": cfg["model"],
        "temperature": 0,
        "messages": [
            {"role": "system", "content": PROMPT},
            {"role": "user", "content": content},
        ],
    }).encode()
    req = urlrequest.Request(
        cfg["base_url"].rstrip("/") + "/chat/completions",
        data=body,
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {cfg['api_key']}"},
        method="POST",
    )
    try:
        with urlrequest.urlopen(req, timeout=60) as resp:
            payload = json.loads(resp.read())
    except urlerror.HTTPError as e:
        detail = e.read().decode(errors="replace")[:300]
        raise HTTPException(502, f"Provider menolak ({e.code}): {detail}") from None
    except Exception as e:
        raise HTTPException(502, f"Gagal menghubungi provider: {e}") from None

    try:
        text_out = payload["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        # from None: the provider's internals are not the user's business.
        raise HTTPException(502, "Respons provider tidak sesuai format OpenAI chat/completions") from None
    match = re.search(r"\{.*\}", text_out, re.S)
    if not match:
        raise HTTPException(502, "Model tidak mengembalikan JSON")
    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError:
        raise HTTPException(502, "JSON dari model tidak valid") from None
    return _norm_items(data.get("items"))


@app.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request, user: dict = Depends(current_user)):
    cfg = ai_settings(user["id"])
    masked = f"••••{cfg['api_key'][-4:]}" if cfg and len(cfg["api_key"]) > 4 else None
    return render(request, "settings.html", user, cfg=cfg, masked=masked, saved=request.query_params.get("saved"))


@app.post("/settings")
def save_settings(
    user: dict = Depends(current_user),
    base_url: str = Form(""),
    model: str = Form(""),
    api_key: str = Form(""),
):
    # Form fields are declared optional: this FastAPI version rejects empty
    # strings on required Form(...) with a 422 before our validation can run.
    base = base_url.strip().rstrip("/")
    if not base.startswith(("https://", "http://")):
        raise HTTPException(400, "Base URL harus diawali http(s)://")
    if not model.strip():
        raise HTTPException(400, "Model wajib diisi")
    existing = ai_settings(user["id"])
    # Empty api_key on save = keep the stored key (so you can edit URL/model).
    key = api_key.strip() or (existing["api_key"] if existing else "")
    if not key:
        raise HTTPException(400, "API key wajib diisi")
    with db.connect() as con:
        con.execute(
            """INSERT INTO ai_settings (user_id, base_url, model, api_key, updated_at)
               VALUES (?,?,?,?,?)
               ON CONFLICT(user_id) DO UPDATE SET
                 base_url=excluded.base_url, model=excluded.model,
                 api_key=excluded.api_key, updated_at=excluded.updated_at""",
            (user["id"], base, model.strip(), key, db.now()),
        )
    return RedirectResponse("/settings?saved=1", status_code=303)


@app.post("/settings/test")
def test_settings(user: dict = Depends(current_user)):
    cfg = ai_settings(user["id"])
    if not cfg:
        raise HTTPException(400, "Simpan konfigurasi dulu")
    items = call_model(cfg, "Laptop ThinkPad | 1 | unit | 15000000", None)
    return {"ok": True, "sample": items}


@app.post("/api/extract")
def extract(payload: dict, user: dict = Depends(current_user)):
    """Prefill request items from text or an uploaded image using the user's own key.
    Falls back to the pipe-format parser when no key is configured."""
    text = (payload.get("text") or "").strip()
    image = payload.get("image")  # data URL: data:image/png;base64,...
    if not text and not image:
        raise HTTPException(400, "Kirim {text} atau {image}")
    if image and not image.startswith("data:image/"):
        raise HTTPException(400, "image harus berupa data URL image/*")
    if image and len(image) > 8_000_000:
        raise HTTPException(413, "Gambar terlalu besar (maks ~6 MB)")

    cfg = ai_settings(user["id"])
    if cfg:
        return {"items": call_model(cfg, text, image), "source": "model"}
    if image:
        raise HTTPException(400, "Computer Vision butuh API key — atur di /settings")
    return {"items": ai_extract(text), "source": "parser"}


@app.post("/settings/delete")
def delete_settings(user: dict = Depends(current_user)):
    with db.connect() as con:
        con.execute("DELETE FROM ai_settings WHERE user_id = ?", (user["id"],))
    return RedirectResponse("/settings", status_code=303)
