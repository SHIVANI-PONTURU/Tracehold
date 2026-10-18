"""Phase 1 step 4: the one way the project talks to FalkorDB.

- Opens only named graphs: network, memory_<bank>, scratch_<id>, test_<id>. Anything else is refused,
  so a typo cannot create a stray graph or reach another bank's memory.
- Every query is parameterised. ro_query uses GRAPH.RO_QUERY, so the engine itself refuses writes
  (Phase 2's read tools go through it).
- Retries connection errors only, never query errors. Deleting `network` needs force=True.
- Every query has a timeout. The FalkorDB image's own defaults (FALKORDB_ARGS) stop read queries after
  1 second and cut results at 10,000 rows. Agent tools keep that default; the load checks, snapshot and
  tests set `timeout_ms = LONG_QUERY_MS` on their handle because they scan the whole graph.
- Every query is kept in a small in-memory log (`activity`), tagged with who ran it (CALLER: an agent's tool, an
  agent's handoff call, or the UI), so the UI can show what FalkorDB is doing. Parameters are shown shortened.
"""
import collections
import contextvars
import itertools
import re
import threading
import time

import redis
from falkordb import FalkorDB
from redis.backoff import NoBackoff
from redis.retry import Retry

from data import settings
from data.ibm import log

NAME_RE = re.compile(r"^(network|memory_[A-Za-z0-9_]+|scratch_[A-Za-z0-9_]+|test_[A-Za-z0-9_]+)$")
PROTECTED = {"network"}
RETRYABLE = (redis.exceptions.ConnectionError, redis.exceptions.TimeoutError)
LONG_QUERY_MS = 600_000           # whole-graph checks: up to 10 minutes instead of the server's 1-second default


class GraphNameError(ValueError):
    pass


def check_name(name):
    if not NAME_RE.match(name or ""):
        raise GraphNameError(f"graph name {name!r} is not allowed: use network, memory_<bank>, "
                             f"scratch_<id> or test_<id>")
    return name


def connect():
    return FalkorDB(host=settings.FALKORDB_HOST, port=settings.FALKORDB_PORT, password=settings.FALKORDB_PASSWORD)


RAW_TIMEOUT_S = 1800      # DUMP, RESTORE and SAVE of the full graph take seconds to minutes


def raw_connection():
    """A plain Redis connection that keeps replies as bytes (needed for DUMP and RESTORE).

    redis-py 8 defaults to a 5-second read timeout and retries on timeout, which re-sends a slow SAVE or
    RESTORE over and over. This connection waits long enough and never re-sends a command by itself.
    """
    return redis.Redis(host=settings.FALKORDB_HOST, port=settings.FALKORDB_PORT,
                       password=settings.FALKORDB_PASSWORD, decode_responses=False,
                       socket_timeout=RAW_TIMEOUT_S, socket_connect_timeout=5, retry=Retry(NoBackoff(), 0))


def reachable():
    try:
        return bool(raw_connection().ping())
    except Exception:
        return False


def _text(v):
    return v.decode() if isinstance(v, bytes) else v


def module_version(raw):
    """FalkorDB's module version from MODULE LIST (redis-py returns dicts or flat lists, by version)."""
    for mod in raw.execute_command("MODULE", "LIST"):
        if isinstance(mod, dict):
            fields = {_text(k): v for k, v in mod.items()}
        else:
            fields = {_text(mod[i]): mod[i + 1] for i in range(0, len(mod) - 1, 2)}
        if _text(fields.get("name")) in ("graph", "falkordb"):
            return str(_text(fields.get("ver")))
    return "unknown"


def graph_names(db):
    return [n.decode() if isinstance(n, bytes) else n for n in db.list_graphs()]


def _retry(fn, attempts=3, wait=1.0):
    for i in range(attempts):
        try:
            return fn()
        except RETRYABLE:
            if i == attempts - 1:
                raise
            time.sleep(wait * (2 ** i))


# ---- the query log behind the UI's live FalkorDB view ----------------------------------------------------
CALLER = contextvars.ContextVar("tracehold_graph_caller", default=None)   # {"source", "name", "bank", "investigation_id"}
_LOG = collections.deque(maxlen=2000)          # every query this process ran, oldest first
_TOOL_LOG = collections.deque(maxlen=1000)     # agents' tool queries only, so polling cannot push them out
_TOTALS = {}                                   # graph -> {"queries", "ms", "errors"}
_SEQ = itertools.count(1)
_LOCK = threading.Lock()
STARTED_MS = int(time.time() * 1000)


def _short(v, n=60):
    if isinstance(v, (list, tuple)):
        return f"[{len(v)} items]" if len(v) > 3 else repr(list(v))[:n]
    if isinstance(v, dict):
        return f"{{{len(v)} keys}}"
    s = repr(v)
    return s if len(s) <= n else s[: n - 3] + "..."


def one_line(q, n=240):
    s = " ".join(str(q).split())
    return s if len(s) <= n else s[: n - 3] + "..."


