"""The typed tools (eleven on MCP, plus execute_lien for the gate and undo_action for the UI) (implementation plan, Appendix E), as plain functions the MCP server wraps.

Every tool takes bank_id and investigation_id and returns either its data or {"error": {"code", "message"}},
which the agent records rather than ignores. Read tools only read: the network graph through GRAPH.RO_QUERY,
and the bank's memory graph through read-only calls. Act tools write only through this deterministic code;
no amount ever comes from a model. execute_lien is not served to any agent: only the gate code calls it.
"""
from __future__ import annotations

import statistics
import time
from dataclasses import dataclass, field

from graph import client, memory_graph
from mcp_server import network
from memory import store
from taint import engine

ALGO_TIMEOUT_MS = 60_000
SAMPLING_SEED = 42
MAX_TRACE_ACCOUNTS = 400            # compute_taint: tracked accounts per call
MAX_TRACE_DEPTH = 3                 # trace_funds: hops per call
TOP_PER_ACCOUNT = 25                # trace_funds: largest later outflows listed per account

# The named signals in an account's signal record (mcp_server/signals.py, so the agents need not import this file).
from mcp_server.signals import SIGNALS  # noqa: E402,F401


class ToolError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code, self.message = code, message


@dataclass
class Ctx:
    """Connections shared by every tool call in one server process."""
    db: object = None
    raw: object = None
    bank_api_url: str = "http://127.0.0.1:8100"
    _net: object = field(default=None, repr=False)

    def __post_init__(self):
        if self.db is None:
            self.db, self.raw = client.connect(), client.raw_connection()
        self._net = client.open_graph(self.db, "network", timeout_ms=network.TOOL_TIMEOUT_MS)

    @property
    def net(self):
        return self._net

    def memory(self, bank_id):
        try:
            return memory_graph.open_memory(self.db, self.raw, bank_id)
        except ValueError as e:
            raise ToolError("bad_input", str(e))


def run_tool(fn, *args, **kwargs):
    """Call a tool; turn every failure into {"error": {code, message}} instead of an exception. The graph queries
    it runs are logged under the tool's name and investigation (graph/client.py, activity)."""
    token = client.CALLER.set({"source": "tool", "name": fn.__name__,
                               "bank": str(args[1]) if len(args) > 1 else "",
                               "investigation_id": str(args[2]) if len(args) > 2 and args[2] else ""})
    try:
        return fn(*args, **kwargs)
    except ToolError as e:
        return {"error": {"code": e.code, "message": e.message}}
    except network.IncompleteRead as e:
        return {"error": {"code": "incomplete_read", "message": str(e)}}
    except engine.TaintError as e:
        return {"error": {"code": "taint_error", "message": str(e)}}
    except Exception as e:                                        # the agent records it and moves on
        msg = str(e)
        code = "timeout" if "timed out" in msg.lower() else "internal"
        return {"error": {"code": code, "message": f"{type(e).__name__}: {msg[:300]}"}}
    finally:
        client.CALLER.reset(token)


def investigation(ctx, bank_id, investigation_id):
    """The investigation and its complaint, checked to belong to this bank."""
    mem = ctx.memory(bank_id)
    inv = store.get_investigation(mem, investigation_id)
    if not inv:
        raise ToolError("not_found", f"no investigation {investigation_id} for bank {bank_id}")
    comp = network.complaint(ctx.net, inv["complaint_id"])
    if not comp:
        raise ToolError("not_found", f"complaint {inv['complaint_id']} is not in the network graph")
    return mem, inv, comp


def _ids(values, what, limit):
    vals = list(dict.fromkeys(str(v).strip() for v in (values or [])))
    if not vals:
        raise ToolError("bad_input", f"give at least one {what}")
    if len(vals) > limit:
        raise ToolError("too_large", f"at most {limit} {what}s per call; got {len(vals)}")
    return vals


def _known(info, ids):
    missing = [a for a in ids if a not in info]
    if missing:
        raise ToolError("not_found", f"unknown account IDs: {missing[:10]}")


# ---- read tools ------------------------------------------------------------------------------
def _lesson_view(l):
    return {"id": l["id"], "text": l["text"], "stance": l["stance"], "label": l.get("label", ""),
            "confidence": l["confidence"], "times_confirmed": l.get("times_confirmed", 0),
            "times_contradicted": l.get("times_contradicted", 0), **({"matched": l["matched"]} if "matched" in l else {})}


