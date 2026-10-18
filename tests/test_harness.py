"""Phase 3 steps 1, 3 and 10 on the running tool server and mock bank: a worker stopped mid-run resumes with no
duplicate actions, an expired claim is taken back, automatic actions can be undone, the review queue resolves, the
case chat rejects an answer citing an ID that does not exist and keeps the conversation with the case."""
import re

import httpx2
import pytest

pytest.importorskip("langgraph")
pytest.importorskip("fastapi")

from agents import worker                                   # noqa: E402
from agents.adjudicator import graph as adj                 # noqa: E402
from agents.common import clients, model as M               # noqa: E402
from agents.common.config import load                       # noqa: E402
from memory import store                                    # noqa: E402
from tests.test_tool_server import servers                  # noqa: E402,F401  (fixture: tool server + mock bank)

pytestmark = [pytest.mark.integration, pytest.mark.data]
BANK, COMPLAINT, ACCOUNT = "11", "CMP-0184", "23-80017F180"     # one traced account holding all the stolen money
ACC = re.compile(r"^([0-9]{1,6}-[0-9A-Z]{9}) \|", re.M)


def text(messages):
    return "\n".join(m[1] if isinstance(m, tuple) else m.content for m in messages)


def follow(messages):
    return {"calls": [{"account_id": a, "decision": "follow", "reason": "passes money on"} for a in ACC.findall(text(messages))]}


def uncertain(messages):
    return {"accounts": [{"account_id": a, "label": "uncertain", "proposed_action": "review_and_request_info",
                          "signals": ["account_age_days", "distinct_payers"], "rationale": "Signals point both ways.",
                          "lessons_applied": [], "flag_entity_ids": [], "info_request": "KYC and device history"}
                         for a in ACC.findall(text(messages))]}


class Killed(RuntimeError):
    """The worker process dying mid-run (called without the worker loop, so nothing marks the run failed)."""


class DiesAtRequestInfo(clients.ToolClient):
    def call(self, tool, **args):
        if tool == "request_info":
            raise Killed()
        return super().call(tool, **args)


@pytest.fixture(scope="module")
def setup(servers, tool_ctx):                                # noqa: F811
    base, have_bank = servers
    if not have_bank:
        pytest.skip("the mock bank API needs fastapi")
    cfg = load()
    cfg["tool_server_url"] = base
    # CMP-0184's one account holds the whole loss, so the concentration rule (Phase 4) would lien it; these tests
    # need the review and the information request that an uncertain label gets, to kill the worker between them.
    cfg["adjudicator"]["lien_whatever_label_part_of_disputed"] = None
    mem = tool_ctx.memory(BANK)
    for i in store.list_investigations(mem):
        if i["status"] not in ("closed", "failed", "executed"):
            store.move(mem, i["id"], [i["status"]], "closed", closed_reason="test cleanup")
    handoff = clients.HandoffClient(base, BANK)
    started = handoff.start(COMPLAINT)
    job = handoff.claim("tracer")
    worker.run_tracer(cfg, handoff, job, model=M.FakeModel([follow] * 10))
    return cfg, handoff, started["investigation_id"], base, tool_ctx.bank_api_url


def test_a_stopped_adjudicator_resumes_with_no_duplicate_actions(setup, tool_ctx):
    cfg, handoff, inv, base, bank_url = setup
    job = handoff.claim("adjudicator")
    assert job["investigation_id"] == inv
    tools = DiesAtRequestInfo(base, "adjudicator", BANK, inv)
    with pytest.raises(Killed):                              # it opened the review, then died before the request
        adj.run(cfg, tools, handoff, M.FakeModel([uncertain]), BANK, inv, job["trace_id"])
    assert handoff.detail(inv)["investigation"]["status"] == "adjudicating"
    assert [a["kind"] for a in handoff.detail(inv)["actions"]] == ["review"]

    again = handoff.claim("adjudicator", resume=True)         # the restarted worker takes its own work back
    assert again["investigation_id"] == inv and again["resumed"] is True
    r = worker.run_adjudicator(cfg, handoff, again, model=M.FakeModel([uncertain]))
    assert r["status"] == "proposed"
    detail = handoff.detail(inv)
    kinds = sorted((a["kind"], a["entity_id"]) for a in detail["actions"])
    assert kinds == [("info_request", ACCOUNT), ("review", ACCOUNT)]          # one each: nothing done twice
    sent = httpx2.get(f"{bank_url}/info-requests", params={"investigation_id": inv}).json()
    assert len(sent) == 1
    assert detail["investigation"]["reclaims"] >= 1
    agents_steps = [s["agent"] for s in detail["steps"]]
    assert agents_steps.count("adjudicator") >= 5 and agents_steps.count("tracer") >= 3


