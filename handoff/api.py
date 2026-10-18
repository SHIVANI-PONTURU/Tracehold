"""The handoff API on the tool server (Phase 2 step 5; Phase 3 steps 1-10), and the run state machine.

Agents claim work and submit handoffs here; nothing else passes between them. Every submission is validated against
its Pydantic contract (handoff/contracts.py) and stored as a Handoff node in the bank's memory graph. Status changes
follow handoff/state.py; anything else is refused. The UI reaches the system only through these routes (the service
layer, Phase 3 step 1): it never talks to FalkorDB.

  new --tracer claims--> tracing --trace--> traced --adjudicator claims--> adjudicating --proposal-->
      checks pass:                proposed --verifier claims--> verifying --verdict--> at_gate --gate--> executed
      checks fail (first time):   checks_failed --adjudicator claims--> adjudicating (one retry)
      checks fail again:          proposed, marked failed with no liens --> verifier --> at_gate --> closed

Claims carry a lease that every Step renews; a claim whose lease ran out (the worker crashed), or any claim of an
agent whose worker has just restarted (`resume`), is taken back, and the run continues from its stored handoffs.

Routes (JSON). Agents:
  POST /handoff/claim                    {bank_id, agent, resume?}                next piece of work, or {}
  POST /handoff/submit                   {bank_id, kind, payload}                 trace, proposal or verdict
  GET  /handoff/read/{bank}/{inv}/{kind}                                          latest handoff of a kind
  POST /handoff/step                     {bank_id, investigation_id, step}        one Step after an agent node
  POST /handoff/gate/record | execute | finish                                    gate code only (the Verifier)
  GET  /handoff/gate/{bank}/{inv}                                                 gate decisions so far
  POST /handoff/chat/claim               {bank_id}                                next case-chat question (Verifier)
  POST /handoff/chat/answer              {bank_id, message_id, answer, cited_ids, final}
  POST /handoff/fail                     {bank_id, investigation_id, agent, reason}  a run guard stopped the run
  GET  /handoff/spend?investigation_id=  model spend totals;  POST /handoff/spend {one model call}
The UI (outside the sandboxes):
  POST /handoff/investigations           {bank_id, complaint_id}                  start an investigation
  GET  /handoff/investigations?bank_id=  list;   GET /handoff/investigations/{bank}/{inv}  everything about one run
  GET  /handoff/complaints?bank_id=      complaints reported to this bank;   GET /handoff/banks?ids=  bank names
  POST /handoff/gate/decide              {bank_id, investigation_id, decisions, gate_mode}  the human at the gate
  POST /handoff/actions/undo             {bank_id, investigation_id, action_id, reason}     reverse an automatic action
  GET  /handoff/reviews?bank_id=&status=  the review queue;  POST /handoff/reviews/resolve {bank_id, action_id, outcome, note}
                                         (resolving also scores the lessons on the account: learn_at_review)
  GET  /handoff/memory?bank_id=          lessons, decisions and cases in this bank's memory
  GET  /handoff/trail/{bank}/{inv}       the trail as nodes and edges, for the graph
  POST /handoff/chat/ask                 {bank_id, investigation_id, text};   GET /handoff/chat/{bank}/{inv}
  POST /handoff/close                    {bank_id, investigation_id, reason}      an analyst closes a case
"""
from __future__ import annotations

import json
import os
import re
import time

import yaml
from pydantic import ValidationError
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from graph import client
from handoff import contracts, spend
from handoff import state as S
from mcp_server import network
from mcp_server import tools as T
from memory import lessons as L
from memory import store

CLAIMS = {"tracer": (["new"], "tracing"), "adjudicator": (["traced", "checks_failed"], "adjudicating"),
          "verifier": (["proposed"], "verifying")}
SUBMITS = {"trace": ("tracing", "tracer"), "proposal": ("adjudicating", "adjudicator"),
           "verdict": ("verifying", "verifier")}
MAX_BODY = 1_000_000
CONFIG = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "agents", "config.yaml")
_cfg = {}


def settings():
    """agents/config.yaml (the lessons and harness sections), read once."""
    if not _cfg:
        with open(CONFIG, encoding="utf-8") as f:
            _cfg.update(yaml.safe_load(f))
    return _cfg


def lease_s():
    return int(settings().get("harness", {}).get("lease_s", store.LEASE_S))


def _err(status, code, message):
    return JSONResponse({"error": {"code": code, "message": message}}, status_code=status)


async def _body(request):
    raw = await request.body()
    if len(raw) > MAX_BODY:
        raise ValueError("request too large")
    return json.loads(raw or b"{}")


AGENT_ROUTES = {"fail", "claim", "submit", "read", "step", "gate_record", "gate_execute", "gate_finish", "spend_post",
                "chat_claim", "chat_answer"}       # the routes only the agents' workers call; the UI calls the rest
VERIFIER_ROUTES = {"gate_record", "gate_execute", "gate_finish", "chat_claim", "chat_answer"}  # their bodies name no agent


