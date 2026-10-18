"""Read-only queries on the `network` graph for the tools. Every query goes through GRAPH.RO_QUERY.

Engine limits shape these queries (QUERIES.md): results are cut at 10,000 rows without an error, so
whole-history reads are paged and every page is checked against the expected count. And the planner needs
help: after an UNWIND, look the account up on its own and pass it on with WITH before traversing (otherwise
it walks every TRANSFERRED edge and filters, about 1,000 times slower); look transfers up by their own ID;
and put time filters after a WITH.
"""
from __future__ import annotations

from taint.engine import Transfer

PAGE = 9_000                 # rows per query, safely below the 10,000-row result cap
TOOL_TIMEOUT_MS = 20_000


class IncompleteRead(RuntimeError):
    """A paged read returned fewer rows than the account's degree."""


def complaint(net, complaint_id):
    r = net.rows("MATCH (c:Complaint {id: $id}) RETURN c.victim_account_id, c.fraud_transfer_ids, "
                 "c.disputed_amount, c.reported_at", {"id": complaint_id}, timeout_ms=TOOL_TIMEOUT_MS)
    if not r:
        return None
    v, f, d, rep = r[0]
    return {"complaint_id": complaint_id, "victim_account_id": v, "fraud_transfer_ids": list(f),
            "disputed_paise": int(d), "reported_at": int(rep)}


def transfers_by_id(net, ids):
    """Transfers looked up by their own ID (edge index), in the order asked."""
    rows = []
    for i in range(0, len(ids), PAGE):
        rows += net.rows("UNWIND $ids AS tid MATCH (a:Account)-[t:TRANSFERRED {id: tid}]->(b:Account) "
                         "RETURN t.id, a.id, b.id, t.amount, t.ts, t.format, t.self_loop",
                         {"ids": list(ids[i:i + PAGE])}, timeout_ms=TOOL_TIMEOUT_MS)
    by_id = {r[0]: Transfer(r[0], r[1], r[2], int(r[3]), int(r[4]), r[5], bool(r[6])) for r in rows}
    return [by_id[t] for t in ids if t in by_id]


def accounts(net, ids):
    """Account facts with owner, bank and transfer counts, for the IDs that exist."""
    rows = net.rows("UNWIND $ids AS id MATCH (a:Account {id: id}) WITH a MATCH (c:Customer)-[:OWNS]->(a)-[:HELD_AT]->(b:Bank) "
                    "RETURN a.id, a.bank_id, a.opened_at, a.opening_balance, c.id, c.kind, c.registered_merchant, "
                    "b.name, outdegree(a, 'TRANSFERRED'), indegree(a, 'TRANSFERRED')",
                    {"ids": list(ids)}, timeout_ms=TOOL_TIMEOUT_MS)
    keys = ["account_id", "bank_id", "opened_at", "opening_balance", "owner_id", "owner_kind", "registered_merchant",
            "bank_name", "n_out", "n_in"]
    return {r[0]: dict(zip(keys, r)) for r in rows}


def _edge_rows(net, q, params):
    return net.rows(q, params, timeout_ms=TOOL_TIMEOUT_MS)


def history(net, info):
    """Every transfer into or out of the given accounts (info: accounts() output), paged under the row cap.

    Returns {account_id: [Transfer, ...]} sorted by time then ID. Raises IncompleteRead if a page came back
    short, so a silent truncation can never reach the taint engine.
    """
    out = {a: [] for a in info}
    small = [a for a, i in info.items() if i["n_out"] + i["n_in"] <= PAGE]
    big = [a for a in info if a not in small]
    batch, size = [], 0
    for a in small + [None]:
        n = info[a]["n_out"] + info[a]["n_in"] if a else 0
        if a is None or (batch and size + n > PAGE):
            if batch:
                for direction in ("out", "in"):
                    pat = "(x)-[t:TRANSFERRED]->(y:Account)" if direction == "out" else \
                          "(y:Account)-[t:TRANSFERRED]->(x)"
                    rows = _edge_rows(net, f"UNWIND $ids AS id MATCH (x:Account {{id: id}}) WITH x MATCH {pat} "
                                           "RETURN x.id, t.id, startNode(t).id, endNode(t).id, t.amount, t.ts, "
                                           "t.format, t.self_loop", {"ids": batch})
                    for r in rows:
                        out[r[0]].append(Transfer(r[1], r[2], r[3], int(r[4]), int(r[5]), r[6], bool(r[7])))
            batch, size = [], 0
        if a is not None:
            batch.append(a)
            size += n
    for a in big:
        for direction, n in (("out", info[a]["n_out"]), ("in", info[a]["n_in"])):
            pat = "(x:Account {id: $id})-[t:TRANSFERRED]->(y:Account)" if direction == "out" else \
                  "(y:Account)-[t:TRANSFERRED]->(x:Account {id: $id})"
            for skip in range(0, n, PAGE):
                rows = _edge_rows(net, f"MATCH {pat} RETURN t.id, startNode(t).id, endNode(t).id, t.amount, t.ts, "
                                       f"t.format, t.self_loop ORDER BY t.id SKIP $s LIMIT $n",
                                  {"id": a, "s": skip, "n": PAGE})
                out[a] += [Transfer(r[0], r[1], r[2], int(r[3]), int(r[4]), r[5], bool(r[6])) for r in rows]
    for a, ts in out.items():
        want = info[a]["n_out"] + info[a]["n_in"]
        if len(ts) != want:
            raise IncompleteRead(f"{a}: read {len(ts)} transfers of {want}")
        ts.sort(key=lambda t: (t.ts, t.id))
    return out


