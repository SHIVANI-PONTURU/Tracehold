"""Phase 4 step 10: the load test. N complaints run in parallel against one bank through the handoff API and the MCP
tools, with scripted models (no model money). It records the latency of reads and writes (p50, p95), counts
duplicate nodes and cross-bank leaks, and checks that the liens on every account stay within its balance when
complaints that share accounts are decided at the same time (the overlap rule, earlier report first).

tests/test_load.py runs it on the server against a tool server and mock bank the test starts, with test_memory_*
graphs, so a bank's real memory is untouched; it writes work/reports/evaluation/load_test.json for the Evaluation
page. The plan runs the load through the three sandboxed agents; here the agents' code runs in threads of one
process, which loads the tool server, the handoff API and FalkorDB the same way and spends nothing.
"""
from __future__ import annotations

import copy
import json
import os
import re
import statistics
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from agents import worker
from agents.common import clients, model as M
from agents.common.config import load

ACC = re.compile(r"^([0-9]{1,6}-[0-9A-Z]{6,12}) \|", re.M)
DEC = re.compile(r"^(DEC-[0-9A-Za-z-]+) \|", re.M)
READ_TOOLS = {"recall_context", "trace_funds", "compute_taint", "account_signals", "shared_attribute_cluster",
              "mule_community", "key_accounts"}
LABELS = ("Investigation", "Handoff", "Step", "Claim", "Decision", "Action", "Lien", "EntityRef", "Lesson", "Message")


def _text(messages):
    return "\n".join(m[1] if isinstance(m, tuple) else m.content for m in messages)


def follow(messages):
    return {"calls": [{"account_id": a, "decision": "follow", "reason": "load test"} for a in ACC.findall(_text(messages))]}


def adjudicate(messages):
    return {"accounts": [{"account_id": a, "label": "mule", "proposed_action": "lien",
                          "signals": ["account_age_days", "distinct_payers"], "rationale": "Scripted for the load test.",
                          "lessons_applied": [], "flag_entity_ids": [], "info_request": None}
                         for a in dict.fromkeys(ACC.findall(_text(messages)))]}


def verify(messages):
    return {"verdicts": [{"decision_id": d, "verdict": "approve", "reason": "Scripted for the load test."}
                         for d in dict.fromkeys(DEC.findall(_text(messages)))]}


class Timer:
    def __init__(self):
        self.rows, self._lock = [], threading.Lock()

    def add(self, kind, what, ms):
        with self._lock:
            self.rows.append((kind, what, ms))


class TimedHandoff(clients.HandoffClient):
    """The handoff client, timing every request: GET is a read, POST a write."""

    def __init__(self, base_url, bank_id, timer):
        super().__init__(base_url, bank_id)
        self.timer = timer

    def _post(self, path, body, ok=()):
        t = time.perf_counter()
        try:
            return super()._post(path, body, ok)
        finally:
            self.timer.add("write", f"POST {path}", (time.perf_counter() - t) * 1000)

    def _get(self, path, params=None):
        t = time.perf_counter()
        try:
            return super()._get(path, params)
        finally:
            self.timer.add("read", "GET " + re.sub(r"/(INV|[0-9]+)[^/]*", "/{id}", path), (time.perf_counter() - t) * 1000)


def _stats(ms):
    ms = sorted(ms)
    if not ms:
        return {"n": 0}
    q = statistics.quantiles(ms, n=100, method="inclusive") if len(ms) > 1 else [ms[0]] * 99
    return {"n": len(ms), "p50": round(statistics.median(ms), 1), "p95": round(q[94], 1), "max": round(ms[-1], 1)}


def one_run(cfg, base_url, bank, cid, timer, checkpoint_db):
    """One complaint end to end with scripted models: Tracer, Adjudicator, Verifier, gate (placeholder, no override)."""
    h = TimedHandoff(base_url, bank, timer)
    inv = h.start(cid)["investigation_id"]
    try:
        worker.run_tracer(cfg, h, h.claim("tracer", investigation_id=inv), model=M.FakeModel([follow] * 80))
        worker.run_adjudicator(cfg, h, h.claim("adjudicator", investigation_id=inv), model=M.FakeModel([adjudicate] * 2))
        c = copy.deepcopy(cfg)
        c["verifier"]["checkpoint_db"] = checkpoint_db
        v = worker.Verifier(c, h, model_factory=lambda j: M.FakeModel([verify] * 2))
        job = h.claim("verifier", investigation_id=inv)
        v.start(job)
        h.gate_decide(inv, [], "placeholder")
        verdicts = (h.read(inv, "verdict") or {}).get("payload", {}).get("verdicts", [])
        g = h.gate(inv)
        done = v.graph_mod.apply_gate(c, h, inv, verdicts, {"decisions": g.get("decisions") or [],
                                                           "gate_mode": g.get("gate_mode")}, job["trace_id"])
        return {"complaint_id": cid, "investigation_id": inv, "status": done["status"]}
    except Exception as e:
        return {"complaint_id": cid, "investigation_id": inv, "status": "error", "error": f"{type(e).__name__}: {e}"[:300]}


