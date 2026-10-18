"""Phase 2 steps 7-10 with a scripted model (no tokens spent): complaint 1 goes Tracer -> Adjudicator -> checks ->
Verifier -> HITL gate -> executed liens on the running tool server and mock bank.

- Tracer: tainted amounts for two accounts match a hand calculation (the engine on a plain manual read).
- Adjudicator: no amount in its output comes from the model; a deliberately wrong claim (a signal that is not in
  the account's record) is caught by the checks and fixed on the retry.
- Verifier: the run pauses at the gate, survives a restart (a new worker on the same checkpoint file) and resumes.
"""
import re

import pytest

pytest.importorskip("langgraph")
pytest.importorskip("fastapi")

from agents import worker                                   # noqa: E402
from agents.common import clients, model as M               # noqa: E402
from agents.common.config import load                       # noqa: E402
from mcp_server import network                              # noqa: E402
from taint import engine                                    # noqa: E402
from tests.test_tool_server import servers                  # noqa: E402,F401  (fixture: tool server + mock bank)

pytestmark = [pytest.mark.integration, pytest.mark.data]
BANK, COMPLAINT = "11", "CMP-0011"
ACC = re.compile(r"^([0-9]{1,6}-[0-9A-Z]{9}) \|", re.M)
DEC = re.compile(r"^(DEC-[0-9A-Za-z-]+) \|", re.M)


def text(messages):
    return "\n".join(m[1] if isinstance(m, tuple) else m.content for m in messages)


def tracer_answer(messages):
    return {"calls": [{"account_id": a, "decision": "follow", "reason": "passes money on"}
                      for a in ACC.findall(text(messages))]}


def adjudicator_answer(wrong):
    def answer(messages):
        calls = []
        for i, a in enumerate(ACC.findall(text(messages))):
            sig = ["account_age_days", "distinct_payers"]
            if wrong and i == 0:
                sig = ["account_age_days", "favourite_colour"]          # not a signal in any record: check 6
            calls.append({"account_id": a, "label": "mule" if i < 2 else "uncertain",
                          "proposed_action": "lien" if i < 2 else "review_and_request_info", "signals": sig,
                          "rationale": "Receives the stolen money and passes most of it on.",
                          "lessons_applied": [], "flag_entity_ids": [], "info_request": None})
        return {"accounts": calls}
    return answer


def verifier_answer(messages):
    return {"verdicts": [{"decision_id": d, "verdict": "approve", "reason": "The evidence is specific to this account."}
                         for d in DEC.findall(text(messages))]}


@pytest.fixture(scope="module")
def setup(servers, tool_ctx, tmp_path_factory):                       # noqa: F811
    base, have_bank = servers
    if not have_bank:
        pytest.skip("the mock bank API needs fastapi")
    cfg = load()
    cfg["tool_server_url"] = base
    cfg["verifier"]["checkpoint_db"] = str(tmp_path_factory.mktemp("gate") / "checkpoints.sqlite")
    handoff = clients.HandoffClient(base, BANK)
    mem = tool_ctx.memory(BANK)
    from memory import store
    for i in store.list_investigations(mem):
        store.move(mem, i["id"], [s for s in store.STATUSES if s not in ("closed", "failed", "executed")], "closed")
    job = handoff.start(COMPLAINT)
    return cfg, handoff, job