def later_outflows(net, starts, top_n):
    """For each {id, after}: transfers out of the account at or after `after` (self-loops excluded).

    One row per account: the count, the total and the `top_n` largest. The time filter sits after a WITH, so
    the planner reads the account's own transfers instead of range-scanning the TRANSFERRED.ts index.
    """
    out = {}
    for i in range(0, len(starts), 500):
        rows = net.rows("UNWIND $starts AS s MATCH (a:Account {id: s.id}) WITH s, a MATCH (a)-[t:TRANSFERRED]->(b:Account) "
                        "WITH s, a, t, b WHERE t.ts >= s.after AND t.self_loop = false "
                        "WITH a.id AS src, t, b ORDER BY t.amount DESC, t.id "
                        "WITH src, count(t) AS n, sum(t.amount) AS total, "
                        "collect([t.id, b.id, t.amount, t.ts, t.format]) AS ts "
                        "RETURN src, n, total, ts[0..$top]",
                        {"starts": starts[i:i + 500], "top": int(top_n)}, timeout_ms=TOOL_TIMEOUT_MS)
        for src, n, total, ts in rows:
            out[src] = {"count": int(n), "total_paise": int(total),
                        "transfers": [{"transfer_id": t[0], "to_account_id": t[1], "amount_paise": int(t[2]),
                                       "ts": int(t[3]), "format": t[4]} for t in ts]}
    return out


def devices_of(net, account_ids):
    rows = net.rows("UNWIND $ids AS id MATCH (a:Account {id: id}) WITH a MATCH (a)-[:USED_DEVICE]->(d:Device) RETURN a.id, d.id",
                    {"ids": list(account_ids)}, timeout_ms=TOOL_TIMEOUT_MS)
    out = {}
    for a, d in rows:
        out.setdefault(a, []).append(d)
    return out


def identifiers(net, account_ids):
    """Each account's devices, phones (through its owner) and IPs: {account_id: [(kind, id), ...]}, sorted."""
    ids = list(dict.fromkeys(account_ids))
    out = {a: [] for a in ids}
    if not ids:
        return out
    for kind, q in (("device", "MATCH (a)-[:USED_DEVICE]->(e:Device)"),
                    ("phone", "MATCH (a)<-[:OWNS]-(:Customer)-[:HAS_PHONE]->(e:Phone)"),
                    ("ip", "MATCH (a)-[:LOGGED_IN_FROM]->(e:IP)")):
        for i in range(0, len(ids), 500):
            rows = net.rows(f"UNWIND $ids AS id MATCH (a:Account {{id: id}}) WITH a {q} RETURN a.id, e.id",
                            {"ids": ids[i:i + 500]}, timeout_ms=TOOL_TIMEOUT_MS)
            for a, e in rows:
                out[a].append((kind, e))
    return {a: sorted(set(v)) for a, v in out.items()}


def shared(net, account_id, cap=200):
    """Accounts sharing a device, a phone (through their owners) or an IP with the account, with what is shared."""
    params = {"id": account_id, "cap": cap}
    dev = net.rows("MATCH (a:Account {id: $id})-[:USED_DEVICE]->(d:Device)<-[:USED_DEVICE]-(b:Account) "
                   "WHERE b.id <> a.id RETURN d.id, b.id LIMIT $cap", params, timeout_ms=TOOL_TIMEOUT_MS)
    ph = net.rows("MATCH (a:Account {id: $id})<-[:OWNS]-(c:Customer)-[:HAS_PHONE]->(p:Phone)<-[:HAS_PHONE]-"
                  "(c2:Customer)-[:OWNS]->(b:Account) WHERE c2.id <> c.id RETURN p.id, b.id LIMIT $cap",
                  params, timeout_ms=TOOL_TIMEOUT_MS)
    ip = net.rows("MATCH (a:Account {id: $id})-[:LOGGED_IN_FROM]->(i:IP)<-[:LOGGED_IN_FROM]-(b:Account) "
                  "WHERE b.id <> a.id RETURN i.id, b.id LIMIT $cap", params, timeout_ms=TOOL_TIMEOUT_MS)
    return {"device": dev, "phone": ph, "ip": ip}


def entity_kind(net, entity_id):
    for label in ("Device", "Phone", "IP"):
        if net.rows(f"MATCH (n:{label} {{id: $id}}) RETURN count(n)", {"id": entity_id},
                    timeout_ms=TOOL_TIMEOUT_MS)[0][0]:
            return label
    return None
