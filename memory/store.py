"""Writers and readers for a bank's memory graph (implementation plan, Appendix B, memory_<bank>).

Every function takes the bank's GraphHandle (graph.memory_graph.open_memory). Writes are single Cypher queries, so
each one is atomic in FalkorDB, and every writer MERGEs on an ID that is deterministic for what it records, so
running it twice leaves one node (Phase 3 step 4): handoffs on their content, steps on (agent run, step number),
claims on (investigation, attempt, account), actions on (investigation, kind, entity), lessons on the decision
that made them, messages on (investigation, number).
"""
from __future__ import annotations

import hashlib
import json
import time
import uuid

from handoff import state as S

STATUSES = S.STATUSES
LEASE_S = 900                      # a claim not renewed for this long is taken back (the worker crashed)


def new_id(prefix):
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


def now():
    return int(time.time())


def digest(*parts, n=12):
    return hashlib.sha1("|".join(str(p) for p in parts).encode("utf-8")).hexdigest()[:n]


def _one(rows):
    return rows[0] if rows else None


def _node(n):
    return dict(n.properties) if n is not None else None


def _json(v):
    return json.dumps(v, separators=(",", ":"), default=str)


# ---- investigations --------------------------------------------------------------------------
def trace_id_for(inv_id):
    """The investigation's Langfuse trace ID: the same rule as Langfuse.create_trace_id(seed=inv_id)."""
    return hashlib.sha256(inv_id.encode("utf-8")).digest()[:16].hex()


def create_investigation(mem, complaint_id, trace_id=None, inv_id=None):
    inv_id = inv_id or new_id(f"INV-{complaint_id[4:]}")
    trace_id = trace_id or trace_id_for(inv_id)
    mem.query("MERGE (i:Investigation {id: $id}) ON CREATE SET i.complaint_id = $c, i.status = 'new', "
              "i.trace_id = $t, i.started_at = $ts, i.updated_at = $ts",
              {"id": inv_id, "c": complaint_id, "t": trace_id, "ts": now()})
    return inv_id


def get_investigation(mem, inv_id):
    r = _one(mem.rows("MATCH (i:Investigation {id: $id}) RETURN i", {"id": inv_id}))
    return _node(r[0]) if r else None


def list_investigations(mem, limit=100):
    rows = mem.rows("MATCH (i:Investigation) RETURN i ORDER BY i.started_at DESC LIMIT $n", {"n": limit})
    return [_node(r[0]) for r in rows]


def move(mem, inv_id, from_statuses, to_status, **props):
    """Move an investigation from one of `from_statuses` to `to_status` along the state machine (handoff/state.py).
    Raises IllegalTransition when no such transition exists; returns False if it was not in `from_statuses`."""
    froms = S.check(from_statuses, to_status)
    sets = "".join(f", i.{k} = ${k}" for k in props)
    r = mem.query(f"MATCH (i:Investigation {{id: $id}}) WHERE i.status IN $from "
                  f"SET i.status = $to, i.updated_at = $ts{sets} RETURN i.id",
                  {"id": inv_id, "from": froms, "to": to_status, "ts": now(), **props})
    return bool(r.result_set)


def claim_next(mem, from_status, to_status, agent=None, lease_s=LEASE_S):
    """Atomically claim the oldest investigation waiting in `from_status`, with a lease; returns its ID or None."""
    S.check([from_status], to_status)
    t = now()
    r = mem.query("MATCH (i:Investigation) WHERE i.status = $from WITH i ORDER BY i.started_at, i.id LIMIT 1 "
                  "SET i.status = $to, i.updated_at = $ts, i.claimed_by = $agent, i.claimed_at = $ts, "
                  "i.lease_until = $lease RETURN i.id",
                  {"from": from_status, "to": to_status, "ts": t, "agent": agent or "", "lease": t + int(lease_s)})
    return r.result_set[0][0] if r.result_set else None


def claim_one(mem, inv_id, from_statuses, to_status, agent=None, lease_s=LEASE_S):
    """Claim one named investigation if it waits in one of `from_statuses` (the Adjudicator taking back its own retry)."""
    froms = S.check(from_statuses, to_status)
    t = now()
    r = mem.query("MATCH (i:Investigation {id: $id}) WHERE i.status IN $from "
                  "SET i.status = $to, i.updated_at = $ts, i.claimed_by = $agent, i.claimed_at = $ts, "
                  "i.lease_until = $lease RETURN i.status",
                  {"id": inv_id, "from": froms, "to": to_status, "ts": t, "agent": agent or "", "lease": t + int(lease_s)})
    return inv_id if r.result_set else None