def recall_context(ctx, bank_id, investigation_id, account_ids=None, device_ids=None, phone_ids=None,
                   ip_ids=None):
    """Earlier investigations in this bank's memory touching these entities, directly or through a device, phone
    or IP that one of the given accounts uses; their decisions (with the gate result), their actions, the active
    lessons that apply to these entities, and the bank's most trusted active lessons."""
    mem, inv, _ = investigation(ctx, bank_id, investigation_id)
    accounts = list(dict.fromkeys(str(a).strip() for a in (account_ids or [])))
    others = list(dict.fromkeys(str(x).strip() for x in (device_ids or []) + (phone_ids or []) + (ip_ids or [])))
    if len(accounts) + len(others) > 200:
        raise ToolError("too_large", "at most 200 entity IDs per call")
    via = {}                                                   # entity looked up -> [(given account, how)]
    for a in accounts:
        via.setdefault(a, []).append((a, "account"))
    for a, ids in (network.identifiers(ctx.net, accounts) if accounts else {}).items():
        for kind, e in ids:
            via.setdefault(e, []).append((a, kind))
    for e in others:
        via.setdefault(e, []).append((e, "given"))
    rows = mem.rows("UNWIND $ids AS eid MATCH (e:EntityRef {id: eid}) WITH e "
                    "MATCH (i:Investigation)-[x:INVOLVES]->(e) WHERE i.id <> $inv "
                    "RETURN e.id, e.kind, i.id, i.complaint_id, i.status, i.started_at, x.role, x.via "
                    "ORDER BY i.started_at, i.id, e.id", {"ids": list(via), "inv": investigation_id})
    cases, matches, earlier_accounts = {}, [], set()
    for eid, ekind, iid, cid, status, started, role, xvia in rows:
        cases[iid] = {"investigation_id": iid, "complaint_id": cid, "status": status, "started_at": started}
        how = [{"account": a, "via": k} for a, k in via[eid]]          # via: account (itself), device, phone, ip, given
        earlier = xvia or (eid if ekind == "account" else "")
        if earlier:
            earlier_accounts.add(earlier)
        matches.append({"entity_id": eid, "entity_kind": ekind, "investigation_id": iid, "complaint_id": cid,
                        "role_then": role or "", "earlier_account": earlier, "how": how[:5]})
    decisions, acts = [], []
    if cases:
        accs = sorted(earlier_accounts | {e for e in via if (via[e][0][1] == "account")})
        for d, iid, acc in mem.rows("MATCH (i:Investigation)-[:HAS_DECISION]->(d:Decision)-[:ABOUT]->(e:EntityRef) "
                                    "WHERE i.id IN $cases AND e.id IN $accs RETURN d, i.id, e.id ORDER BY d.ts",
                                    {"cases": list(cases), "accs": accs}):
            p = dict(d.properties)
            decisions.append({"investigation_id": iid, "decision_id": p["id"], "account_id": acc, "label": p.get("label"),
                              "proposed_action": p.get("proposed_action"), "lien_amount_paise": p.get("lien_amount"),
                              "approved": p.get("approved"), "decided_by": p.get("decided_by"),
                              "reason": p.get("gate_reason") or p.get("reason")})
        for a, iid, eid in mem.rows("MATCH (i:Investigation)-[:TOOK]->(a:Action)-[:ON]->(e:EntityRef) "
                                    "WHERE i.id IN $cases AND (e.id IN $accs OR e.id IN $look) AND a.status <> 'undone' "
                                    "RETURN a, i.id, e.id ORDER BY a.ts", {"cases": list(cases), "accs": accs,
                                                                           "look": list(via)}):
            p = dict(a.properties)
            acts.append({"investigation_id": iid, "action_id": p["id"], "kind": p.get("kind"), "entity_id": eid,
                         "status": p.get("status")})
    lessons = []
    for l in store.lessons_touching(mem, list(via)):
        v = _lesson_view(l)
        v["for_accounts"] = sorted({a for e in l["matched"] for a, k in via.get(e, []) if k != "given"})
        lessons.append(v)
    bank = [_lesson_view(l) for l in store.active_lessons(mem, 10)]
    return {"cases": list(cases.values())[:20], "matches": matches[:100], "decisions": decisions[:50],
            "actions": acts[:50], "lessons": lessons[:20], "bank_lessons": bank, "looked_up": len(via)}


