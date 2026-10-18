"""Phase 1 step 3: check the pinned FalkorDB engine does everything later phases rely on.

Builds a toy graph in `test_engine` and checks, each with an expected answer written in advance:
  - algo.WCC, algo.labelPropagation, algo.betweenness, algo.SSpaths exist and give the expected results
  - algo.SSpaths ignores time order (so trace_funds must hop forward in time in Cypher)
  - betweenness gives the same scores twice with a fixed samplingSeed
  - relationship index, unique constraint, array properties, UNWIND over a list of maps,
    GRAPH.RO_QUERY refusing a write, GRAPH.MEMORY USAGE, DUMP and RESTORE, GRAPH.COPY
Writes work/reports/engine_check.json and deletes every test graph it made.

  python -m graph.engine_check
"""
import json
import os
import time

import redis

from data import settings
from data.ibm import log
from graph import client

ALGOS = ["algo.SSpaths", "algo.WCC", "algo.labelPropagation", "algo.betweenness"]
TOY = """
CREATE (a:Account {id:'A'}), (b:Account {id:'B'}), (m:Account {id:'M'}), (c:Account {id:'C'}), (d:Account {id:'D'}),
       (r1:Account {id:'R1'}), (r2:Account {id:'R2'}), (r3:Account {id:'R3'}),
       (x:Account {id:'X'}), (y:Account {id:'Y'}), (z:Account {id:'Z'}),
       (a)-[:TRANSFERRED {ts: 1}]->(m), (b)-[:TRANSFERRED {ts: 2}]->(m),
       (m)-[:TRANSFERRED {ts: 3}]->(c), (m)-[:TRANSFERRED {ts: 4}]->(d),
       (r1)-[:TRANSFERRED {ts: 1}]->(r2), (r2)-[:TRANSFERRED {ts: 2}]->(r3), (r3)-[:TRANSFERRED {ts: 3}]->(r1),
       (x)-[:TRANSFERRED {ts: 10}]->(y), (y)-[:TRANSFERRED {ts: 5}]->(z)
"""
EXPECTED_COMPONENTS = {frozenset("A B M C D".split()), frozenset("R1 R2 R3".split()), frozenset("X Y Z".split())}
FILTER = "nodeLabels: ['Account'], relationshipTypes: ['TRANSFERRED']"


def image_info():
    path = os.path.join(settings.ROOT, "deploy", "falkordb.env")
    info = {}
    if os.path.exists(path):
        for line in open(path, encoding="utf-8"):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                info[k] = v
    return info