def claim_stale(mem, working_status, agent=None, lease_s=LEASE_S, force=False):
    """Take back an investigation left in a working status by a crashed worker: its lease has run out, or (force)
    the agent's worker has just restarted and so cannot still be working on it. Returns its ID or None."""
    S.check([working_status], working_status)
    t = now()
    r = mem.query("MATCH (i:Investigation) WHERE i.status = $s AND ($force OR coalesce(i.lease_until, 0) < $ts) "
                  "WITH i ORDER BY i.started_at, i.id LIMIT 1 "
                  "SET i.updated_at = $ts, i.claimed_by = $agent, i.claimed_at = $ts, i.lease_until = $lease, "
                  "i.reclaims = coalesce(i.reclaims, 0) + 1 RETURN i.id",
                  {"s": working_status, "force": bool(force), "ts": t, "agent": agent or "", "lease": t + int(lease_s)})
    return r.result_set[0][0] if r.result_set else None


def renew(mem, inv_id, lease_s=LEASE_S):
    """Extend a working investigation's lease (every Step does this)."""
    mem.query("MATCH (i:Investigation {id: $id}) WHERE i.status IN $working SET i.lease_until = $lease",
              {"id": inv_id, "working": sorted(S.WORKING.values()), "lease": now() + int(lease_s)})


# ---- handoffs ----------------------------------------------------------------------------
def handoff_id(inv_id, kind, payload):
    return f"HO-{digest(inv_id, kind, _json(payload), n=16)}"


def add_handoff(mem, inv_id, kind, from_agent, to_agent, payload, schema_version=1):
    """Store a handoff; the ID comes from its content, so the same submission twice is one node."""
    hid = handoff_id(inv_id, kind, payload)
    mem.query("MATCH (i:Investigation {id: $inv}) OPTIONAL MATCH (i)-[:HAS_HANDOFF]->(o:Handoff) "
              "WITH i, count(o) AS n "
              "MERGE (h:Handoff {id: $id}) ON CREATE SET h.kind = $kind, h.from_agent = $f, h.to_agent = $t, "
              "h.schema_version = $v, h.payload = $p, h.ts = $ts, h.n = n + 1 "
              "MERGE (i)-[:HAS_HANDOFF]->(h)",
              {"inv": inv_id, "id": hid, "kind": kind, "f": from_agent, "t": to_agent, "v": schema_version,
               "p": _json(payload), "ts": now()})
    return hid


def handoffs(mem, inv_id, kind=None):
    rows = mem.rows("MATCH (:Investigation {id: $inv})-[:HAS_HANDOFF]->(h:Handoff) "
                    "WHERE $kind IS NULL OR h.kind = $kind RETURN h ORDER BY coalesce(h.n, 0), h.ts, h.id",
                    {"inv": inv_id, "kind": kind})
    out = []
    for r in rows:
        h = _node(r[0])
        h["payload"] = json.loads(h["payload"])
        out.append(h)
    return out


def latest_handoff(mem, inv_id, kind):
    hs = handoffs(mem, inv_id, kind)
    return hs[-1] if hs else None


# ---- entity references, involvement ------------------------------------------------------------
def entity_ref(mem, kind, entity_id):
    mem.query("MERGE (e:EntityRef {id: $id}) ON CREATE SET e.kind = $kind", {"id": entity_id, "kind": kind})


def involve(mem, inv_id, refs):
    """(Investigation)-[:INVOLVES {role, via, layer, tainted}]->(EntityRef) for each ref: the accounts a trace reached
    and their devices, phones and IPs, so a later case can find this one through any of them."""
    rows = {}
    for r in refs:                       # one row per entity: a repeated ID in one MERGE batch would break uniqueness
        rows.setdefault(str(r["id"]), {"id": str(r["id"]), "kind": r.get("kind", "account"), "role": r.get("role", ""),
                                       "via": r.get("via") or "", "layer": int(r.get("layer") or 0),
                                       "tainted": int(r.get("tainted") or 0)})
    rows = list(rows.values())
    for i in range(0, len(rows), 500):
        mem.query("MATCH (i:Investigation {id: $inv}) UNWIND $rows AS r "
                  "MERGE (e:EntityRef {id: r.id}) ON CREATE SET e.kind = r.kind "
                  "MERGE (i)-[x:INVOLVES]->(e) ON CREATE SET x.role = r.role, x.via = r.via, x.layer = r.layer, "
                  "x.tainted = r.tainted", {"inv": inv_id, "rows": rows[i:i + 500]})
    return len(rows)