def trace_funds(ctx, bank_id, investigation_id, start_account_ids, after_ts, depth=1):
    """Next-layer accounts and the transfers reaching them, one time-respecting hop at a time.

    From each start account, only transfers at or after `after_ts` count; each further hop only counts
    transfers at or after the transfer that reached the account (so money never flows back in time).
    """
    _, _, comp = investigation(ctx, bank_id, investigation_id)
    starts = _ids(start_account_ids, "start account ID", 100)
    depth = int(depth)
    if not 1 <= depth <= MAX_TRACE_DEPTH:
        raise ToolError("bad_input", f"depth must be 1 to {MAX_TRACE_DEPTH}")
    _known(network.accounts(ctx.net, starts), starts)
    frontier = [{"id": a, "after": int(after_ts)} for a in starts]
    hops, seen = [], set(starts)
    for hop in range(1, depth + 1):
        found = network.later_outflows(ctx.net, frontier, TOP_PER_ACCOUNT)
        layer, nxt = [], {}
        for s in frontier:
            f = found.get(s["id"], {"count": 0, "total_paise": 0, "transfers": []})
            layer.append({"from_account_id": s["id"], "after_ts": s["after"], "later_outflows": f["count"],
                          "later_outflow_total_paise": f["total_paise"], "listed": f["transfers"],
                          "truncated": f["count"] > len(f["transfers"])})
            for t in f["transfers"]:
                b = t["to_account_id"]
                if b == comp["victim_account_id"]:
                    continue
                if b not in nxt or t["ts"] < nxt[b]:
                    nxt[b] = t["ts"]
        hops.append({"hop": hop, "accounts": layer})
        frontier = [{"id": b, "after": ts} for b, ts in sorted(nxt.items()) if b not in seen][:100]
        seen.update(b["id"] for b in frontier)
        if not frontier:
            break
    return {"hops": hops, "note": "amounts in paise; listed = the largest later outflows per account"}


def _taint_for(ctx, comp, account_ids):
    fraud = network.transfers_by_id(ctx.net, comp["fraud_transfer_ids"])
    if len(fraud) != len(comp["fraud_transfer_ids"]):
        raise ToolError("not_found", "a fraud transfer of this complaint is missing from the graph")
    tracked = [a for a in account_ids if a != comp["victim_account_id"]]
    info = network.accounts(ctx.net, tracked) if tracked else {}
    _known(info, tracked)
    hist = network.history(ctx.net, info) if info else {}
    opening = {a: int(info[a]["opening_balance"]) for a in tracked}
    transfers = [t for a in tracked for t in hist[a]]
    return engine.compute(fraud, opening, transfers), info


def compute_taint(ctx, bank_id, investigation_id, account_ids):
    """Tainted amount, balance now and tainted share per account, plus the transfer IDs that carried taint.

    Runs the Appendix D engine over each account's whole history. Taint only flows through the accounts the
    engine is given, so while the Tracer builds the trail the given accounts are the trail so far; accounts reached
    but not listed appear under `not_followed` with the taint they received (the next layer to consider). Once the
    investigation has a trace handoff, the engine always runs over the whole traced trail plus the given accounts,
    so a check on one deep account (the Verifier's) sees the money that reached it; empty then means every traced
    account.
    """
    mem, _, comp = investigation(ctx, bank_id, investigation_id)
    ids = list(dict.fromkeys(str(a).strip() for a in (account_ids or [])))    # empty: just the first layer
    trace = store.latest_handoff(mem, investigation_id, "trace")
    traced = [a["account_id"] for a in trace["payload"]["accounts"]] if trace else []
    over = list(dict.fromkeys(traced + ids))
    if len(over) > MAX_TRACE_ACCOUNTS:
        raise ToolError("too_large", f"at most {MAX_TRACE_ACCOUNTS} account IDs per call; got {len(over)}")
    res, _ = _taint_for(ctx, comp, over)
    accounts = []
    for a in ids or traced:
        if a == comp["victim_account_id"]:
            continue
        t = res.accounts[a]
        accounts.append({"account_id": a, "layer": t.layer, "tainted_paise": t.tainted, "balance_paise": t.balance,
                         "tainted_share": round(t.tainted_share, 6), "lien_amount_paise": t.lien_amount,
                         "tainted_in_paise": t.tainted_in, "tainted_out_paise": t.tainted_out,
                         "unrecoverable_out_paise": t.unrecoverable_out, "first_tainted_ts": t.first_tainted_ts,
                         "from_account_ids": sorted({c.src for c in res.carried if c.dst == a})[:20],
                         "tainted_in_transfer_ids": [c.transfer_id for c in res.carried if c.dst == a][:50],
                         "transfer_ids": res.transfers_used(a)[:50]})
    nf = sorted(res.frontier.items(), key=lambda kv: (-kv[1], kv[0]))
    return {"disputed_paise": res.disputed, "accounts": accounts,
            "not_followed": [{"account_id": a, "tainted_paise": v, "layer": res.frontier_layer[a],
                              "first_tainted_ts": res.frontier_first_ts[a],
                              "from_account_ids": sorted(res.frontier_from[a])[:20]} for a, v in nf[:100]],
            "not_followed_count": len(nf), "not_followed_total_paise": sum(res.frontier.values()),
            "unrecoverable_paise": res.unrecoverable, "conservation_ok": res.conservation_ok(),
            "victim_account_id": comp["victim_account_id"],
            "computed_over": "the traced trail plus the accounts given" if traced else "the accounts given"}