def record(graph, kind, q, params, ms, rows=None, engine_ms=None, ok=True):
    """Keep one query in the log (called by GraphHandle; tests call it directly)."""
    who = CALLER.get() or {}
    entry = {"seq": next(_SEQ), "ts_ms": int(time.time() * 1000), "graph": graph, "kind": kind,
             "query": one_line(q), "params": {k: _short(v) for k, v in list((params or {}).items())[:6]},
             "ms": round(ms, 2), "engine_ms": None if engine_ms is None else round(float(engine_ms), 3),
             "rows": rows, "ok": ok, "source": who.get("source", "other"), "name": who.get("name", ""),
             "bank": who.get("bank", ""), "investigation_id": who.get("investigation_id", ""), "agent": who.get("agent", "")}
    _LOG.append(entry)
    if entry["source"] == "tool":
        _TOOL_LOG.append(entry)
    with _LOCK:
        t = _TOTALS.setdefault(graph, {"queries": 0, "ms": 0.0, "errors": 0})
        t["queries"] += 1
        t["ms"] = round(t["ms"] + ms, 2)
        t["errors"] += 0 if ok else 1
    return entry


def activity(since_seq=0, limit=200, source=None, investigation_id=None):
    """The newest logged queries after `since_seq` (oldest first), optionally one source or one investigation,
    with totals per graph since this process started."""
    pool = list(_TOOL_LOG if source == "tool" else _LOG)
    rows = [e for e in pool if e["seq"] > int(since_seq or 0) and (not source or e["source"] == source)
            and (not investigation_id or e["investigation_id"] == investigation_id)]
    with _LOCK:
        totals = {g: dict(v) for g, v in _TOTALS.items()}
    return {"entries": rows[-max(1, min(int(limit), 1000)):], "last_seq": _LOG[-1]["seq"] if _LOG else 0,
            "totals": totals, "started_ms": STARTED_MS}


class GraphHandle:
    def __init__(self, db, name, timeout_ms=None):
        self.db = db
        self.name = check_name(name)
        self.g = db.select_graph(name)
        self.timeout_ms = timeout_ms      # None: the server's default (1 s for reads on the FalkorDB image)

    def exists(self):
        return self.name in graph_names(self.db)

    def _logged(self, kind, q, params, run):
        t0, res, ok = time.perf_counter(), None, False
        try:
            res = run()
            ok = True
            return res
        finally:
            record(self.name, kind, q, params, (time.perf_counter() - t0) * 1000,
                   rows=len(res.result_set) if ok and getattr(res, "result_set", None) is not None else None,
                   engine_ms=getattr(res, "run_time_ms", None) if ok else None, ok=ok)

    def query(self, q, params=None, timeout_ms=None):
        t = timeout_ms if timeout_ms is not None else self.timeout_ms
        return self._logged("read-write", q, params, lambda: _retry(lambda: self.g.query(q, params, timeout=t)))

    def ro_query(self, q, params=None, timeout_ms=None):
        t = timeout_ms if timeout_ms is not None else self.timeout_ms
        return self._logged("read", q, params, lambda: _retry(lambda: self.g.ro_query(q, params, timeout=t)))

    def rows(self, q, params=None, read_only=True, timeout_ms=None):
        res = self.ro_query(q, params, timeout_ms) if read_only else self.query(q, params, timeout_ms)
        return res.result_set

    def value(self, q, params=None, timeout_ms=None):
        rows = self.rows(q, params, timeout_ms=timeout_ms)
        return rows[0][0] if rows else None

    def write_batches(self, q, frames, batch_size, label, total=None):
        """Run `UNWIND $rows AS r ...` over one DataFrame or an iterator of DataFrames, in batches.

        Every row must create exactly one node or one relationship; a batch that creates fewer
        (for example a MATCH that found no node) stops the load. Returns timings and counts.
        """
        if hasattr(frames, "iloc"):
            frames = [frames]
        t0, timings, done, created = time.time(), [], 0, 0
        for frame in frames:
            for start in range(0, len(frame), batch_size):
                rows = frame.iloc[start:start + batch_size].to_dict("records")
                tb = time.time()
                res = self.query(q, {"rows": rows})
                timings.append(time.time() - tb)
                made = res.nodes_created + res.relationships_created
                created += made
                done += len(rows)
                if made != len(rows):
                    raise RuntimeError(f"{label}: a batch of {len(rows):,} rows created {made:,} elements "
                                       f"(rows {done - len(rows):,}-{done:,}); a referenced node is missing")
                if len(timings) % 25 == 0:
                    rate = done / max(time.time() - t0, 1e-9)
                    of = f"/{total:,}" if total else ""
                    log(f"  {label}: {done:,}{of} ({rate:,.0f} rows/s)")
        secs = time.time() - t0
        log(f"  {label}: {done:,} in {secs:,.0f} s")
        return {"rows": done, "created": created, "batches": len(timings), "seconds": round(secs, 1),
                "slowest_batch_s": round(max(timings), 2) if timings else 0.0}

    def delete(self, force=False):
        if self.name in PROTECTED and not force:
            raise PermissionError(f"refusing to delete {self.name!r} without force=True")
        if self.exists():
            self.g.delete()


def open_graph(db, name, timeout_ms=None):
    return GraphHandle(db, name, timeout_ms)


def server_limits(raw):
    """FalkorDB's query limits as configured on this server (the image sets some through FALKORDB_ARGS)."""
    out = {}
    for key in ("TIMEOUT", "TIMEOUT_DEFAULT", "TIMEOUT_MAX", "RESULTSET_SIZE", "MAX_QUEUED_QUERIES"):
        try:
            v = raw.execute_command("GRAPH.CONFIG", "GET", key)
            out[key] = int(v[1]) if isinstance(v, (list, tuple)) else v
        except Exception as e:
            out[key] = f"unknown ({e})"
    return out