def involved(mem, inv_id):
    rows = mem.rows("MATCH (:Investigation {id: $inv})-[x:INVOLVES]->(e:EntityRef) RETURN e.id, e.kind, x.role, x.via",
                    {"inv": inv_id})
    return [dict(zip(["id", "kind", "role", "via"], r)) for r in rows]


# ---- steps -------------------------------------------------------------------------------------
def add_step(mem, inv_id, agent, tool, input_summary, output_summary, latency_ms, tokens=0, step_id=None, node=None,
             usd=0.0, started_ms=None, status="ok"):
    sid = step_id or new_id("STEP")
    mem.query("MATCH (i:Investigation {id: $inv}) OPTIONAL MATCH (i)-[:HAS_STEP]->(s:Step) "
              "WITH i, count(s) AS n "
              "MERGE (st:Step {id: $id}) ON CREATE SET st.n = n + 1, st.agent = $agent, st.node = $node, st.tool = $tool, "
              "st.input_summary = $in, st.output_summary = $out, st.latency_ms = $ms, st.tokens = $tok, st.usd = $usd, "
              "st.started_ms = $start, st.status = $status, st.ts = $ts "
              "MERGE (i)-[:HAS_STEP]->(st)",
              {"inv": inv_id, "id": sid, "agent": agent, "node": node or tool, "tool": tool,
               "in": str(input_summary)[:500], "out": str(output_summary)[:500], "ms": int(latency_ms),
               "tok": int(tokens), "usd": float(usd), "start": int(started_ms or now() * 1000), "status": status,
               "ts": now()})
    return sid


def steps(mem, inv_id):
    rows = mem.rows("MATCH (:Investigation {id: $inv})-[:HAS_STEP]->(s:Step) RETURN s ORDER BY s.n, s.id",
                    {"inv": inv_id})
    return [_node(r[0]) for r in rows]


# ---- claims ------------------------------------------------------------------------------------
def write_claims(mem, inv_id, attempt, accounts):
    """One Claim per account in a proposal: what the Adjudicator asserted and the evidence it cited."""
    rows, seen = [], set()
    for a in accounts:
        if a["account_id"] in seen:
            continue
        seen.add(a["account_id"])
        rows.append({"id": f"CLM-{inv_id[4:]}-a{attempt}-{a['account_id']}", "acc": a["account_id"],
                     "text": a.get("rationale", ""), "label": a.get("label", ""), "action": a.get("proposed_action", ""),
                     "amount": int(a.get("lien_amount_paise") or 0),
                     "transfers": list(a.get("evidence", {}).get("transfer_ids", []))[:50],
                     "signals": list(a.get("evidence", {}).get("signals", []))[:12],
                     "lessons": list(a.get("lessons_applied", []))[:10], "decision": a.get("lien_proposal_id") or ""})
    if rows:
        mem.query("MATCH (i:Investigation {id: $inv}) UNWIND $rows AS r "
                  "MERGE (e:EntityRef {id: r.acc}) ON CREATE SET e.kind = 'account' "
                  "MERGE (c:Claim {id: r.id}) ON CREATE SET c.text = r.text, c.account_id = r.acc, c.label = r.label, "
                  "c.proposed_action = r.action, c.amount = r.amount, c.transfer_ids = r.transfers, "
                  "c.signals = r.signals, c.lessons_applied = r.lessons, c.decision_id = r.decision, c.attempt = $att, "
                  "c.ts = $ts MERGE (i)-[:PRODUCED]->(c) MERGE (c)-[:ABOUT]->(e)",
                  {"inv": inv_id, "rows": rows, "att": int(attempt), "ts": now()})
    return [r["id"] for r in rows]