def _signal_record(ctx, mem, comp, inv_id, account_id):
    info = network.accounts(ctx.net, [account_id])
    _known(info, [account_id])
    acc = info[account_id]
    hist = network.history(ctx.net, info)[account_id]
    moving = [t for t in hist if not t.self_loop]
    ins = [t for t in moving if t.dst == account_id]
    outs = [t for t in moving if t.src == account_id]
    in_v, out_v = sum(t.amount for t in ins), sum(t.amount for t in outs)
    waits, j = [], 0
    out_ts = sorted(t.ts for t in outs)
    for t in sorted(ins, key=lambda x: x.ts):
        while j < len(out_ts) and out_ts[j] < t.ts:
            j += 1
        if j < len(out_ts):
            waits.append((out_ts[j] - t.ts) / 60)
    fraud_ts = min(t.ts for t in network.transfers_by_id(ctx.net, comp["fraud_transfer_ids"]))
    sh = network.shared(ctx.net, account_id)
    known_mules = store.approved_mules(mem)
    dev_accounts, phone_accounts = {}, {}
    for d, b in sh["device"]:
        dev_accounts.setdefault(d, set()).add(b)
    for p, b in sh["phone"]:
        phone_accounts.setdefault(p, set()).add(b)
    own = [e for _, e in network.identifiers(ctx.net, [account_id])[account_id]]
    related = mem.rows("UNWIND $ids AS eid MATCH (e:EntityRef {id: eid}) WITH e MATCH (i:Investigation)-[:INVOLVES]->(e) "
                       "WHERE i.id <> $inv RETURN count(DISTINCT i)", {"ids": [account_id] + own, "inv": inv_id})
    trace = store.latest_handoff(mem, inv_id, "trace")
    share = None
    if trace:
        for a in trace["payload"]["accounts"]:
            if a["account_id"] == account_id and a["balance_paise"]:
                share = round(a["tainted_paise"] / a["balance_paise"], 6)
    prior = store.prior_decisions(mem, account_id)
    return {
        "account_id": account_id, "bank_id": acc["bank_id"],
        "pass_through_ratio": round(out_v / in_v, 4) if in_v else None,
        "median_minutes_to_forward": round(statistics.median(waits), 1) if waits else None,
        "distinct_payers": len({t.src for t in ins}), "distinct_payees": len({t.dst for t in outs}),
        "account_age_days": round((fraud_ts - int(acc["opened_at"])) / 86400, 1),
        "registered_merchant": bool(acc["registered_merchant"]), "owner_kind": acc["owner_kind"],
        "inflow_count": len(ins), "outflow_count": len(outs), "tainted_share": share,
        "accounts_sharing_device": len({b for _, b in sh["device"]}),
        "accounts_sharing_phone": len({b for _, b in sh["phone"]}),
        "accounts_sharing_ip": len({b for _, b in sh["ip"]}),
        "devices_shared_with_known_mules": sum(1 for bs in dev_accounts.values() if bs & known_mules),
        "phones_shared_with_known_mules": sum(1 for bs in phone_accounts.values() if bs & known_mules),
        "related_cases": int(related[0][0]) if related else 0,
        "prior_decisions": [{k: p[k] for k in ("investigation_id", "label", "approved", "reason")} for p in prior],
    }