def _tagged(fn, pp, qp, b):
    """Run a route with its graph queries logged under its name (graph/client.py, activity)."""
    body = b if isinstance(b, dict) else {}
    agent = str(body.get("agent") or "") or ("verifier" if fn.__name__ in VERIFIER_ROUTES else "")
    token = client.CALLER.set({"source": "agent" if fn.__name__ in AGENT_ROUTES else "ui", "name": fn.__name__, "agent": agent,
                               "bank": str(pp.get("bank") or body.get("bank_id") or qp.get("bank_id") or ""),
                               "investigation_id": str(pp.get("inv") or body.get("investigation_id") or "")})
    try:
        return fn(pp, qp, b)
    finally:
        client.CALLER.reset(token)


def _handler(fn):
    """Read the JSON body on the event loop, then run the (blocking) graph work in a worker thread."""
    async def h(request: Request):
        try:
            body = await _body(request) if request.method == "POST" else {}
            return await run_in_threadpool(_tagged, fn, request.path_params, request.query_params, body)
        except S.IllegalTransition as e:
            return _err(409, "illegal_transition", str(e))
        except (ValueError, T.ToolError) as e:
            return _err(400, getattr(e, "code", "bad_input"), str(e))
    return h


# ---- memory written by code at the handoffs and the gate -----------------------------------------------
def remember_trace(ctx, mem, inv_id, payload, victim):
    """Every account the trace reached, the victim, and the devices, phones and IPs of the traced accounts."""
    accs = payload["accounts"]
    refs = [{"id": victim, "kind": "account", "role": "victim"}]
    refs += [{"id": a["account_id"], "kind": "account", "role": "traced", "layer": a["layer"],
              "tainted": a["tainted_paise"]} for a in accs]
    for acc, ids in network.identifiers(ctx.net, [a["account_id"] for a in accs]).items():
        refs += [{"id": e, "kind": k, "role": "identifier", "via": acc} for k, e in ids]
    return store.involve(mem, inv_id, refs)


def _lessons_near(ctx, mem, acc, cited):
    """The lessons on an account or its devices and phones, plus the active ones cited for it; and its identifiers."""
    idents = [(k, e) for k, e in network.identifiers(ctx.net, [acc])[acc] if k in ("device", "phone")]
    relevant = {l["id"]: l for l in store.lessons_touching(mem, [acc] + [e for _, e in idents])}
    relevant.update({i: l for i, l in store.lessons_by_id(mem, cited).items() if l.get("active")})
    return relevant, idents


def _score(mem, lessons, effect_of, event_id, ts, cfg, key="decision_id"):
    """Confirm or contradict each lesson (effect_of(stance) is +1 or -1) and store it; what changed, per lesson."""
    updated = []
    for l in lessons.values():
        eff = effect_of(l["stance"])
        upd = L.apply(l, eff, event_id, ts, cfg, key=key)
        store.update_lesson(mem, l["id"], upd)
        updated.append({"lesson_id": l["id"], "effect": "confirmed" if eff > 0 else "contradicted",
                        "confidence": upd["confidence"], "active": upd["active"], "history": upd["history"]})
    return updated


def learn_at_gate(ctx, mem, inv_id, decision, approved, decided_by, reason, cfg=None):
    """After a gate decision: update the lessons it confirms or contradicts, make the lesson it teaches (if any),
    and let that lesson supersede older ones it contradicts (Phase 3 steps 7-8)."""
    cfg = cfg if cfg is not None else settings()
    acc = decision["account_id"]
    verdict = next((v for v in (store.latest_handoff(mem, inv_id, "verdict") or {}).get("payload", {}).get("verdicts", [])
                    if v["decision_id"] == decision["id"]), {})
    prop = next((a for a in (store.latest_handoff(mem, inv_id, "proposal") or {}).get("payload", {}).get("accounts", [])
                 if a.get("lien_proposal_id") == decision["id"]), {})
    cited = set(verdict.get("lessons_cited", [])) | set(prop.get("lessons_applied", []))
    relevant, idents = _lessons_near(ctx, mem, acc, cited)
    ts = store.now()
    relevant = {i: l for i, l in relevant.items()                     # not this case's own, and the same label only
                if not i.startswith(f"LES-{inv_id[4:]}-") and L.scored_by(l, decision.get("label"))}
    updated = _score(mem, relevant, lambda stance: L.effect(stance, approved), decision["id"], ts, cfg)
    made = L.lesson_from_gate(approved, decided_by, reason, verdict.get("verdict"))
    lesson = None
    if made:
        stance, source = made
        s = L.settings(cfg)
        lesson = store.create_lesson(mem, f"LES-{decision['id'][4:]}", decision["id"], reason, stance, source,
                                     decision.get("label", ""), L.scope(acc, idents, decision.get("label"), cfg),
                                     s["initial_confidence"].get(source, 0.5))
        for l in relevant.values():
            if l["id"] != lesson["id"] and L.supersedes(stance, l):
                hist = next((u["history"] for u in updated if u["lesson_id"] == l["id"]), l["history"])
                store.supersede(mem, l["id"], lesson["id"], hist + [{"ts": ts, "event": "superseded",
                                                                     "decision_id": decision["id"],
                                                                     "by": lesson["id"]}])
    return {"lesson": lesson, "updated": updated}