def claims(mem, inv_id):
    rows = mem.rows("MATCH (:Investigation {id: $inv})-[:PRODUCED]->(c:Claim) RETURN c ORDER BY c.attempt, c.id",
                    {"inv": inv_id})
    return [_node(r[0]) for r in rows]


# ---- decisions ---------------------------------------------------------------------------------
def write_decision(mem, inv_id, decision_id, account_id, label, proposed_action, lien_amount, reason):
    """A proposed decision with `approved` unset; idempotent on decision_id (returns the existing one)."""
    existing = get_decision(mem, decision_id)
    if existing:
        return existing
    mem.query("MATCH (i:Investigation {id: $inv}) MERGE (e:EntityRef {id: $acc}) ON CREATE SET e.kind = 'account' "
              "MERGE (d:Decision {id: $id}) ON CREATE SET d.account_id = $acc, d.label = $label, "
              "d.proposed_action = $action, d.lien_amount = $amount, d.reason = $reason, d.ts = $ts "
              "MERGE (i)-[:HAS_DECISION]->(d) MERGE (d)-[:ABOUT]->(e) MERGE (i)-[:INVOLVES]->(e)",
              {"inv": inv_id, "id": decision_id, "acc": account_id, "label": label, "action": proposed_action,
               "amount": int(lien_amount), "reason": reason, "ts": now()})
    return get_decision(mem, decision_id)


def get_decision(mem, decision_id):
    r = _one(mem.rows("MATCH (i:Investigation)-[:HAS_DECISION]->(d:Decision {id: $id}) RETURN d, i.id",
                      {"id": decision_id}))
    if not r:
        return None
    d = _node(r[0])
    d["investigation_id"] = r[1]
    return d


def decisions(mem, inv_id):
    rows = mem.rows("MATCH (:Investigation {id: $inv})-[:HAS_DECISION]->(d:Decision) RETURN d ORDER BY d.id",
                    {"inv": inv_id})
    return [_node(r[0]) for r in rows]


def record_gate(mem, decision_id, approved, decided_by, gate_mode, reason):
    """Only the HITL gate code calls this: it is the one place `approved` is ever set."""
    r = mem.query("MATCH (d:Decision {id: $id}) WHERE d.approved IS NULL "
                  "SET d.approved = $ok, d.decided_by = $by, d.gate_mode = $mode, d.gate_reason = $reason, "
                  "d.gate_ts = $ts RETURN d.id",
                  {"id": decision_id, "ok": bool(approved), "by": decided_by, "mode": gate_mode, "reason": reason,
                   "ts": now()})
    return bool(r.result_set)


def prior_decisions(mem, account_id):
    rows = mem.rows("MATCH (i:Investigation)-[:HAS_DECISION]->(d:Decision)-[:ABOUT]->(:EntityRef {id: $acc}) "
                    "RETURN i.id, d.id, d.label, d.proposed_action, d.approved, d.decided_by, d.gate_reason, d.ts "
                    "ORDER BY d.ts", {"acc": account_id})
    keys = ["investigation_id", "decision_id", "label", "proposed_action", "approved", "decided_by", "reason", "ts"]
    return [dict(zip(keys, r)) for r in rows]


def approved_mules(mem):
    rows = mem.rows("MATCH (d:Decision) WHERE d.approved = true AND d.label = 'mule' RETURN DISTINCT d.account_id")
    return {r[0] for r in rows}


def recent_decisions(mem, limit=50):
    rows = mem.rows("MATCH (i:Investigation)-[:HAS_DECISION]->(d:Decision) RETURN d, i.id, i.complaint_id "
                    "ORDER BY coalesce(d.gate_ts, d.ts) DESC LIMIT $n", {"n": limit})
    out = []
    for d, iid, cid in rows:
        x = _node(d)
        x.update(investigation_id=iid, complaint_id=cid)
        out.append(x)
    return out


# ---- actions and liens -----------------------------------------------------------------------
def action_id_for(inv_id, kind, entity_id, key=""):
    """Deterministic: the same action on the same entity in the same investigation is one action, so a worker that
    restarts mid-run and repeats its steps does not repeat its actions."""
    return f"ACT-{digest(inv_id, kind, entity_id, key)}"


