"""The model spend ledger, kept by the tool server so all three agents (in separate sandboxes) share one total.

Every model call is recorded with its tokens and cost in US dollars; agents check the totals before each
call and stop when a run or the whole ledger reaches its cap (agents/config.yaml, budget). SQLite file in
work/ (SPEND_DB to change).
"""
import os
import sqlite3
import threading
import time

from data import settings

DB_PATH = os.environ.get("SPEND_DB") or os.path.join(settings.WORK_DIR, "spend.sqlite")
_lock = threading.Lock()
_con = {}


def _db(path=None):
    path = path or DB_PATH
    if path not in _con:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        con = sqlite3.connect(path, check_same_thread=False)
        con.execute("CREATE TABLE IF NOT EXISTS calls (ts INTEGER, agent TEXT, investigation_id TEXT, model TEXT, "
                    "input_tokens INTEGER, cached_input_tokens INTEGER, output_tokens INTEGER, "
                    "reasoning_tokens INTEGER, usd REAL)")
        con.commit()
        _con[path] = con
    return _con[path]


FIELDS = ("agent", "investigation_id", "model", "input_tokens", "cached_input_tokens", "output_tokens",
          "reasoning_tokens", "usd")


def record(entry, path=None):
    row = [entry.get(k) for k in FIELDS]
    if not isinstance(entry.get("usd"), (int, float)) or entry["usd"] < 0:
        raise ValueError("usd must be a non-negative number")
    with _lock:
        con = _db(path)
        con.execute(f"INSERT INTO calls (ts, {', '.join(FIELDS)}) VALUES (?, {', '.join('?' * len(FIELDS))})",
                    [int(time.time())] + row)
        con.commit()
    return totals(entry.get("investigation_id"), path)


def totals(investigation_id=None, path=None):
    with _lock:
        con = _db(path)
        q = "SELECT count(*), coalesce(sum(usd), 0), coalesce(sum(input_tokens), 0), coalesce(sum(output_tokens), 0) FROM calls"
        all_ = con.execute(q).fetchone()
        run = con.execute(q + " WHERE investigation_id = ?", (investigation_id,)).fetchone() if investigation_id else None
        agents = con.execute("SELECT agent, count(*), coalesce(sum(usd), 0), coalesce(sum(input_tokens), 0), "
                             "coalesce(sum(output_tokens), 0) FROM calls WHERE investigation_id = ? GROUP BY agent "
                             "ORDER BY agent", (investigation_id,)).fetchall() if investigation_id else []
    out = {"total": {"calls": all_[0], "usd": round(all_[1], 6), "input_tokens": all_[2], "output_tokens": all_[3]}}
    if run:
        out["investigation"] = {"calls": run[0], "usd": round(run[1], 6), "input_tokens": run[2], "output_tokens": run[3]}
        out["by_agent"] = {a: {"calls": n, "usd": round(u, 6), "input_tokens": i, "output_tokens": o}
                           for a, n, u, i, o in agents}
    return out


CALL_KEYS = ("id", "ts", "agent", "investigation_id", "model", "input_tokens", "cached_input_tokens", "output_tokens",
             "reasoning_tokens", "usd")


def calls(investigation_id=None, limit=5000, path=None):
    """The ledger's rows, newest first, one per model call (optionally one investigation's)."""
    q, args = f"SELECT rowid, {', '.join(CALL_KEYS[1:])} FROM calls", []
    if investigation_id:
        q, args = q + " WHERE investigation_id = ?", [investigation_id]
    with _lock:
        rows = _db(path).execute(q + " ORDER BY rowid DESC LIMIT ?", args + [max(1, min(int(limit), 20000))]).fetchall()
    return [dict(zip(CALL_KEYS, r)) for r in rows]
