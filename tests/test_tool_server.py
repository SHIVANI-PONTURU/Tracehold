"""Phase 2 steps 3-5 on a running tool server: each MCP path lists only its agent's tools, and one complaint goes
through the handoff API's state machine, with the deterministic checks catching a wrong amount and passing the
corrected retry, then the gate placing the lien through the mock bank API."""
import asyncio
import socket
import threading
import time

import pytest

from handoff import contracts as C
from mcp_server import tools as T
from memory import store

pytestmark = [pytest.mark.integration, pytest.mark.data]
BANK, COMPLAINT = "11", "CMP-0011"
TRAIL = ["20-800283B60", "10-8001A6140", "22-800202780", "10-800119810", "12-80010D740", "20-8001B0FA0",
         "23-8002A0E00", "220-800115860"]


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _serve(app, port):
    import uvicorn
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    threading.Thread(target=server.run, daemon=True).start()
    import httpx2
    for _ in range(100):
        try:
            if httpx2.get(f"http://127.0.0.1:{port}/health", timeout=1).status_code == 200:
                return server
        except Exception:
            time.sleep(0.1)
    raise RuntimeError("server did not start")


@pytest.fixture(scope="module")
def servers(tool_ctx, tmp_path_factory):
    from mcp_server.server import create_app
    bank = None
    try:
        from bank_api.app import create_app as bank_app
        bport = _free_port()
        bank = _serve(bank_app(str(tmp_path_factory.mktemp("bank") / "bank.sqlite")), bport)
        tool_ctx.bank_api_url = f"http://127.0.0.1:{bport}"
    except ImportError:
        pass
    port = _free_port()
    srv = _serve(create_app(tool_ctx), port)
    yield f"http://127.0.0.1:{port}", bank is not None
    srv.should_exit = True
    if bank:
        bank.should_exit = True


def test_each_mcp_path_lists_only_its_agents_tools(servers):
    from mcp.client.session import ClientSession
    from mcp.client.streamable_http import streamable_http_client
    base, _ = servers

    async def listing(agent):
        async with streamable_http_client(f"{base}/mcp/{agent}/") as streams:
            async with ClientSession(streams[0], streams[1]) as s:
                await s.initialize()
                return sorted(t.name for t in (await s.list_tools()).tools)
    for agent, names in T.SCOPES.items():
        assert asyncio.run(listing(agent)) == sorted(names)
    assert "propose_lien" not in asyncio.run(listing("tracer"))