def learn_at_review(ctx, mem, review, outcome, cfg=None):
    """After an analyst resolves a review: the lessons on the reviewed account (and its devices and phones), plus
    those the proposal cited for it, are confirmed or contradicted when the review comes from a later case and the
    account has the lesson's label. "escalated" counts as an approved lien, "cleared" as a rejected one. A review
    makes no lesson (decided 17 Oct, 18:30)."""
    cfg = cfg if cfg is not None else settings()
    inv_id, acc = review["investigation_id"], review["entity_id"]
    prop = next((a for a in (store.latest_handoff(mem, inv_id, "proposal") or {}).get("payload", {}).get("accounts", [])
                 if a.get("account_id") == acc), {})
    relevant, _ = _lessons_near(ctx, mem, acc, prop.get("lessons_applied", []))
    relevant = {i: l for i, l in relevant.items() if L.review_counts(l, inv_id, review.get("ts"), prop.get("label"))}
    return _score(mem, relevant, lambda stance: L.review_effect(stance, outcome), review["id"], store.now(), cfg,
                  key="action_id")


# ---- case chat ---------------------------------------------------------------------------------
MEM_ID = re.compile(r"\b(?:INV|DEC|LES|CLM|ACT|HO|MSG|STEP|LREQ|LIEN|INFO)-[0-9A-Za-z](?:[0-9A-Za-z-]*[0-9A-Za-z])?\b")
TRANSFER_ID = re.compile(r"\bT[0-9]{9}\b")
ACCOUNT_ID = re.compile(r"\b[0-9]{1,6}-[0-9A-F]{6,12}\b")
ENTITY_ID = re.compile(r"\b(?:DV|PH)-[0-9]{4,9}\b")


def cited_in(text):
    """The evidence IDs written in a text, by kind (memory IDs first, so an account inside a claim ID is not
    counted twice)."""
    mem_ids = MEM_ID.findall(text)
    rest = MEM_ID.sub(" ", text)
    return {"memory": sorted(set(mem_ids)), "transfer": sorted(set(TRANSFER_ID.findall(rest))),
            "account": sorted(set(ACCOUNT_ID.findall(rest))), "entity": sorted(set(ENTITY_ID.findall(rest)))}


def unknown_ids(ctx, mem, text, cited):
    """The IDs in the answer (written in it or listed as cited) that do not exist in the graphs."""
    ids = cited_in(text + " " + " ".join(cited))
    missing = []
    if ids["memory"]:
        have = store.existing_ids(mem, ids["memory"])
        have |= {r[0] for r in mem.rows("UNWIND $ids AS x MATCH (a:Action {external_id: x}) RETURN DISTINCT x",
                                        {"ids": ids["memory"]})}
        missing += [i for i in ids["memory"] if i not in have]
    if ids["transfer"]:
        have = {t.id for t in network.transfers_by_id(ctx.net, ids["transfer"])}
        missing += [i for i in ids["transfer"] if i not in have]
    if ids["account"]:
        have = set(network.accounts(ctx.net, ids["account"]))
        missing += [i for i in ids["account"] if i not in have]
    missing += [i for i in ids["entity"] if not network.entity_kind(ctx.net, i)]
    return missing


def chat_context(ctx, mem, inv_id):
    """What the Verifier may answer from: the case's trace, claims, decisions, lessons, actions, related cases and
    earlier messages, all read from the graphs by code."""
    inv = store.get_investigation(mem, inv_id)
    comp = network.complaint(ctx.net, inv["complaint_id"]) or {}
    t = (store.latest_handoff(mem, inv_id, "trace") or {}).get("payload", {})
    decs = store.decisions(mem, inv_id)
    accs = [a["account_id"] for a in t.get("accounts", [])]
    made = store.lessons_by_id(mem, [f"LES-{d['id'][4:]}" for d in decs])
    touching = {l["id"]: l for l in store.lessons_touching(mem, accs, active_only=False)}
    touching.update(made)
    related = mem.rows("MATCH (i:Investigation {id: $id})-[:INVOLVES]->(e:EntityRef)<-[:INVOLVES]-(j:Investigation) "
                       "WHERE j.id <> i.id RETURN j.id, j.complaint_id, collect(DISTINCT e.id)[0..5]", {"id": inv_id})
    return {
        "investigation": {k: inv.get(k) for k in ("id", "complaint_id", "status", "gate_mode")},
        "complaint": {"victim_account_id": comp.get("victim_account_id"), "disputed_paise": comp.get("disputed_paise")},
        "trace": {"accounts": [{k: a.get(k) for k in ("account_id", "layer", "tainted_paise", "balance_paise",
                                                      "branch_end")} for a in t.get("accounts", [])][:40],
                  "unrecoverable_paise": t.get("unrecoverable_paise"), "not_followed_paise": t.get("not_followed_paise")},
        "claims": [{k: c.get(k) for k in ("id", "account_id", "label", "proposed_action", "text", "lessons_applied")}
                   | {"transfer_ids": list(c.get("transfer_ids") or [])[:5]} for c in store.claims(mem, inv_id)][-40:],
        "decisions": [{k: d.get(k) for k in ("id", "account_id", "label", "lien_amount", "approved", "decided_by",
                                             "gate_mode", "gate_reason")} for d in decs],
        "liens": [{k: x.get(k) for k in ("id", "decision_id", "account_id", "amount", "status")}
                  for x in store.liens(mem, inv_id)],
        "lessons": [{k: l.get(k) for k in ("id", "text", "stance", "label", "confidence", "active", "times_confirmed",
                                           "times_contradicted", "decision_id")} for l in touching.values()][:15],
        "actions": [{k: a.get(k) for k in ("id", "kind", "entity_id", "status", "external_id")}
                    for a in store.actions(mem, inv_id)][:40],
        "related_cases": [{"investigation_id": r[0], "complaint_id": r[1], "shared": r[2]} for r in related][:10],
        "earlier_messages": [{k: m.get(k) for k in ("id", "role", "text", "status")} for m in store.messages(mem, inv_id)][-10:],
    }