def test_complaint_1_end_to_end_with_a_scripted_model(setup, tool_ctx):
    cfg, handoff, started = setup
    inv = started["investigation_id"]

    # Tracer
    job = handoff.claim("tracer")
    assert job["investigation_id"] == inv
    r = worker.run_tracer(cfg, handoff, job, model=M.FakeModel([tracer_answer] * 20))
    assert r["status"] == "traced"
    trace = handoff.read(inv, "trace")["payload"]
    held = {a["account_id"]: a for a in trace["accounts"]}
    assert sum(a["tainted_paise"] for a in trace["accounts"]) + trace["unrecoverable_paise"] + \
        trace["not_followed_paise"] == trace["disputed_paise"]

    # two accounts against a hand calculation: the engine on a plain read of the traced accounts' transfers
    comp = network.complaint(tool_ctx.net, COMPLAINT)
    fraud = network.transfers_by_id(tool_ctx.net, comp["fraud_transfer_ids"])
    opening, transfers = {}, []
    for a in held:
        opening[a] = tool_ctx.net.value("MATCH (x:Account {id: $id}) RETURN x.opening_balance", {"id": a})
        for q in ("MATCH (x:Account {id: $id})-[t:TRANSFERRED]->(y) RETURN t.id, x.id, y.id, t.amount, t.ts, t.format, t.self_loop",
                  "MATCH (y)-[t:TRANSFERRED]->(x:Account {id: $id}) RETURN t.id, y.id, x.id, t.amount, t.ts, t.format, t.self_loop"):
            transfers += [engine.Transfer(*row[:5], row[5], row[6]) for row in tool_ctx.net.rows(q, {"id": a})]
    hand = engine.compute(fraud, opening, transfers)
    top_two = sorted(held, key=lambda a: -held[a]["tainted_paise"])[:2]
    for a in top_two:
        assert held[a]["tainted_paise"] == hand.accounts[a].tainted
        assert held[a]["balance_paise"] == hand.accounts[a].balance

    # Adjudicator: the first proposal cites a signal that is not in the record, so check 6 fails; the retry is fixed
    job = handoff.claim("adjudicator")
    fake = M.FakeModel([adjudicator_answer(True), adjudicator_answer(False)])
    r = worker.run_adjudicator(cfg, handoff, job, model=fake)
    assert r["status"] == "proposed" and r["checks"]["passed"], r
    note = handoff.read(inv, "check_failure")["payload"]
    assert [f["check"] for f in note["failed"]] == [6]
    assert "check 6" in text(fake.prompts[-1][1])          # the retry prompt names the failed check
    proposal = handoff.read(inv, "proposal")["payload"]
    assert proposal["attempt"] == 2
    detail = handoff.detail(inv)
    decided = {d["id"]: d for d in detail["decisions"]}
    liens = [a for a in proposal["accounts"] if a["proposed_action"] == "lien"]
    assert liens
    for a in liens:                                       # every amount is code's: equal to the Decision's
        assert a["lien_amount_paise"] == decided[a["lien_proposal_id"]]["lien_amount"]
    from agents.adjudicator.graph import AccountCall
    assert not any("amount" in f for f in AccountCall.model_fields)   # the model cannot even state one

    # Verifier: pause at the gate, "restart", decide at the gate, resume, liens executed
    job = handoff.claim("verifier")
    v1 = worker.Verifier(cfg, handoff, model_factory=lambda j: M.FakeModel([verifier_answer]))
    out = v1.start(job)
    assert "__interrupt__" in out
    assert handoff.detail(inv)["investigation"]["status"] == "at_gate"
    assert [p["id"] for p in v1.paused()] == [inv]
    del v1                                                # the worker process stops here

    v2 = worker.Verifier(cfg, handoff)                    # a new worker on the same checkpoint file
    assert v2.resume_ready() == []                        # no gate decision yet: still paused
    handoff.gate_decide(inv, [], "placeholder")           # the human clicks Approve
    done = v2.resume_ready()
    assert done == [{"investigation_id": inv, "status": "executed", "how": "resumed"}]
    detail = handoff.detail(inv)
    assert detail["investigation"]["status"] == "executed"
    assert {l["decision_id"] for l in detail["liens"]} == {a["lien_proposal_id"] for a in liens}
    for d in detail["decisions"]:
        if d["id"] in {a["lien_proposal_id"] for a in liens}:
            assert d["approved"] is True and d["decided_by"] == "verifier_agent" and d["gate_mode"] == "placeholder"