def test_one_complaint_through_the_handoff_api(servers, tool_ctx):
    import httpx2
    base, have_bank = servers
    http = httpx2.Client(base_url=base, timeout=60)
    mem = tool_ctx.memory(BANK)
    for i in store.list_investigations(mem):                 # leave only this test's run waiting
        store.move(mem, i["id"], ["new", "traced", "checks_failed", "proposed"], "closed")

    assert http.post("/handoff/investigations", json={"bank_id": "12", "complaint_id": COMPLAINT}).status_code == 403
    inv = http.post("/handoff/investigations", json={"bank_id": BANK, "complaint_id": COMPLAINT}).json()["investigation_id"]
    assert http.post("/handoff/claim", json={"bank_id": BANK, "agent": "tracer"}).json()["investigation_id"] == inv
    assert http.post("/handoff/claim", json={"bank_id": BANK, "agent": "tracer"}).json() == {}

    res = T.run_tool(T.compute_taint, tool_ctx, BANK, inv, TRAIL)
    trace = C.TraceHandoff(investigation_id=inv, complaint_id=COMPLAINT, disputed_paise=res["disputed_paise"],
                           trail=[C.TraceLayer(layer=1, account_ids=[TRAIL[0]], transfer_ids=["T000418368"])],
                           accounts=[C.TracedAccount(account_id=a["account_id"], layer=a["layer"] or 1,
                                                     tainted_paise=a["tainted_paise"], balance_paise=a["balance_paise"])
                                     for a in res["accounts"]],
                           unrecoverable_paise=res["unrecoverable_paise"],
                           not_followed_paise=res["not_followed_total_paise"], trace_id=inv).model_dump(mode="json")
    bad = http.post("/handoff/submit", json={"bank_id": BANK, "kind": "trace", "payload": {**trace, "notes": "x"}})
    assert bad.status_code == 422 and bad.json()["error"]["code"] == "invalid_handoff"
    assert http.post("/handoff/submit", json={"bank_id": BANK, "kind": "trace", "payload": trace}).json()["status"] == "traced"

    assert http.post("/handoff/claim", json={"bank_id": BANK, "agent": "adjudicator"}).json()["investigation_id"] == inv
    lien = T.run_tool(T.propose_lien, tool_ctx, BANK, inv, "20-800283B60", f"DEC-{inv[4:]}-1", "mule",
                      "Passes on far more than it receives.")
    others = [{"account_id": a, "label": "uncertain", "proposed_action": "review",
               "evidence": {"transfer_ids": ["T000418370"], "signals": ["account_age_days", "distinct_payers"]},
               "rationale": "Old account with few payers; needs a closer look."} for a in TRAIL[1:3]]

    def proposal(amount, attempt):
        return {"investigation_id": inv, "attempt": attempt, "trace_id": inv, "accounts": [
            {"account_id": "20-800283B60", "label": "mule", "proposed_action": "lien",
             "lien_proposal_id": lien["decision_id"], "lien_amount_paise": amount,
             "evidence": {"transfer_ids": ["T000418368", "T000418370"],
                          "signals": ["pass_through_ratio", "median_minutes_to_forward"]},
             "rationale": "Passes on far more than it receives, within hours."}] + others}

    wrong = http.post("/handoff/submit", json={"bank_id": BANK, "kind": "proposal",
                                               "payload": proposal(lien["lien_amount_paise"] + 1, 1)}).json()
    assert wrong["status"] == "checks_failed"
    assert [f["check"] for f in wrong["checks"]["failed"]] == [2]
    note = http.get(f"/handoff/read/{BANK}/{inv}/check_failure").json()
    assert note["payload"]["failed"] == [{"check": 2, "account_ids": ["20-800283B60"]}]

    assert http.post("/handoff/claim", json={"bank_id": BANK, "agent": "adjudicator"}).json()["claimed_from"] == "checks_failed"
    fixed = http.post("/handoff/submit", json={"bank_id": BANK, "kind": "proposal",
                                               "payload": proposal(lien["lien_amount_paise"], 2)}).json()
    assert fixed["status"] == "proposed" and fixed["checks"]["passed"], fixed

    assert http.post("/handoff/claim", json={"bank_id": BANK, "agent": "verifier"}).json()["investigation_id"] == inv
    verdict = {"investigation_id": inv, "trace_id": inv, "verdicts": [
        {"decision_id": lien["decision_id"], "verdict": "approve", "reason": "Signals are specific to this account.",
         "decided_by": "verifier_agent"}]}
    assert http.post("/handoff/submit", json={"bank_id": BANK, "kind": "verdict", "payload": verdict}).json()["status"] == "at_gate"

    # the gate: refused before it is recorded, placed after
    refused = http.post("/handoff/gate/execute", json={"bank_id": BANK, "investigation_id": inv,
                                                       "decision_id": lien["decision_id"]})
    assert refused.status_code == 409 and refused.json()["error"]["code"] == "refused_not_approved"
    rec = http.post("/handoff/gate/record", json={"bank_id": BANK, "investigation_id": inv,
                                                  "decision_id": lien["decision_id"], "approved": True,
                                                  "decided_by": "verifier_agent", "gate_mode": "placeholder",
                                                  "reason": "Verifier approved; no override."}).json()
    assert rec["recorded"] and rec["decision"]["approved"] is True
    if not have_bank:
        pytest.skip("fastapi is not installed: the mock bank API part is skipped")
    placed = http.post("/handoff/gate/execute", json={"bank_id": BANK, "investigation_id": inv,
                                                      "decision_id": lien["decision_id"]}).json()
    assert placed["already_executed"] is False and placed["amount_paise"] == lien["lien_amount_paise"]
    again = http.post("/handoff/gate/execute", json={"bank_id": BANK, "investigation_id": inv,
                                                     "decision_id": lien["decision_id"]}).json()
    assert again["already_executed"] is True and again["lien_id"] == placed["lien_id"]
    done = http.post("/handoff/gate/finish", json={"bank_id": BANK, "investigation_id": inv}).json()
    assert done["status"] == "executed" and len(done["liens"]) == 1