def write_action(mem, inv_id, kind, entity_kind, entity_id, detail, external_id=None, status="open", action_id=None):
    aid = action_id or action_id_for(inv_id, kind, entity_id)
    mem.query("MATCH (i:Investigation {id: $inv}) MERGE (e:EntityRef {id: $ent}) ON CREATE SET e.kind = $ekind "
              "MERGE (a:Action {id: $id}) ON CREATE SET a.kind = $kind, a.status = $status, a.detail = $detail, "
              "a.external_id = $ext, a.ts = $ts MERGE (i)-[:TOOK]->(a) MERGE (a)-[:ON]->(e) MERGE (i)-[:INVOLVES]->(e)",
              {"inv": inv_id, "ent": entity_id, "ekind": entity_kind, "id": aid, "kind": kind, "status": status,
               "detail": detail[:500], "ext": external_id, "ts": now()})
    return aid


def get_action(mem, action_id):
    r = _one(mem.rows("MATCH (i:Investigation)-[:TOOK]->(a:Action {id: $id})-[:ON]->(e:EntityRef) RETURN a, i.id, e.id, e.kind",
                      {"id": action_id}))
    if not r:
        return None
    a = _node(r[0])
    a.update(investigation_id=r[1], entity_id=r[2], entity_kind=r[3])
    return a


def set_action_status(mem, action_id, status, from_statuses, **props):
    sets = "".join(f", a.{k} = ${k}" for k in props)
    r = mem.query(f"MATCH (a:Action {{id: $id}}) WHERE a.status IN $from SET a.status = $status, a.updated_at = $ts{sets} "
                  f"RETURN a.id", {"id": action_id, "from": list(from_statuses), "status": status, "ts": now(), **props})
    return bool(r.result_set)


def actions(mem, inv_id):
    rows = mem.rows("MATCH (:Investigation {id: $inv})-[:TOOK]->(a:Action)-[:ON]->(e:EntityRef) "
                    "RETURN a, e.id, e.kind ORDER BY a.ts, a.id", {"inv": inv_id})
    out = []
    for a, eid, ekind in rows:
        d = _node(a)
        d.update(entity_id=eid, entity_kind=ekind)
        out.append(d)
    return out


def reviews(mem, status="open", limit=200):
    rows = mem.rows("MATCH (i:Investigation)-[:TOOK]->(a:Action {kind: 'review'})-[:ON]->(e:EntityRef) "
                    "WHERE $status IS NULL OR a.status = $status "
                    "RETURN a, i.id, i.complaint_id, e.id ORDER BY a.ts DESC LIMIT $n",
                    {"status": status, "n": limit})
    out = []
    for a, iid, cid, eid in rows:
        d = _node(a)
        d.update(investigation_id=iid, complaint_id=cid, account_id=eid)
        out.append(d)
    return out


def lien_for(mem, decision_id):
    r = _one(mem.rows("MATCH (:Decision {id: $id})-[:EXECUTED_AS]->(l:Lien) RETURN l", {"id": decision_id}))
    return _node(r[0]) if r else None


def write_lien(mem, decision_id, lien_id, account_id, amount, status, external_id):
    mem.query("MATCH (d:Decision {id: $dec}) WHERE NOT (d)-[:EXECUTED_AS]->(:Lien) "
              "MERGE (l:Lien {id: $id}) ON CREATE SET l.account_id = $acc, l.amount = $amount, l.status = $status, "
              "l.external_id = $ext, l.ts = $ts MERGE (d)-[:EXECUTED_AS]->(l)",
              {"dec": decision_id, "id": lien_id, "acc": account_id, "amount": int(amount), "status": status,
               "ext": external_id, "ts": now()})
    return lien_for(mem, decision_id)


def liens(mem, inv_id):
    rows = mem.rows("MATCH (:Investigation {id: $inv})-[:HAS_DECISION]->(d:Decision)-[:EXECUTED_AS]->(l:Lien) "
                    "RETURN l, d.id ORDER BY l.ts", {"inv": inv_id})
    out = []
    for l, did in rows:
        d = _node(l)
        d["decision_id"] = did
        out.append(d)
    return out


def liens_on(mem, account_id):
    """Liens this bank already placed or requested on an account, in any investigation."""
    rows = mem.rows("MATCH (i:Investigation)-[:HAS_DECISION]->(d:Decision)-[:EXECUTED_AS]->(l:Lien {account_id: $acc}) "
                    "RETURN l.id, l.amount, l.status, i.id, i.complaint_id", {"acc": account_id})
    return [dict(zip(["lien_id", "amount_paise", "status", "investigation_id", "complaint_id"], r)) for r in rows]