def make_routes(ctx: T.Ctx):
    def start(pp, qp, b):
        bank, cid = str(b.get("bank_id", "")), str(b.get("complaint_id", ""))
        comp = network.complaint(ctx.net, cid)
        if not comp:
            return _err(404, "not_found", f"no complaint {cid}")
        victim_bank = comp["victim_account_id"].split("-")[0]
        if bank != victim_bank:
            return _err(403, "forbidden", f"{cid} was reported to bank {victim_bank}, not {bank}")
        mem = ctx.memory(bank)
        inv = store.create_investigation(mem, cid, trace_id=b.get("trace_id"))
        return JSONResponse({"investigation_id": inv, "complaint_id": cid, "bank_id": bank,
                             "trace_id": store.get_investigation(mem, inv)["trace_id"]})

    def list_inv(pp, qp, b):
        bank = qp.get("bank_id", "")
        return JSONResponse(store.list_investigations(ctx.memory(bank)))

    def detail(pp, qp, b):
        bank, inv_id = pp["bank"], pp["inv"]
        mem = ctx.memory(bank)
        inv = store.get_investigation(mem, inv_id)
        if not inv:
            return _err(404, "not_found", f"no investigation {inv_id}")
        decs = store.decisions(mem, inv_id)
        made = store.lessons_by_id(mem, [f"LES-{d['id'][4:]}" for d in decs])
        return JSONResponse({"investigation": inv, "handoffs": store.handoffs(mem, inv_id), "decisions": decs,
                             "actions": store.actions(mem, inv_id), "liens": store.liens(mem, inv_id),
                             "steps": store.steps(mem, inv_id), "claims": store.claims(mem, inv_id),
                             "messages": store.messages(mem, inv_id), "lessons_made": list(made.values())})

    def claim(pp, qp, b):
        agent, bank = b.get("agent"), str(b.get("bank_id", ""))
        if agent not in CLAIMS:
            return _err(400, "bad_input", f"agent must be one of {sorted(CLAIMS)}")
        mem = ctx.memory(bank)
        froms, to = CLAIMS[agent]
        if b.get("investigation_id"):                      # one named investigation (the Adjudicator's own retry)
            inv_id = store.claim_one(mem, str(b["investigation_id"]), froms, to, agent, lease_s())
            if not inv_id:
                return JSONResponse({})
            inv = store.get_investigation(mem, inv_id)
            return JSONResponse({"investigation_id": inv_id, "complaint_id": inv["complaint_id"],
                                 "trace_id": inv["trace_id"], "claimed_from": "named", "resumed": False})
        tries = ([(None, True)] if b.get("resume") else []) + [(f, False) for f in froms] + [(None, False)]
        for f, force in tries:
            inv_id = store.claim_next(mem, f, to, agent, lease_s()) if f else \
                store.claim_stale(mem, to, agent, lease_s(), force=force)
            if inv_id:
                inv = store.get_investigation(mem, inv_id)
                return JSONResponse({"investigation_id": inv_id, "complaint_id": inv["complaint_id"],
                                     "trace_id": inv["trace_id"], "claimed_from": f or to, "resumed": f is None})
        return JSONResponse({})

    def submit(pp, qp, b):
        kind, bank, payload = b.get("kind"), str(b.get("bank_id", "")), b.get("payload") or {}
        if kind not in SUBMITS:
            return _err(400, "bad_input", f"kind must be one of {sorted(SUBMITS)}")
        try:
            h = contracts.parse(kind, payload)
        except ValidationError as e:
            return _err(422, "invalid_handoff", e.json(include_url=False)[:2000])
        mem = ctx.memory(bank)
        inv = store.get_investigation(mem, h.investigation_id)
        status, agent = SUBMITS[kind]
        if not inv:
            return _err(404, "not_found", f"no investigation {h.investigation_id}")
        if inv["status"] != status:
            return _err(409, "wrong_state", f"{h.investigation_id} is {inv['status']}, not {status}")
        data = h.model_dump(mode="json")
        frm, to = contracts.ROUTES[kind]
        if kind == "trace":
            store.add_handoff(mem, h.investigation_id, kind, frm, to, data)
            comp = network.complaint(ctx.net, inv["complaint_id"])
            remember_trace(ctx, mem, h.investigation_id, data, comp["victim_account_id"])
            store.move(mem, h.investigation_id, [status], "traced")
            return JSONResponse({"stored": True, "status": "traced"})
        if kind == "verdict":
            store.add_handoff(mem, h.investigation_id, kind, frm, to, data)
            store.move(mem, h.investigation_id, [status], "at_gate")
            return JSONResponse({"stored": True, "status": "at_gate"})
        # proposal: run the deterministic checks first
        from safety import checks
        result = checks.run(ctx, bank, h.investigation_id, data)
        notes = store.handoffs(mem, h.investigation_id, "check_failure")
        data["checks"] = result
        store.write_claims(mem, h.investigation_id, h.attempt, data["accounts"])
        if result["passed"]:
            store.add_handoff(mem, h.investigation_id, kind, frm, to, data)
            store.move(mem, h.investigation_id, [status], "proposed")
            return JSONResponse({"stored": True, "status": "proposed", "checks": result})
        if not notes and h.attempt == 1:
            note = contracts.CheckFailureNote(investigation_id=h.investigation_id,
                                              failed=[contracts.FailedCheck(**f) for f in result["failed"]])
            store.add_handoff(mem, h.investigation_id, kind, frm, to, data)
            store.add_handoff(mem, h.investigation_id, "check_failure", "checks", "adjudicator",
                              note.model_dump(mode="json"))
            store.move(mem, h.investigation_id, [status], "checks_failed")
            return JSONResponse({"stored": True, "status": "checks_failed", "checks": result})
        data["checks_failed_twice"] = True               # the Verifier sees it marked failed, with no liens
        store.add_handoff(mem, h.investigation_id, kind, frm, to, data)
        store.move(mem, h.investigation_id, [status], "proposed")
        return JSONResponse({"stored": True, "status": "proposed", "checks": result, "failed_twice": True})

    def read(pp, qp, b):
        bank, inv_id, kind = pp["bank"], pp["inv"], pp["kind"]
        h = store.latest_handoff(ctx.memory(bank), inv_id, kind)
        return JSONResponse(h or {})

    def step(pp, qp, b):
        mem = ctx.memory(str(b.get("bank_id", "")))
        inv_id, s = b.get("investigation_id"), b.get("step") or {}
        if not store.get_investigation(mem, inv_id):
            return _err(404, "not_found", f"no investigation {inv_id}")
        agent = str(s.get("agent", ""))
        if agent not in ("tracer", "adjudicator", "verifier", "gate", "chat"):
            return _err(422, "bad_input", "step.agent is tracer, adjudicator, verifier, gate or chat")
        sid = store.add_step(mem, inv_id, agent, str(s.get("tool", ""))[:200], s.get("input_summary", ""),
                             s.get("output_summary", ""), float(s.get("latency_ms", 0)), int(s.get("tokens", 0)),
                             step_id=str(s.get("id") or "")[:80] or None, node=str(s.get("node", ""))[:40],
                             usd=float(s.get("usd", 0)), started_ms=s.get("started_ms"),
                             status=str(s.get("status", "ok"))[:20])
        store.renew(mem, inv_id, lease_s())
        return JSONResponse({"step_id": sid})

    def gate_decide(pp, qp, b):
        bank, inv_id = str(b.get("bank_id", "")), b.get("investigation_id")
        decisions = b.get("decisions") or []
        mem = ctx.memory(bank)
        inv = store.get_investigation(mem, inv_id)
        if not inv or inv["status"] != "at_gate":
            return _err(409, "wrong_state", f"{inv_id} is not waiting at the gate")
        mode = str(b.get("gate_mode", "placeholder"))
        if mode not in ("placeholder", "confirm"):
            return _err(422, "bad_input", "gate_mode is placeholder or confirm")
        clean = []
        for d in decisions:
            if set(d) - {"decision_id", "approve", "reason"} or not isinstance(d.get("approve"), bool):
                return _err(422, "bad_input", "each decision is {decision_id, approve: true|false, reason}")
            clean.append({"decision_id": str(d["decision_id"]), "approve": d["approve"],
                          "reason": str(d.get("reason", ""))[:400]})
        mem.query("MATCH (i:Investigation {id: $id}) SET i.gate_decisions = $g, i.gate_mode = $m",
                  {"id": inv_id, "g": json.dumps(clean), "m": mode})
        return JSONResponse({"stored": len(clean)})

    def gate_read(pp, qp, b):
        inv = store.get_investigation(ctx.memory(pp["bank"]), pp["inv"])
        if not inv:
            return _err(404, "not_found", "no such investigation")
        g = inv.get("gate_decisions")
        return JSONResponse({"status": inv["status"], "gate_mode": inv.get("gate_mode"),
                             "decisions": json.loads(g) if g else None})

    def gate_record(pp, qp, b):
        mem = ctx.memory(str(b.get("bank_id", "")))
        inv = store.get_investigation(mem, b.get("investigation_id"))
        d = store.get_decision(mem, str(b.get("decision_id")))
        if not inv or inv["status"] != "at_gate" or not d or d["investigation_id"] != inv["id"]:
            return _err(409, "wrong_state", "the decision is not waiting at this investigation's gate")
        if b.get("decided_by") not in ("verifier_agent", "human") or b.get("gate_mode") not in ("confirm", "placeholder"):
            return _err(422, "bad_input", "decided_by is verifier_agent or human; gate_mode is confirm or placeholder")
        reason = str(b.get("reason", ""))[:400]
        ok = store.record_gate(mem, d["id"], bool(b.get("approved")), b["decided_by"], b["gate_mode"], reason)
        learned = learn_at_gate(ctx, mem, inv["id"], d, bool(b.get("approved")), b["decided_by"], reason) if ok else None
        return JSONResponse({"recorded": ok, "decision": store.get_decision(mem, d["id"]), "learned": learned})

    def gate_execute(pp, qp, b):
        r = T.run_tool(T.execute_lien, ctx, str(b.get("bank_id", "")), b.get("investigation_id"),
                       str(b.get("decision_id")))
        return JSONResponse(r, status_code=409 if "error" in r else 200)

    def gate_finish(pp, qp, b):
        mem = ctx.memory(str(b.get("bank_id", "")))
        inv_id = b.get("investigation_id")
        placed = store.liens(mem, inv_id)
        to = "executed" if placed else "closed"
        moved = store.move(mem, inv_id, ["at_gate"], to)
        return JSONResponse({"status": to if moved else store.get_investigation(mem, inv_id)["status"],
                             "liens": placed})

    def fail(pp, qp, b):
        mem = ctx.memory(str(b.get("bank_id", "")))
        inv_id = b.get("investigation_id")
        moved = store.move(mem, inv_id, [s for s in S.STATUSES if s not in S.FINAL], "failed",
                           failed_by=str(b.get("agent", ""))[:30], failure=str(b.get("reason", ""))[:500])
        return JSONResponse({"failed": moved})

    def close(pp, qp, b):
        mem = ctx.memory(str(b.get("bank_id", "")))
        inv_id = b.get("investigation_id")
        if not str(b.get("reason", "")).strip():
            return _err(422, "bad_input", "closing a case needs a reason")
        moved = store.move(mem, inv_id, [s for s in S.STATUSES if s not in S.FINAL], "closed",
                           closed_reason=str(b["reason"])[:400])
        return JSONResponse({"closed": moved})

    def complaints(pp, qp, b):
        """Complaints reported to this bank (its customers are the victims), for the UI's picker."""
        bank = str(qp.get("bank_id", ""))
        ctx.memory(bank)                                   # validates the bank ID
        rows = ctx.net.rows("MATCH (c:Complaint) WHERE c.victim_account_id STARTS WITH $p "
                            "RETURN c.id, c.victim_account_id, c.disputed_amount, c.reported_at, size(c.fraud_transfer_ids) "
                            "ORDER BY c.id", {"p": f"{bank}-"})
        return JSONResponse([{"complaint_id": r[0], "victim_account_id": r[1], "disputed_paise": r[2],
                              "reported_at": r[3], "fraud_transfers": r[4]} for r in rows])

    def banks(pp, qp, b):
        ids = [x for x in str(qp.get("ids", "")).split(",") if x]
        rows = ctx.net.rows("MATCH (b:Bank) WHERE b.id IN $ids RETURN b.id, b.name", {"ids": ids})
        names = dict(rows)
        return JSONResponse([{"bank_id": i, "name": names.get(i, f"Bank {i}")} for i in ids])

    def undo(pp, qp, b):
        r = T.run_tool(T.undo_action, ctx, str(b.get("bank_id", "")), b.get("investigation_id"), b.get("action_id"),
                       b.get("reason", ""))
        return JSONResponse(r, status_code=400 if "error" in r else 200)

    def review_list(pp, qp, b):
        mem = ctx.memory(str(qp.get("bank_id", "")))
        status = qp.get("status", "open")
        return JSONResponse(store.reviews(mem, None if status == "all" else status))

    def review_resolve(pp, qp, b):
        mem = ctx.memory(str(b.get("bank_id", "")))
        outcome = b.get("outcome")
        if outcome not in ("cleared", "escalated"):
            return _err(422, "bad_input", "outcome is cleared or escalated")
        a = store.get_action(mem, str(b.get("action_id")))
        if not a or a["kind"] != "review":
            return _err(404, "not_found", "no such review")
        ok = store.set_action_status(mem, a["id"], outcome, ["open"], resolution_note=str(b.get("note", ""))[:500],
                                     resolved_at=store.now())
        lessons = learn_at_review(ctx, mem, a, outcome) if ok else []   # a review resolves once, so it scores once
        return JSONResponse({"resolved": ok, "review": store.get_action(mem, a["id"]), "lessons": lessons})

    def memory_view(pp, qp, b):
        mem = ctx.memory(str(qp.get("bank_id", "")))
        invs = store.list_investigations(mem, 200)
        return JSONResponse({"lessons": store.all_lessons(mem), "decisions": store.recent_decisions(mem, 40),
                             "counts": store.counts(mem),
                             "investigations": [{k: i.get(k) for k in ("id", "complaint_id", "status", "started_at")}
                                                for i in invs]})

    def trail(pp, qp, b):
        bank, inv_id = pp["bank"], pp["inv"]
        mem = ctx.memory(bank)
        inv = store.get_investigation(mem, inv_id)
        t = store.latest_handoff(mem, inv_id, "trace") if inv else None
        if not t:
            return JSONResponse({"nodes": [], "edges": []})
        p = t["payload"]
        comp = network.complaint(ctx.net, inv["complaint_id"])
        victim = comp["victim_account_id"]
        accs = {a["account_id"]: a for a in p["accounts"]}
        prop = {a["account_id"]: a for a in (store.latest_handoff(mem, inv_id, "proposal") or {}).get("payload", {}).get("accounts", [])}
        decs = {d["account_id"]: d for d in store.decisions(mem, inv_id)}
        others = {r[0] for r in mem.rows("MATCH (i:Investigation {id: $id})-[:INVOLVES]->(e:EntityRef)<-[:INVOLVES]-(j:Investigation) "
                                         "WHERE j.id <> i.id RETURN DISTINCT e.id", {"id": inv_id})}
        tids = sorted({x for a in p["accounts"] for x in a.get("transfer_ids", [])})
        edges = {}
        for tr in network.transfers_by_id(ctx.net, tids):
            if tr.dst in accs and (tr.src in accs or tr.src == victim):
                e = edges.setdefault((tr.src, tr.dst), {"src": tr.src, "dst": tr.dst, "transfers": 0, "amount_paise": 0,
                                                       "transfer_ids": []})
                e["transfers"] += 1
                e["amount_paise"] += tr.amount
                e["transfer_ids"] = (e["transfer_ids"] + [tr.id])[:5]
        nodes = [{"id": victim, "layer": 0, "role": "victim", "disputed_paise": p["disputed_paise"]}]
        for a in p["accounts"]:
            d = decs.get(a["account_id"], {})
            nodes.append({"id": a["account_id"], "layer": a["layer"], "role": "traced", "tainted_paise": a["tainted_paise"],
                          "balance_paise": a["balance_paise"], "branch_end": a.get("branch_end"),
                          "label": prop.get(a["account_id"], {}).get("label"),
                          "action": prop.get(a["account_id"], {}).get("proposed_action"),
                          "approved": d.get("approved"), "seen_before": a["account_id"] in others})
        return JSONResponse({"nodes": nodes, "edges": list(edges.values())})

    def chat_ask(pp, qp, b):
        mem = ctx.memory(str(b.get("bank_id", "")))
        inv_id, text = b.get("investigation_id"), str(b.get("text", "")).strip()
        if not store.get_investigation(mem, inv_id):
            return _err(404, "not_found", f"no investigation {inv_id}")
        if not 3 <= len(text) <= 1000:
            return _err(422, "bad_input", "a question is 3 to 1000 characters")
        mid = store.add_message(mem, inv_id, "human", text, "pending", author="analyst")
        return JSONResponse({"message_id": mid})

    def chat_read(pp, qp, b):
        mem = ctx.memory(pp["bank"])
        return JSONResponse(store.messages(mem, pp["inv"]))

    def chat_claim(pp, qp, b):
        mem = ctx.memory(str(b.get("bank_id", "")))
        mid, inv_id = store.claim_question(mem)
        if not mid:
            return JSONResponse({})
        inv = store.get_investigation(mem, inv_id)
        return JSONResponse({"message_id": mid, "investigation_id": inv_id, "trace_id": inv["trace_id"],
                             "complaint_id": inv["complaint_id"], "question": store.get_message(mem, mid)["text"],
                             "context": chat_context(ctx, mem, inv_id)})

    def chat_answer(pp, qp, b):
        mem = ctx.memory(str(b.get("bank_id", "")))
        q = store.get_message(mem, str(b.get("message_id")))
        if not q or q["role"] != "human" or q["status"] != "answering":
            return _err(409, "wrong_state", "no question is waiting for this answer")
        answer = str(b.get("answer", "")).strip()[:4000]
        cited = [str(x)[:80] for x in (b.get("cited_ids") or [])][:40]
        if not answer:
            return _err(422, "bad_input", "an empty answer")
        missing = unknown_ids(ctx, mem, answer, cited)
        if missing and not b.get("final"):
            return JSONResponse({"accepted": False, "unknown_ids": missing}, status_code=422)
        status = "rejected" if missing else "answer"
        note = f"rejected: cites IDs that do not exist: {', '.join(missing[:10])}" if missing else ""
        aid = store.add_message(mem, q["investigation_id"], "verifier", answer, status, cited_ids=cited,
                                reply_to=q["id"], author="verifier_agent", note=note)
        store.set_message(mem, q["id"], status="rejected" if missing else "answered")
        return JSONResponse({"accepted": not missing, "message_id": aid, "unknown_ids": missing})

    def spend_get(pp, qp, b):
        return JSONResponse(spend.totals(qp.get("investigation_id")))

    def spend_post(pp, qp, b):
        return JSONResponse(spend.record(b))

    def spend_calls(pp, qp, b):
        """The ledger's rows, newest first: one per model call (the UI's cost charts)."""
        return JSONResponse(spend.calls(qp.get("investigation_id"), int(qp.get("limit", 5000))))

    def graph_activity(pp, qp, b):
        """The queries this tool server ran on FalkorDB, newest last (graph/client.py, activity)."""
        return JSONResponse(client.activity(int(qp.get("since", 0)), int(qp.get("limit", 200)), qp.get("source") or None,
                                            qp.get("investigation_id") or None))

    def graph_stats(pp, qp, b):
        """Node and relationship counts of the network graph and the given banks' memory graphs (cached 10 min)."""
        banks = [x for x in str(qp.get("banks", "")).split(",") if x][:10]
        now = time.time()
        if now - _stats.get("at", 0) > 600 or _stats.get("banks") != banks:
            net = {}
            for lab in [r[0] for r in ctx.net.rows("CALL db.labels()")]:
                net[lab] = ctx.net.value(f"MATCH (n:`{lab}`) RETURN count(n)", timeout_ms=30_000)
            for rel in [r[0] for r in ctx.net.rows("CALL db.relationshipTypes()")]:
                net[rel] = ctx.net.value(f"MATCH ()-[r:`{rel}`]->() RETURN count(r)", timeout_ms=30_000)
            _stats.update({"at": now, "banks": banks, "network": net,
                           "memory": {bk: store.counts(ctx.memory(bk)) for bk in banks},
                           "graphs": client.graph_names(ctx.db)})
        return JSONResponse({k: _stats[k] for k in ("network", "memory", "graphs", "at")})

    _stats = {}
    return [
        Route("/handoff/fail", _handler(fail), methods=["POST"]),
        Route("/handoff/close", _handler(close), methods=["POST"]),
        Route("/handoff/complaints", _handler(complaints), methods=["GET"]),
        Route("/handoff/banks", _handler(banks), methods=["GET"]),
        Route("/handoff/spend", _handler(spend_get), methods=["GET"]),
        Route("/handoff/spend", _handler(spend_post), methods=["POST"]),
        Route("/handoff/spend/calls", _handler(spend_calls), methods=["GET"]),
        Route("/handoff/graph/activity", _handler(graph_activity), methods=["GET"]),
        Route("/handoff/graph/stats", _handler(graph_stats), methods=["GET"]),
        Route("/handoff/investigations", _handler(start), methods=["POST"]),
        Route("/handoff/investigations", _handler(list_inv), methods=["GET"]),
        Route("/handoff/investigations/{bank}/{inv}", _handler(detail), methods=["GET"]),
        Route("/handoff/claim", _handler(claim), methods=["POST"]),
        Route("/handoff/submit", _handler(submit), methods=["POST"]),
        Route("/handoff/read/{bank}/{inv}/{kind}", _handler(read), methods=["GET"]),
        Route("/handoff/step", _handler(step), methods=["POST"]),
        Route("/handoff/gate/decide", _handler(gate_decide), methods=["POST"]),
        Route("/handoff/gate/{bank}/{inv}", _handler(gate_read), methods=["GET"]),
        Route("/handoff/gate/record", _handler(gate_record), methods=["POST"]),
        Route("/handoff/gate/execute", _handler(gate_execute), methods=["POST"]),
        Route("/handoff/gate/finish", _handler(gate_finish), methods=["POST"]),
        Route("/handoff/actions/undo", _handler(undo), methods=["POST"]),
        Route("/handoff/reviews", _handler(review_list), methods=["GET"]),
        Route("/handoff/reviews/resolve", _handler(review_resolve), methods=["POST"]),
        Route("/handoff/memory", _handler(memory_view), methods=["GET"]),
        Route("/handoff/trail/{bank}/{inv}", _handler(trail), methods=["GET"]),
        Route("/handoff/chat/ask", _handler(chat_ask), methods=["POST"]),
        Route("/handoff/chat/claim", _handler(chat_claim), methods=["POST"]),
        Route("/handoff/chat/answer", _handler(chat_answer), methods=["POST"]),
        Route("/handoff/chat/{bank}/{inv}", _handler(chat_read), methods=["GET"]),
    ]
