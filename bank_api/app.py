"""Phase 2 step 3: the mock bank API (FastAPI and SQLite) for liens, lien requests, information requests and
reversals. It stands in for each bank's core system; one service plays every bank.

  python -m uvicorn bank_api.app:app --port 8100

Liens are idempotent on decision_id: asking twice for the same decision returns the first lien.
"""
from __future__ import annotations

import os
import sqlite3
import threading
import time
import uuid
from contextlib import closing

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from data import settings

DB_PATH = os.environ.get("BANK_API_DB") or os.path.join(settings.WORK_DIR, "bank_api.sqlite")
_lock = threading.Lock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS liens (
    id TEXT PRIMARY KEY, kind TEXT NOT NULL, decision_id TEXT NOT NULL UNIQUE, investigation_id TEXT NOT NULL,
    from_bank_id TEXT NOT NULL, bank_id TEXT NOT NULL, account_id TEXT NOT NULL, amount_paise INTEGER NOT NULL,
    status TEXT NOT NULL, created_at INTEGER NOT NULL, reversed_at INTEGER, reversal_reason TEXT);
CREATE TABLE IF NOT EXISTS info_requests (
    id TEXT PRIMARY KEY, investigation_id TEXT NOT NULL, from_bank_id TEXT NOT NULL, to_bank_id TEXT NOT NULL,
    account_id TEXT NOT NULL, what TEXT NOT NULL, status TEXT NOT NULL, created_at INTEGER NOT NULL);