def test_one_step_per_node_for_the_tracer(setup):
    cfg, handoff, inv, base, _ = setup
    tracer = [s for s in handoff.detail(inv)["steps"] if s["agent"] == "tracer"]
    nodes = [s["node"] for s in tracer]
    assert nodes[0] == "recall" and nodes[1] == "plan" and nodes[-1] == "finish"
    assert nodes.count("expand") == nodes.count("judge") + 1                 # the loop: expand, judge, ..., expand
    assert "recall_context" in tracer[0]["tool"] and all(s["latency_ms"] >= 0 for s in tracer)


def test_undo_withdraws_the_request_and_the_review_queue_resolves(setup):
    cfg, handoff, inv, base, bank_url = setup
    acts = {a["kind"]: a for a in handoff.detail(inv)["actions"]}
    r = handoff.undo(inv, acts["info_request"]["id"], "sent to the wrong bank")
    assert r["status"] == "undone" and r["bank"]["status"] == "withdrawn"
    assert handoff.undo(inv, acts["info_request"]["id"], "again")["already_undone"] is True
    sent = httpx2.get(f"{bank_url}/info-requests", params={"investigation_id": inv}).json()
    assert sent[0]["status"] == "withdrawn"
    queue = handoff.reviews("open")
    assert acts["review"]["id"] in {q["id"] for q in queue}
    done = handoff.resolve_review(acts["review"]["id"], "cleared", "Old salary account; payer is the employer.")
    assert done["resolved"] and done["review"]["status"] == "cleared"
    with pytest.raises(clients.HandoffError):
        handoff.undo(inv, "ACT-does-not-exist", "x")


def test_case_chat_rejects_unknown_ids_and_keeps_the_conversation(setup):
    cfg, handoff, inv, base, _ = setup
    handoff.chat_ask(inv, "Which transfer brought the money to the account?")
    q = handoff.chat_claim()
    assert q["investigation_id"] == inv and q["context"]["trace"]["accounts"][0]["account_id"] == ACCOUNT
    bad = handoff.chat_answer(q["message_id"], "It came in T999999999 to 23-80017F180.", ["T999999999"])
    assert bad["accepted"] is False and bad["unknown_ids"] == ["T999999999"]
    final = handoff.chat_answer(q["message_id"], "It came in T999999999.", ["T999999999"], final=True)
    assert final["accepted"] is False
    handoff.chat_ask(inv, "And which account holds it now?")
    q2 = handoff.chat_claim()
    good = handoff.chat_answer(q2["message_id"], f"{ACCOUNT} holds it, per {q2['context']['claims'][0]['id']}.",
                               [ACCOUNT])
    assert good["accepted"] is True
    reopened = clients.HandoffClient(base, BANK).chat(inv)                  # a new session reads the same case
    assert [(m["role"], m["status"]) for m in reopened] == [("human", "rejected"), ("verifier", "rejected"),
                                                           ("human", "answered"), ("verifier", "answer")]


def test_an_expired_claim_is_taken_back(setup, tool_ctx):
    cfg, handoff, inv, base, _ = setup
    other = handoff.start("CMP-0308")["investigation_id"]
    assert handoff.claim("tracer")["investigation_id"] == other
    assert handoff.claim("tracer") == {}                                     # its lease is running
    tool_ctx.memory(BANK).query("MATCH (i:Investigation {id: $id}) SET i.lease_until = 0", {"id": other})
    back = handoff.claim("tracer")
    assert back["investigation_id"] == other and back["resumed"] is True
    handoff.close(other, "test over")
    assert handoff.detail(other)["investigation"]["status"] == "closed"