def account_signals(ctx, bank_id, investigation_id, account_id):
    """The account's signal record: the named signals the Adjudicator may cite (SIGNALS)."""
    mem, inv, comp = investigation(ctx, bank_id, investigation_id)
    return _signal_record(ctx, mem, comp, investigation_id, str(account_id).strip())


def shared_attribute_cluster(ctx, bank_id, investigation_id, account_id):
    """Accounts sharing a device, a phone (through their owners) or an IP with this account."""
    investigation(ctx, bank_id, investigation_id)
    account_id = str(account_id).strip()
    _known(network.accounts(ctx.net, [account_id]), [account_id])
    sh = network.shared(ctx.net, account_id)
    out = {}
    for kind, pairs in sh.items():
        groups = {}
        for ent, b in pairs:
            groups.setdefault(ent, []).append(b)
        out[kind] = [{f"{kind}_id": e, "accounts": sorted(bs)} for e, bs in sorted(groups.items())]
    out["truncated"] = any(len(p) >= 200 for p in sh.values())
    return out


# ---- scratch graphs for the algorithms ---------------------------------------------------------
def _scratch_name(investigation_id, tool):
    return f"scratch_{investigation_id.replace('-', '_')}_{tool}_{int(time.time() * 1000) % 10**8}"


def _build_scratch(ctx, name, account_ids):
    """Copy the investigation's accounts into a scratch graph, with two kinds of link between them:
    TRANSFERRED (money moved a -> b; one edge per pair, with count and total) and SHARES (both directions; the
    two accounts use the same device, their owners share a phone, or they logged in from the same IP).
    Shared identifiers become direct account links so label propagation can see a ring."""
    g = client.open_graph(ctx.db, name, timeout_ms=ALGO_TIMEOUT_MS)
    ids = sorted(set(account_ids))
    tx = ctx.net.rows("UNWIND $ids AS id MATCH (a:Account {id: id}) WITH a MATCH (a)-[t:TRANSFERRED]->(b:Account) "
                      "WITH a, t, b WHERE b.id IN $ids AND t.self_loop = false "
                      "RETURN a.id, b.id, count(t), sum(t.amount)", {"ids": ids}, timeout_ms=ALGO_TIMEOUT_MS)
    shared_by = {}
    for kind, q in (("device", "MATCH (a)-[:USED_DEVICE]->(e:Device)"),
                    ("phone", "MATCH (a)<-[:OWNS]-(:Customer)-[:HAS_PHONE]->(e:Phone)"),
                    ("ip", "MATCH (a)-[:LOGGED_IN_FROM]->(e:IP)")):
        rows = ctx.net.rows(f"UNWIND $ids AS id MATCH (a:Account {{id: id}}) WITH a {q} RETURN a.id, e.id",
                            {"ids": ids}, timeout_ms=ALGO_TIMEOUT_MS)
        for a, e in rows:
            shared_by.setdefault((kind, e), set()).add(a)
    pairs = {}
    for (kind, e), accs in shared_by.items():
        accs = sorted(accs)
        for i, a in enumerate(accs):
            for b in accs[i + 1:]:
                pairs.setdefault((a, b), set()).add(kind)
    g.query("UNWIND $ids AS id CREATE (:Account {id: id})", {"ids": ids})
    g.query("CREATE INDEX FOR (n:Account) ON (n.id)")
    if tx:
        g.query("UNWIND $rows AS r MATCH (a:Account {id: r[0]}), (b:Account {id: r[1]}) "
                "CREATE (a)-[:TRANSFERRED {n: r[2], amount: r[3]}]->(b)", {"rows": [list(r) for r in tx]})
    if pairs:
        rows = [[a, b, sorted(k)] for (a, b), k in pairs.items()] + [[b, a, sorted(k)] for (a, b), k in pairs.items()]
        g.query("UNWIND $rows AS r MATCH (a:Account {id: r[0]}), (b:Account {id: r[1]}) "
                "CREATE (a)-[:SHARES {via: r[2]}]->(b)", {"rows": rows})
    return g, len(tx), len(pairs)