# ---- lessons -----------------------------------------------------------------------------------
def _lesson(n):
    d = _node(n)
    if d is None:
        return None
    d["history"] = json.loads(d.get("history") or "[]")
    d["applies_to"] = list(d.get("applies_to") or [])
    return d


def create_lesson(mem, lesson_id, decision_id, text, stance, source, label, applies_to, confidence):
    """A lesson made at the gate, linked to the decision that made it and to the entities it applies to.
    Idempotent on lesson_id (one lesson per decision)."""
    from memory.lessons import entity_ids
    ts = now()
    hist = [{"ts": ts, "event": "created", "decision_id": decision_id, "confidence": confidence}]
    mem.query("MATCH (d:Decision {id: $dec}) "
              "MERGE (l:Lesson {id: $id}) ON CREATE SET l.text = $text, l.stance = $stance, l.source = $source, "
              "l.label = $label, l.applies_to = $applies, l.confidence = $conf, l.times_confirmed = 0, "
              "l.times_contradicted = 0, l.active = true, l.decision_id = $dec, l.created_ts = $ts, l.updated_ts = $ts, "
              "l.history = $hist MERGE (d)-[:CREATED]->(l)",
              {"dec": decision_id, "id": lesson_id, "text": text[:400], "stance": stance, "source": source,
               "label": label or "", "applies": list(applies_to), "conf": float(confidence), "ts": ts,
               "hist": _json(hist)})
    ents = list(dict.fromkeys(entity_ids(applies_to)))
    if ents:
        mem.query("MATCH (l:Lesson {id: $id}) UNWIND $ents AS eid MERGE (e:EntityRef {id: eid}) "
                  "MERGE (l)-[:APPLIES_TO]->(e)", {"id": lesson_id, "ents": ents})
    return get_lesson(mem, lesson_id)


def get_lesson(mem, lesson_id):
    r = _one(mem.rows("MATCH (l:Lesson {id: $id}) OPTIONAL MATCH (l)-[:SUPERSEDED_BY]->(n:Lesson) RETURN l, n.id",
                      {"id": lesson_id}))
    if not r:
        return None
    d = _lesson(r[0])
    d["superseded_by"] = r[1]
    return d


def lessons_touching(mem, entity_ids_, active_only=True):
    """Lessons whose scope includes any of these accounts, devices or phones.

    Each entity is looked up first and passed on with WITH (QUERIES.md, rule 3). Written as two MATCH clauses, FalkorDB
    6.0.2 ran the filter on e.id before e was bound and returned no rows whenever the lesson filter was there."""
    if not entity_ids_:
        return []
    rows = mem.rows("UNWIND $ids AS eid MATCH (e:EntityRef {id: eid}) WITH e MATCH (l:Lesson)-[:APPLIES_TO]->(e) "
                    "WHERE NOT $active OR l.active = true "
                    "WITH l, collect(DISTINCT e.id) AS via RETURN l, via ORDER BY l.confidence DESC, l.id",
                    {"ids": list(entity_ids_), "active": bool(active_only)})
    out = []
    for n, via in rows:
        d = _lesson(n)
        d["matched"] = sorted(via)
        out.append(d)
    return out


def active_lessons(mem, limit=10):
    rows = mem.rows("MATCH (l:Lesson) WHERE l.active = true RETURN l ORDER BY l.confidence DESC, l.created_ts DESC "
                    "LIMIT $n", {"n": limit})
    return [_lesson(r[0]) for r in rows]


def all_lessons(mem, limit=200):
    rows = mem.rows("MATCH (l:Lesson) OPTIONAL MATCH (l)-[:SUPERSEDED_BY]->(n:Lesson) "
                    "OPTIONAL MATCH (d:Decision)-[:CREATED]->(l) OPTIONAL MATCH (i:Investigation)-[:HAS_DECISION]->(d) "
                    "RETURN l, n.id, i.id, i.complaint_id, d.account_id ORDER BY l.created_ts DESC LIMIT $n", {"n": limit})
    out = []
    for n, sup, iid, cid, acc in rows:
        d = _lesson(n)
        d.update(superseded_by=sup, investigation_id=iid, complaint_id=cid, account_id=acc)
        out.append(d)
    return out