"""


INFO_COLUMNS = {"request_key": "TEXT", "withdrawn_at": "INTEGER", "withdraw_reason": "TEXT"}   # added in Phase 3


def connect(path=None):
    path = path or DB_PATH
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    con = sqlite3.connect(path, check_same_thread=False)
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)
    have = {r["name"] for r in con.execute("PRAGMA table_info(info_requests)")}
    for col, typ in INFO_COLUMNS.items():                  # a Phase 2 database gains the new columns in place
        if col not in have:
            con.execute(f"ALTER TABLE info_requests ADD COLUMN {col} {typ}")
    con.execute("CREATE UNIQUE INDEX IF NOT EXISTS info_requests_key ON info_requests(request_key)")
    con.commit()
    return con


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class LienIn(Strict):
    decision_id: str = Field(min_length=5, max_length=80)
    investigation_id: str = Field(min_length=5, max_length=60)
    from_bank_id: str = Field(pattern=r"^[0-9]{1,6}$")
    bank_id: str = Field(pattern=r"^[0-9]{1,6}$")
    account_id: str = Field(pattern=r"^[0-9]{1,6}-[0-9A-Z]{6,12}$")
    amount_paise: int = Field(gt=0, le=10**15)
    balance_paise: int | None = Field(default=None, ge=0, le=10**15)   # Phase 4: cap at the account's balance


class InfoIn(Strict):
    investigation_id: str = Field(min_length=5, max_length=60)
    from_bank_id: str = Field(pattern=r"^[0-9]{1,6}$")
    to_bank_id: str = Field(pattern=r"^[0-9]{1,6}$")
    account_id: str = Field(pattern=r"^[0-9]{1,6}-[0-9A-Z]{6,12}$")
    what: str = Field(min_length=3, max_length=500)
    request_key: str | None = Field(default=None, min_length=5, max_length=80)   # idempotency key (the action ID)


class WithdrawIn(Strict):
    reason: str = Field(min_length=3, max_length=500)


class ReversalIn(Strict):
    lien_id: str = Field(min_length=5, max_length=80)
    reason: str = Field(min_length=3, max_length=500)


def create_app(db_path=None):
    app = FastAPI(title="TraceHold mock bank API")
    con = connect(db_path)

    def lien(kind, body: LienIn):
        if kind == "lien" and body.from_bank_id != body.bank_id:
            raise HTTPException(400, "a lien is placed by the account's own bank; use /lien-requests")
        if kind == "request" and body.from_bank_id == body.bank_id:
            raise HTTPException(400, "a lien request goes to another bank; use /liens")
        with _lock, con:
            row = con.execute("SELECT * FROM liens WHERE decision_id = ?", (body.decision_id,)).fetchone()
            if row:                         # a replay; the first lien may have been capped below the amount asked
                if row["amount_paise"] > body.amount_paise or row["account_id"] != body.account_id:
                    raise HTTPException(409, "this decision already has a lien with a different account or amount")
                return dict(row) | {"idempotent_replay": True}
            amount = body.amount_paise
            if body.balance_paise is not None:      # earlier report first, checked under the lock: what other
                held = con.execute("SELECT coalesce(sum(amount_paise), 0) FROM liens WHERE account_id = ? AND "
                                   "status != 'reversed' AND investigation_id != ?",
                                   (body.account_id, body.investigation_id)).fetchone()[0]
                amount = min(amount, body.balance_paise - held)     # investigations' liens leave of the balance
                if amount <= 0:
                    raise HTTPException(409, "earlier liens already hold the account's whole balance")
            lid = ("LIEN-" if kind == "lien" else "LREQ-") + uuid.uuid4().hex[:12]
            status = "placed" if kind == "lien" else "requested"
            con.execute("INSERT INTO liens (id, kind, decision_id, investigation_id, from_bank_id, bank_id, account_id, "
                        "amount_paise, status, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (lid, kind, body.decision_id, body.investigation_id, body.from_bank_id, body.bank_id,
                         body.account_id, amount, status, int(time.time())))
            return dict(con.execute("SELECT * FROM liens WHERE id = ?", (lid,)).fetchone()) | {"idempotent_replay": False}

    @app.get("/health")
    def health():
        return {"ok": True}

    @app.post("/liens")
    def place_lien(body: LienIn):
        return lien("lien", body)

    @app.post("/lien-requests")
    def request_lien(body: LienIn):
        return lien("request", body)

    @app.get("/liens")
    def list_liens(investigation_id: str | None = None, account_id: str | None = None):
        """Liens and lien requests, optionally for one investigation or one account (the overlap rule reads what
        earlier complaints already hold on an account)."""
        where, args = [], []
        for col, val in (("investigation_id", investigation_id), ("account_id", account_id)):
            if val:
                where.append(f"{col} = ?")
                args.append(val)
        sql = "SELECT * FROM liens" + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY created_at, id"
        with closing(con.cursor()) as cur:
            rows = cur.execute(sql, args).fetchall()
        return [dict(r) for r in rows]

    @app.post("/reversals")
    def reverse(body: ReversalIn):
        with _lock, con:
            row = con.execute("SELECT * FROM liens WHERE id = ?", (body.lien_id,)).fetchone()
            if not row:
                raise HTTPException(404, f"no lien {body.lien_id}")
            if row["status"] == "reversed":
                return dict(row)
            con.execute("UPDATE liens SET status = 'reversed', reversed_at = ?, reversal_reason = ? WHERE id = ?",
                        (int(time.time()), body.reason, body.lien_id))
            return dict(con.execute("SELECT * FROM liens WHERE id = ?", (body.lien_id,)).fetchone())

    @app.post("/info-requests")
    def info_request(body: InfoIn):
        with _lock, con:
            if body.request_key:
                row = con.execute("SELECT * FROM info_requests WHERE request_key = ?", (body.request_key,)).fetchone()
                if row:
                    return {"request_id": row["id"], "status": row["status"], "idempotent_replay": True}
            rid = "INFO-" + uuid.uuid4().hex[:12]
            con.execute("INSERT INTO info_requests (id, investigation_id, from_bank_id, to_bank_id, account_id, what, "
                        "status, created_at, request_key) VALUES (?, ?, ?, ?, ?, ?, 'sent', ?, ?)",
                        (rid, body.investigation_id, body.from_bank_id, body.to_bank_id, body.account_id, body.what,
                         int(time.time()), body.request_key))
        return {"request_id": rid, "status": "sent", "idempotent_replay": False}

    @app.get("/info-requests")
    def list_info_requests(investigation_id: str | None = None):
        with closing(con.cursor()) as cur:
            q = "SELECT * FROM info_requests" + (" WHERE investigation_id = ?" if investigation_id else "")
            rows = cur.execute(q + " ORDER BY created_at, id", (investigation_id,) if investigation_id else ()).fetchall()
        return [dict(r) for r in rows]

    @app.post("/info-requests/{request_id}/withdraw")
    def withdraw(request_id: str, body: WithdrawIn):
        with _lock, con:
            row = con.execute("SELECT * FROM info_requests WHERE id = ?", (request_id,)).fetchone()
            if not row:
                raise HTTPException(404, f"no information request {request_id}")
            if row["status"] != "withdrawn":
                con.execute("UPDATE info_requests SET status = 'withdrawn', withdrawn_at = ?, withdraw_reason = ? "
                            "WHERE id = ?", (int(time.time()), body.reason, request_id))
            return dict(con.execute("SELECT * FROM info_requests WHERE id = ?", (request_id,)).fetchone())

    return app


app = create_app()