def run():
    if not client.reachable():
        raise SystemExit(f"FalkorDB is not reachable at {settings.FALKORDB_HOST}:{settings.FALKORDB_PORT}. "
                         f"Start it with: powershell -File deploy\\falkordb.ps1 start")
    db, raw = client.connect(), client.raw_connection()
    checks = []

    def check(name, ok, detail=""):
        checks.append({"check": name, "result": "PASS" if ok else "FAIL", "detail": str(detail)})
        log(f"  {'PASS' if ok else 'FAIL'}  {name}  {detail}")

    def attempt(name, fn):
        try:
            fn()
        except Exception as e:                       # one broken capability must not hide the others
            check(name, False, f"error: {e}")

    version = client.module_version(raw)
    log(f"FalkorDB module version {version}")
    limits = client.server_limits(raw)
    log(f"server query limits: {limits} (0 = none); long checks pass their own timeout")

    for gname in ("test_engine", "test_engine_restored", "test_engine_copy"):
        client.open_graph(db, gname).delete()
    g = client.open_graph(db, "test_engine")
    g.query(TOY)

    procs = {r[0] for r in g.rows("CALL dbms.procedures() YIELD name RETURN name")}
    for p in ALGOS:
        check(f"{p} is available", p in procs)

    def wcc():
        rows = g.rows(f"CALL algo.WCC({{{FILTER}}}) YIELD node, componentId RETURN node.id, componentId")
        comps = {}
        for nid, cid in rows:
            comps.setdefault(cid, set()).add(nid)
        got = {frozenset(v) for v in comps.values()}
        check("algo.WCC finds the three components", got == EXPECTED_COMPONENTS, sorted(sorted(c) for c in got))
    attempt("algo.WCC finds the three components", wcc)

    def lp():
        rows = g.rows(f"CALL algo.labelPropagation({{{FILTER}, maxIterations: 10}}) YIELD node, communityId "
                      f"RETURN node.id, communityId")
        comm = dict(rows)
        ring_together = len({comm.get("R1"), comm.get("R2"), comm.get("R3")}) == 1
        check("algo.labelPropagation labels every account", len(comm) == 11,
              f"{len(set(comm.values()))} communities; ring in one community: {ring_together}")
    attempt("algo.labelPropagation labels every account", lp)

    def betweenness():
        q = (f"CALL algo.betweenness({{{FILTER}, samplingSize: 100, samplingSeed: 42}}) YIELD node, score "
             f"RETURN node.id, score ORDER BY node.id")
        first, second = g.rows(q), g.rows(q)
        top = max(first, key=lambda r: r[1])[0]
        check("algo.betweenness ranks the hub M highest", top == "M", {k: round(v, 2) for k, v in first})
        check("algo.betweenness repeats exactly with a fixed samplingSeed", first == second)
    attempt("algo.betweenness ranks the hub M highest", betweenness)

    def sspaths():
        rows = g.rows("MATCH (x:Account {id: 'X'}) CALL algo.SSpaths({sourceNode: x, relTypes: ['TRANSFERRED'], "
                      "relDirection: 'outgoing', maxLen: 3, pathCount: 10}) YIELD path "
                      "RETURN [n IN nodes(path) | n.id]")
        paths = [r[0] for r in rows]
        ignores_time = ["X", "Y", "Z"] in paths
        check("algo.SSpaths ignores time order (Y->Z happened before X->Y)", ignores_time, paths)
        timed = g.rows("MATCH (:Account {id: 'X'})-[r1:TRANSFERRED]->(:Account)-[r2:TRANSFERRED]->(z:Account) "
                       "WHERE r2.ts >= r1.ts RETURN z.id")
        check("Time-respecting hops in Cypher exclude that path", timed == [], timed)
    attempt("algo.SSpaths ignores time order (Y->Z happened before X->Y)", sspaths)

    def rel_index():
        g.query("CREATE INDEX FOR ()-[r:TRANSFERRED]-() ON (r.ts)")
        rows = g.rows("CALL db.indexes() YIELD label, properties, entitytype RETURN label, properties, entitytype")
        ok = any(r[0] == "TRANSFERRED" and "ts" in r[1] for r in rows)
        check("Index on a relationship property (TRANSFERRED.ts)", ok, rows)
    attempt("Index on a relationship property (TRANSFERRED.ts)", rel_index)

    def unique():
        g.query("CREATE INDEX FOR (n:Account) ON (n.id)")
        raw.execute_command("GRAPH.CONSTRAINT", "CREATE", "test_engine", "UNIQUE", "NODE", "Account", "PROPERTIES", "1", "id")
        status = None
        for _ in range(30):
            st = g.rows("CALL db.constraints() YIELD label, status RETURN label, status")
            status = dict(st).get("Account")
            if status == "OPERATIONAL":
                break
            time.sleep(0.5)
        try:
            g.query("CREATE (:Account {id: 'A'})")
            refused = False
        except redis.exceptions.ResponseError:
            refused = True
        check("Unique constraint on Account.id refuses a duplicate", status == "OPERATIONAL" and refused,
              f"status {status}, duplicate refused: {refused}")
    attempt("Unique constraint on Account.id refuses a duplicate", unique)

    def arrays():
        g.query("CREATE (:Probe {ids: ['T000000001', 'T000000002'], n: [1, 2, 3]})")
        got = g.rows("MATCH (p:Probe) RETURN p.ids, p.n")
        check("Array properties round-trip", got == [[["T000000001", "T000000002"], [1, 2, 3]]], got)
    attempt("Array properties round-trip", arrays)

    def unwind():
        g.query("UNWIND $rows AS r CREATE (:Probe2 {id: r.id, ok: r.ok, n: r.n})",
                {"rows": [{"id": "p1", "ok": True, "n": 7}, {"id": "p2", "ok": False, "n": 8}]})
        got = g.rows("MATCH (p:Probe2) RETURN p.id, p.ok, p.n ORDER BY p.id")
        check("UNWIND over a list of maps (the loader's batch shape)", got == [["p1", True, 7], ["p2", False, 8]], got)
    attempt("UNWIND over a list of maps (the loader's batch shape)", unwind)

    def ro():
        try:
            g.ro_query("CREATE (:Probe3)")
            refused = False
        except redis.exceptions.ResponseError:
            refused = True
        check("GRAPH.RO_QUERY refuses a write", refused)
    attempt("GRAPH.RO_QUERY refuses a write", ro)

    def memory():
        usage = raw.execute_command("GRAPH.MEMORY", "USAGE", "test_engine")
        check("GRAPH.MEMORY USAGE reports memory", bool(usage), usage)
    attempt("GRAPH.MEMORY USAGE reports memory", memory)

    def dump_restore():
        payload = raw.dump("test_engine")
        raw.restore("test_engine_restored", 0, payload, replace=True)
        n = client.open_graph(db, "test_engine_restored").value("MATCH (n) RETURN count(n)")
        m = g.value("MATCH (n) RETURN count(n)")
        check("DUMP and RESTORE copy a graph", n == m, f"{len(payload):,} bytes, {n} nodes restored of {m}")
        client.open_graph(db, "test_engine_restored").delete()
    attempt("DUMP and RESTORE copy a graph", dump_restore)

    def copy():
        g.g.copy("test_engine_copy")
        n = client.open_graph(db, "test_engine_copy").value("MATCH (n) RETURN count(n)")
        check("GRAPH.COPY copies a graph (Phase 2 scratch graphs)", n == g.value("MATCH (n) RETURN count(n)"), f"{n} nodes")
        client.open_graph(db, "test_engine_copy").delete()
    attempt("GRAPH.COPY copies a graph (Phase 2 scratch graphs)", copy)

    rows = len(g.rows("UNWIND range(1, 20000) AS x RETURN x"))
    limits["rows_returned_of_20000"] = rows
    log(f"a 20,000-row result came back with {rows:,} rows"
        + (": Phase 2 tools must aggregate or page" if rows < 20000 else ""))

    g.delete()
    info = image_info()
    passed = sum(c["result"] == "PASS" for c in checks)
    report = {"image": info.get("FALKORDB_IMAGE", "?"), "digest": info.get("FALKORDB_DIGEST", "?"),
              "module_version": version, "server_limits": limits, "passed": passed, "total": len(checks), "checks": checks}
    settings.ensure_dirs(settings.REPORT_DIR)
    with open(os.path.join(settings.REPORT_DIR, "engine_check.json"), "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2)
    log(f"engine check: {passed} of {len(checks)} pass -> work/reports/engine_check.json")
    return report


if __name__ == "__main__":
    r = run()
    raise SystemExit(0 if r["passed"] == r["total"] else 1)
