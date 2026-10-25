# ProcuraFlow

Aplikasi web procurement: dari pengajuan request sampai barang/jasa diterima di gudang.

Dibangun dengan **Python (FastAPI)** dan **SQLite atau PostgreSQL**. Termasuk pengisian form otomatis dari foto/scan dokumen memakai AI (Computer Vision) dengan **API key milik sendiri**.

> Di luar cakupan: manajemen gudang dan pembayaran.

---

## Menjalankan

```bash
py -m pip install -r requirements.txt

# SQLite (default, tanpa konfigurasi apa pun)
py -m uvicorn app:app --port 8100

# PostgreSQL
$env:DATABASE_URL = "postgresql://postgres:postgres@127.0.0.1:5432/procurement"
py -m uvicorn app:app --port 8100
```

Buka <http://127.0.0.1:8100>.

> Port 8000 sering sudah dipakai proses lain di Windows, jadi contoh di sini memakai **8100**.

### Akun demo

Password semua akun: `demo123`

| Email | Role |
|---|---|
| `budi@demo.test` | requester |
| `sari@demo.test` | procurement |
| `andi@demo.test` | manager (approver) |
| `admin@demo.test` | admin |

Schema dan data awal dibuat otomatis saat aplikasi start. Lokasi berkas SQLite bisa diubah lewat `PROCUREMENT_DB`.

---

## Alur proses

```
draft → submitted → verified → approved → sourcing → po_issued → received → closed
          ↑          ↓verify    ↓revise               ↘ rejected
        submit      (kembali ke draft)      cancel (draft..sourcing)
```

| Tahap | Pelaku | Keterangan |
|---|---|---|
| Draft | Requester | Bisa diedit pemiliknya |
| Verifikasi | Procurement | Cek kelengkapan; bisa mengembalikan untuk revisi |
| Approval | Manager | Setuju atau tolak, dengan catatan |
| Sourcing | Procurement | Kumpulkan dan bandingkan quotation vendor |
| Purchase Order | Procurement | Diterbitkan dari quotation terpilih |
| Goods Receipt | Procurement | Konfirmasi barang diterima |

Aturan transisi hidup di `workflow.py` sebagai fungsi murni (tanpa database dan tanpa web framework), sehingga mudah diuji dan diaudit. Setiap perubahan status dicatat di `request_history` lengkap dengan pelaku dan catatannya. Draft hanya bisa diubah pemiliknya; setelah di-submit langsung terkunci.

---

## Fitur

**Alur procurement end-to-end** — PR multi-item, verifikasi, approval, perbandingan quotation, PO, dan goods receipt, dengan audit trail di setiap langkah.

**Pengisian form otomatis dengan AI (BYOK)** — menu **AI** di bilah atas (`/settings`) untuk mengatur sendiri **Base URL, API key, dan model**. Provider apa pun yang kompatibel dengan OpenAI `/chat/completions` bisa dipakai.

- Unggah foto/scan quotation, PO, atau nota → model vision membaca itemnya → form PR terisi otomatis (`POST /api/extract`).
- Tombol **Tes koneksi** memanggil provider sekali dengan contoh kecil, untuk memastikan konfigurasi benar.
- Tanpa API key pun form tetap berfungsi memakai parser lokal berformat `nama | qty | satuan | harga` — tanpa panggilan jaringan sama sekali.
- Keluaran model divalidasi ketat (`_norm_items`): baris tanpa nama atau dengan qty tidak valid dibuang, bukan dipercaya mentah-mentah.
- API key disimpan **per pengguna** di tabel `ai_settings`, hanya ditampilkan termasking (`••••abcd`), dan tidak pernah dipanggil otomatis — hanya saat tombol Ekstrak ditekan.

Contoh konfigurasi provider:

| Provider | Base URL | Contoh model |
|---|---|---|
| OpenAI | `https://api.openai.com/v1` | `gpt-4o-mini` |
| OpenRouter | `https://openrouter.ai/api/v1` | `google/gemini-2.0-flash-001` |
| Groq | `https://api.groq.com/openai/v1` | `llama-3.2-11b-vision-preview` |
| Ollama (lokal) | `http://localhost:11434/v1` | `llama3.2-vision` |

**Antarmuka responsif** — layout menyesuaikan layar kecil hingga lebar, dan tabel bisa digeser horizontal supaya tidak merusak tampilan di ponsel.

---

## Struktur proyek

```
app.py         route HTTP + pemanggilan AI provider
workflow.py    state machine (transisi yang diizinkan × role)
db.py          skema + koneksi SQLite & PostgreSQL, data awal
templates/     Jinja2: dashboard, detail PR, pengaturan
static/        CSS, JS, dan ikon
test_app.py    pengujian end-to-end
pyproject.toml konfigurasi ruff
```

---

## Pengujian

```bash
py -m pytest -q      # 9 pengujian
py -m ruff check .   # 0 temuan
```

Menjalankan pengujian di PostgreSQL juga:

```bash
$env:TEST_DATABASE_URL = "postgresql://postgres:postgres@127.0.0.1:5432/procurement_test"
py -m pytest -q
```

Yang diuji: aturan state machine per role dan status, alur lengkap lewat HTTP, validasi keluaran AI, parser item, pesan kesalahan form, serta idempotensi input.

### Kualitas kode

Ruff dikonfigurasi di `pyproject.toml` (panjang baris 120, target Python 3.11). Ada dua pengecualian yang disengaja:

- `B008` di `app.py` — `Depends(...)` pada argumen default adalah idiom resmi FastAPI, bukan kesalahan.
- `ARG` di `test_app.py` — pengujian memang sengaja membuat stub dengan argumen yang tidak dipakai.

Konvensi yang dipakai:

- State machine murni di `workflow.py`, terpisah dari basis data dan web framework.
- SQL ditulis dengan placeholder `?`; `db.py` menerjemahkannya untuk PostgreSQL, sehingga satu kueri jalan di dua mesin.
- Field form dideklarasikan opsional (`Form("")`) lalu divalidasi sendiri. Alasannya: FastAPI 0.141 menolak string kosong pada `Form(...)` dengan **422** sebelum pesan kesalahan kita sempat ditampilkan.
- Kegagalan dari pihak eksternal (respons provider AI) memakai `raise ... from None` agar traceback internal tidak bocor ke pengguna.

---

## Dokumen analisis

| Berkas | Isi |
|---|---|
| `BPMN_Procurement.pdf` | Diagram swimlane vertikal: 4 role sebagai kolom, alur proses mengalir ke bawah, 2 gateway keputusan, jalur revisi, dan legenda |
| `BPMN_Procurement.drawio` | Sumber diagram yang bisa diedit di [app.diagrams.net](https://app.diagrams.net) |
| `WBS_Procurement.xlsx` | Work Breakdown Structure. Sheet `WBS` berisi modul dan fitur; sheet `Traceability` memetakan tiap langkah BPMN ke kode WBS |

---