def lessons_by_id(mem, ids):
    if not ids:
        return {}
    rows = mem.rows("MATCH (l:Lesson) WHERE l.id IN $ids RETURN l", {"ids": list(ids)})
    return {d["id"]: d for d in (_lesson(r[0]) for r in rows)}


def update_lesson(mem, lesson_id, fields):
    f = dict(fields)
    if "history" in f:
        f["history"] = _json(f["history"])
    sets = ", ".join(f"l.{k} = ${k}" for k in f)
    mem.query(f"MATCH (l:Lesson {{id: $id}}) SET {sets}, l.updated_ts = $ts", {"id": lesson_id, "ts": now(), **f})


def supersede(mem, old_id, new_id_, history):
    mem.query("MATCH (o:Lesson {id: $old}), (n:Lesson {id: $new}) MERGE (o)-[:SUPERSEDED_BY]->(n) "
              "SET o.active = false, o.history = $hist, o.updated_ts = $ts",
              {"old": old_id, "new": new_id_, "hist": _json(history), "ts": now()})


# ---- messages (case chat) -----------------------------------------------------------------------
def add_message(mem, inv_id, role, text, status, cited_ids=(), reply_to=None, author="", note=""):
    """A question or answer in the case chat: MSG-<investigation>-<n>, in order."""
    r = mem.query("MATCH (i:Investigation {id: $inv}) OPTIONAL MATCH (i)-[:HAS_MESSAGE]->(m:Message) "
                  "WITH i, count(m) AS n "
                  "CREATE (i)-[:HAS_MESSAGE]->(x:Message {id: $prefix + toString(n + 1), n: n + 1, role: $role, "
                  "text: $text, status: $status, cited_ids: $cited, reply_to: $reply, author: $author, note: $note, "
                  "ts: $ts}) RETURN x.id",
                  {"inv": inv_id, "prefix": f"MSG-{inv_id[4:]}-", "role": role, "text": text[:4000],
                   "status": status, "cited": list(cited_ids)[:40], "reply": reply_to or "", "author": author[:60],
                   "note": note[:500], "ts": now()})
    return r.result_set[0][0] if r.result_set else None


def messages(mem, inv_id):
    rows = mem.rows("MATCH (:Investigation {id: $inv})-[:HAS_MESSAGE]->(m:Message) RETURN m ORDER BY m.n",
                    {"inv": inv_id})
    return [_node(r[0]) for r in rows]


def claim_question(mem, lease_s=300):
    """The oldest unanswered question at this bank (or one whose answerer's lease ran out), marked as being answered."""
    t = now()
    r = mem.query("MATCH (i:Investigation)-[:HAS_MESSAGE]->(m:Message {role: 'human'}) "
                  "WHERE m.status = 'pending' OR (m.status = 'answering' AND coalesce(m.lease_until, 0) < $ts) "
                  "WITH i, m ORDER BY m.ts, m.n, m.id LIMIT 1 SET m.status = 'answering', m.lease_until = $lease "
                  "RETURN m.id, i.id", {"ts": t, "lease": t + int(lease_s)})
    return (r.result_set[0][0], r.result_set[0][1]) if r.result_set else (None, None)


def set_message(mem, message_id, **props):
    sets = ", ".join(f"m.{k} = ${k}" for k in props)
    mem.query(f"MATCH (m:Message {{id: $id}}) SET {sets}", {"id": message_id, **props})


def get_message(mem, message_id):
    r = _one(mem.rows("MATCH (i:Investigation)-[:HAS_MESSAGE]->(m:Message {id: $id}) RETURN m, i.id", {"id": message_id}))
    if not r:
        return None
    m = _node(r[0])
    m["investigation_id"] = r[1]
    return m


def existing_ids(mem, ids):
    """Which of these memory IDs (investigations, decisions, lessons, claims, actions, liens, handoffs, messages,
    steps) exist in this bank's memory."""
    if not ids:
        return set()
    rows = mem.rows("UNWIND $ids AS x MATCH (n) WHERE n.id = x RETURN n.id, labels(n)", {"ids": list(ids)})
    return {r[0] for r in rows if "EntityRef" not in list(r[1] or [])}


def counts(mem):
    rows = mem.rows("MATCH (n) RETURN labels(n)[0], count(n)")
    return {r[0]: r[1] for r in rows}