def _communities(g):
    """Communities: label propagation over SHARES links (accounts tied by shared devices, phones or IPs).
    Components: WCC over SHARES and TRANSFERRED. Both are named after their smallest account ID."""
    lp = g.rows("CALL algo.labelPropagation({nodeLabels: ['Account'], relationshipTypes: ['SHARES'], "
                "maxIterations: 20}) YIELD node, communityId RETURN node.id, communityId")
    wcc = g.rows("CALL algo.WCC({nodeLabels: ['Account'], relationshipTypes: ['SHARES', 'TRANSFERRED']}) "
                 "YIELD node, componentId RETURN node.id, componentId")

    def name(rows):
        members = {}
        for nid, cid in rows:
            members.setdefault(cid, []).append(nid)
        out = {}
        for ms in members.values():
            ms = sorted(ms)
            out.update({a: {"id": f"C-{ms[0]}", "size": len(ms)} for a in ms})
        return out
    return name(lp), name(wcc)


def _trace_accounts(mem, investigation_id):
    t = store.latest_handoff(mem, investigation_id, "trace")
    if not t:
        raise ToolError("not_ready", "no trace handoff yet for this investigation")
    return [a["account_id"] for a in t["payload"]["accounts"]]


def mule_community(ctx, bank_id, investigation_id, account_ids):
    """Community per account (algo.labelPropagation over shared devices, phones and IPs) and connected
    component (algo.WCC, adding the transfers between them), on a scratch graph that is deleted afterwards."""
    investigation(ctx, bank_id, investigation_id)
    ids = _ids(account_ids, "account ID", MAX_TRACE_ACCOUNTS)
    _known(network.accounts(ctx.net, ids), ids)
    name = _scratch_name(investigation_id, "community")
    g = None
    try:
        g, n_tx, n_shared = _build_scratch(ctx, name, ids)
        lp, wcc = _communities(g)
    finally:
        if g is not None:
            g.delete()
    return {"accounts": [{"account_id": a, "community_id": lp[a]["id"], "community_size": lp[a]["size"],
                          "component_id": wcc[a]["id"], "component_size": wcc[a]["size"]} for a in ids],
            "scratch": {"transfer_links": n_tx, "shared_identifier_links": n_shared}}


def key_accounts(ctx, bank_id, investigation_id, community_id):
    """Accounts of one community ranked by algo.betweenness (fixed samplingSeed, samplingSize >= its size).

    The community is recomputed from the investigation's trace handoff, exactly as mule_community does.
    """
    mem, _, _ = investigation(ctx, bank_id, investigation_id)
    ids = _trace_accounts(mem, investigation_id)
    g = None
    try:
        g, *_ = _build_scratch(ctx, _scratch_name(investigation_id, "community"), ids)
        lp, _ = _communities(g)
    finally:
        if g is not None:
            g.delete()
    members = sorted(a for a, c in lp.items() if c["id"] == community_id)
    if not members:
        raise ToolError("not_found", f"no community {community_id} among this investigation's accounts")
    g = None
    try:
        g, *_ = _build_scratch(ctx, _scratch_name(investigation_id, "key"), members)
        n_nodes = g.value("MATCH (n) RETURN count(n)")
        rows = g.rows(f"CALL algo.betweenness({{nodeLabels: ['Account'], relationshipTypes: ['SHARES', 'TRANSFERRED'], "
                      f"samplingSize: {max(int(n_nodes), 1)}, samplingSeed: {SAMPLING_SEED}}}) "
                      f"YIELD node, score RETURN node.id, score ORDER BY score DESC, node.id")
    finally:
        if g is not None:
            g.delete()
    return {"community_id": community_id, "size": len(members),
            "ranking": [{"account_id": a, "score": round(float(s), 4)} for a, s in rows]}


# ---- act tools --------------------------------------------------------------------------------
def _bank_post(ctx, path, body):
    import httpx2
    r = httpx2.post(f"{ctx.bank_api_url}{path}", json=body, timeout=10)
    if r.status_code >= 400:
        raise ToolError("bank_api", f"{path} answered {r.status_code}: {r.text[:200]}")
    return r.json()


def _bank_get(ctx, path, params):
    import httpx2
    r = httpx2.get(f"{ctx.bank_api_url}{path}", params=params, timeout=10)
    if r.status_code >= 400:
        raise ToolError("bank_api", f"{path} answered {r.status_code}: {r.text[:200]}")
    return r.json()