def run(base_url, tool_ctx, bank="11", n=20, threads=20, tmp_dir=".", other_banks=("20", "12"), out_path=None):
    cfg = load()
    cfg["tool_server_url"] = base_url
    timer = Timer()
    original = clients.ToolClient.call

    def timed_call(self, tool, **args):                  # every MCP tool call, timed as a read or a write
        t = time.perf_counter()
        try:
            return original(self, tool, **args)
        finally:
            timer.add("read" if tool in READ_TOOLS else "write", f"tool {tool}", (time.perf_counter() - t) * 1000)
    complaints = [c["complaint_id"] for c in clients.HandoffClient(base_url, bank).complaints()][:4]
    if not complaints:
        raise RuntimeError(f"bank {bank} has no complaints")
    clients.ToolClient.call = timed_call
    t0 = time.time()
    try:
        with ThreadPoolExecutor(max_workers=threads) as pool:
            runs = list(pool.map(lambda i: one_run(cfg, base_url, bank, complaints[i % len(complaints)], timer,
                                                   os.path.join(str(tmp_dir), f"gate_{i}.sqlite")), range(n)))
    finally:
        clients.ToolClient.call = original
    wall = round(time.time() - t0, 1)

    mem = tool_ctx.memory(bank)
    dup = {lab: int(mem.rows(f"MATCH (x:{lab}) WITH x.id AS id, count(*) AS c WHERE c > 1 RETURN count(*)")[0][0])
           for lab in LABELS}
    dup["same_action_twice"] = int(mem.rows(
        "MATCH (i:Investigation)-[:TOOK]->(a:Action)-[:ON]->(e:EntityRef) WITH i, a.kind AS k, e, count(a) AS c "
        "WHERE c > 1 RETURN count(*)")[0][0])
    dup["two_liens_one_decision"] = int(mem.rows(
        "MATCH (d:Decision)-[:EXECUTED_AS]->(l:Lien) WITH d, count(l) AS c WHERE c > 1 RETURN count(*)")[0][0])
    ids = [r["investigation_id"] for r in runs]
    leaks = {}
    for b in other_banks:
        seen = {i["id"] for i in clients.HandoffClient(base_url, b).investigations()} & set(ids)
        nodes = int(tool_ctx.memory(b).rows("MATCH (x) WHERE x.id IN $ids OR x.investigation_id IN $ids RETURN count(x)",
                                            {"ids": ids})[0][0])
        leaks[b] = len(seen) + nodes

    import httpx2                                         # the overlap rule under concurrency: liens within balance
    liens = [l for l in httpx2.get(f"{tool_ctx.bank_api_url}/liens", timeout=30).json()
             if l["investigation_id"] in set(ids) and l["status"] != "reversed"]
    balance = {}
    h = clients.HandoffClient(base_url, bank)
    for r in runs:
        tr = (h.read(r["investigation_id"], "trace") or {}).get("payload", {})
        for a in tr.get("accounts", []):
            balance[a["account_id"]] = a["balance_paise"]
    held = {}
    for l in liens:
        held[l["account_id"]] = held.get(l["account_id"], 0) + int(l["amount_paise"])
    over = [{"account_id": a, "liens": v, "balance": balance.get(a)} for a, v in held.items()
            if balance.get(a) is not None and v > balance[a]]

    reads = [ms for k, _, ms in timer.rows if k == "read"]
    writes = [ms for k, _, ms in timer.rows if k == "write"]
    routes = {}
    for k, what, ms in timer.rows:
        routes.setdefault((k, what), []).append(ms)
    statuses = {}
    for r in runs:
        statuses[r["status"]] = statuses.get(r["status"], 0) + 1
    res = {"written": time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime()), "bank_id": bank, "complaints": n,
           "threads": threads, "distinct_complaints": complaints, "wall_seconds": wall, "statuses": statuses,
           "latency_ms": {"read": _stats(reads), "write": _stats(writes)},
           "by_route": sorted([{"kind": k, "route": w, **_stats(v)} for (k, w), v in routes.items()],
                              key=lambda x: (x["kind"], -x["n"])),
           "duplicates": {**dup, "total": sum(dup.values())}, "leaks": {**leaks, "total": sum(leaks.values())},
           "liens": len(liens), "liened_accounts": len(held), "liens_over_balance": over,
           "errors": [r for r in runs if r["status"] == "error"]}
    if out_path:
        os.makedirs(os.path.dirname(out_path), exist_ok=True)
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(res, f, indent=1)
    return res