def held_by_others(ctx, investigation_id, account_id):
    """What liens and lien requests for other investigations already hold on the account (the mock bank keeps
    every bank's). A reversed lien holds nothing."""
    rows = _bank_get(ctx, "/liens", {"account_id": account_id})
    return sum(int(r["amount_paise"]) for r in rows
               if r["status"] != "reversed" and r["investigation_id"] != investigation_id)


def lien_cap(ctx, investigation_id, account_id, t):
    """The lien amount under the overlap rule, earlier report first (Phase 4 step 3, the user's pick on 17 Oct):
    the remaining tainted amount, capped by the balance less what earlier complaints' liens already hold on the
    account. Complaints are investigated as they are reported, so an earlier report's lien is already there, and a
    placed lien never changes. t is the account's engine result (engine.AccountTaint)."""
    return max(0, min(t.lien_amount, t.balance - held_by_others(ctx, investigation_id, account_id)))


def open_review(ctx, bank_id, investigation_id, account_id, question):
    """Open a review case for an analyst. Reversible; no money moves. One review per account per investigation:
    asking again (a restarted worker, the retry after a check failure) returns the same case."""
    mem, _, _ = investigation(ctx, bank_id, investigation_id)
    account_id = str(account_id).strip()
    _known(network.accounts(ctx.net, [account_id]), [account_id])
    if not str(question).strip():
        raise ToolError("bad_input", "a review needs a question for the analyst")
    aid = store.write_action(mem, investigation_id, "review", "account", account_id, str(question))
    return {"review_case_id": aid}


def request_info(ctx, bank_id, investigation_id, account_id, what, account_bank_id=None):
    """Ask the account's bank (mock bank API) for information. Reversible. The action ID is the request's
    idempotency key at the bank, so asking again returns the first request instead of sending another."""
    mem, _, _ = investigation(ctx, bank_id, investigation_id)
    account_id = str(account_id).strip()
    info = network.accounts(ctx.net, [account_id])
    _known(info, [account_id])
    to_bank = str(account_bank_id or info[account_id]["bank_id"])
    aid = store.action_id_for(investigation_id, "info_request", account_id)
    r = _bank_post(ctx, "/info-requests", {"from_bank_id": str(bank_id), "to_bank_id": to_bank,
                                           "account_id": account_id, "what": str(what)[:500],
                                           "investigation_id": investigation_id, "request_key": aid})
    store.write_action(mem, investigation_id, "info_request", "account", account_id, str(what),
                       external_id=r["request_id"], action_id=aid)
    return {"request_id": r["request_id"], "action_id": aid, "to_bank_id": to_bank}


def flag_entity(ctx, bank_id, investigation_id, entity_id, reason):
    """Flag a device, phone or IP for this bank. Reversible; one flag per entity per investigation."""
    mem, _, _ = investigation(ctx, bank_id, investigation_id)
    entity_id = str(entity_id).strip()
    kind = network.entity_kind(ctx.net, entity_id)
    if not kind:
        raise ToolError("not_found", f"{entity_id} is not a device, phone or IP in the network graph")
    aid = store.write_action(mem, investigation_id, "flag", kind.lower(), entity_id, str(reason))
    return {"flag_id": aid, "entity_kind": kind.lower()}


def undo_action(ctx, bank_id, investigation_id, action_id, reason):
    """Reverse an automatic action (Phase 3 step 1): withdraw the information request at the bank, close the
    review, or remove the flag; the Action keeps its history with status `undone`. Liens are not automatic actions."""
    mem, _, _ = investigation(ctx, bank_id, investigation_id)
    a = store.get_action(mem, str(action_id))
    if not a or a["investigation_id"] != investigation_id:
        raise ToolError("not_found", f"no action {action_id} in {investigation_id}")
    if a["status"] == "undone":
        return {"action_id": a["id"], "status": "undone", "already_undone": True}
    if not str(reason or "").strip():
        raise ToolError("bad_input", "an undo needs a reason")
    bank = None
    if a["kind"] == "info_request" and a.get("external_id"):
        bank = _bank_post(ctx, f"/info-requests/{a['external_id']}/withdraw", {"reason": str(reason)[:500]})
    store.set_action_status(mem, a["id"], "undone", [a["status"]], undo_reason=str(reason)[:500], undone_at=store.now(),
                            status_before=a["status"])
    return {"action_id": a["id"], "status": "undone", "kind": a["kind"], "bank": bank, "already_undone": False}


def propose_lien(ctx, bank_id, investigation_id, account_id, decision_id, label, rationale):
    """Write a Decision with approved unset and the lien amount calculated in code (never by a model).

    The amount is the account's remaining tainted amount, capped by its balance less the liens earlier complaints
    already hold on it (lien_cap), from the taint engine over the investigation's traced accounts. Idempotent on
    decision_id.
    """
    mem, inv, comp = investigation(ctx, bank_id, investigation_id)
    account_id = str(account_id).strip()
    if account_id == comp["victim_account_id"]:
        raise ToolError("forbidden", "no lien on the victim's account")
    if not str(decision_id).startswith(f"DEC-{investigation_id[4:]}"):
        raise ToolError("bad_input", f"decision IDs for this investigation start with DEC-{investigation_id[4:]}")
    traced = _trace_accounts(mem, investigation_id)
    if account_id not in traced:
        raise ToolError("forbidden", f"{account_id} was not reached by this investigation's trace")
    res, _ = _taint_for(ctx, comp, traced)
    t = res.accounts[account_id]
    if t.lien_amount <= 0:
        raise ToolError("nothing_to_lien", f"{account_id} holds no tainted money")
    amount = lien_cap(ctx, investigation_id, account_id, t)
    if amount <= 0:
        raise ToolError("nothing_to_lien", f"earlier complaints' liens already hold {account_id}'s whole balance")
    d = store.write_decision(mem, investigation_id, decision_id, account_id, label, "lien", amount, rationale)
    return {"decision_id": d["id"], "proposal_id": d["id"], "account_id": account_id,
            "lien_amount_paise": int(d["lien_amount"])}


def execute_lien(ctx, bank_id, investigation_id, decision_id):
    """Place the approved lien (own account) or send a lien request (other bank). Gate code only.

    The guard (safety/guard.py) refuses unless the decision exists, is approved, records who decided, has
    passed the HITL gate and has not already been executed.
    """
    from safety import guard
    mem, inv, comp = investigation(ctx, bank_id, investigation_id)
    d, existing = guard.check_execute(mem, decision_id, investigation_id, comp)
    if existing:
        return {"lien_id": existing["id"], "status": existing["status"], "already_executed": True}
    info = network.accounts(ctx.net, [d["account_id"]])
    own = str(info[d["account_id"]]["bank_id"]) == str(bank_id)
    res, _ = _taint_for(ctx, comp, _trace_accounts(mem, investigation_id))
    balance = res.accounts[d["account_id"]].balance if d["account_id"] in res.accounts else None
    # The bank caps the lien at the balance less other investigations' liens, under its lock (earlier report
    # first), so two complaints approved at the same time cannot hold more than the account has.
    r = _bank_post(ctx, "/liens" if own else "/lien-requests",
                   {"decision_id": decision_id, "investigation_id": investigation_id,
                    "from_bank_id": str(bank_id), "bank_id": str(info[d["account_id"]]["bank_id"]),
                    "account_id": d["account_id"], "amount_paise": int(d["lien_amount"]), "balance_paise": balance})
    lien = store.write_lien(mem, decision_id, r["id"], d["account_id"], int(r["amount_paise"]), r["status"], r["id"])
    return {"lien_id": lien["id"], "status": lien["status"], "own_account": own,
            "amount_paise": int(lien["amount"]), "already_executed": False}


READ_TOOLS = {"recall_context": recall_context, "trace_funds": trace_funds, "compute_taint": compute_taint,
              "account_signals": account_signals, "shared_attribute_cluster": shared_attribute_cluster,
              "mule_community": mule_community, "key_accounts": key_accounts}
ACT_TOOLS = {"open_review": open_review, "request_info": request_info, "flag_entity": flag_entity,
             "propose_lien": propose_lien, "execute_lien": execute_lien}
SCOPES = {
    "tracer": ["recall_context", "trace_funds", "compute_taint"],
    "adjudicator": ["recall_context", "account_signals", "shared_attribute_cluster", "mule_community", "key_accounts",
                    "open_review", "request_info", "flag_entity", "propose_lien"],
    "verifier": ["recall_context", "compute_taint", "account_signals", "shared_attribute_cluster"],
}
