"""Phase 3 step 14 with a scripted model (no tokens spent), at bank 20: complaint 1 (CMP-0283), then complaint 2
(CMP-0354), whose first layer reaches the same account (23-8001B8810) and, through a shared phone, a second one.

- Complaint 1: the human rejects the lien on 23-8001B8810 at the gate with a reason: a "do not lien" lesson.
- Complaint 2: the Tracer's first step (recall) finds complaint 1; its table shows the memory. The lesson reaches the
  Adjudicator's table for both linked accounts; following it turns 23-8001B8810's lien into a review (the rubric
  alone would lien it). The Verifier rejects the lien on the phone-sharing account citing the lesson; at the gate
  that rejection stands, which confirms the lesson: its confidence rises.
The scripted answers stand in for the model's judgement; the live run checks what Claude decides.
"""
import re

import pytest

pytest.importorskip("langgraph")
pytest.importorskip("fastapi")

from agents import worker                                   # noqa: E402
from agents.common import clients, model as M               # noqa: E402
from agents.common.config import load                       # noqa: E402
from memory import store                                    # noqa: E402
from tests.test_tool_server import servers                  # noqa: E402,F401  (fixture: tool server + mock bank)

pytestmark = [pytest.mark.integration, pytest.mark.data]
BANK, C1, C2 = "20", "CMP-0283", "CMP-0354"
SHARED, PHONE_SHARER = "23-8001B8810", "12-800063990"
ROW = re.compile(r"^([0-9]{1,6}-[0-9A-Z]{9}) \|(.*)$", re.M)
DEC = re.compile(r"^(DEC-[0-9A-Za-z-]+) \| ([0-9]{1,6}-[0-9A-Z]{9}) \|(.*)$", re.M)
LESSON = re.compile(r"\b(LES-[0-9A-Za-z-]+) \[")


def text(messages):
    return "\n".join(m[1] if isinstance(m, tuple) else m.content for m in messages)


def follow(messages):
    return {"calls": [{"account_id": a, "decision": "follow", "reason": "passes money on"}
                      for a, _ in ROW.findall(text(messages))]}


def adjudicate(follow_lessons_for=()):
    def answer(messages):
        calls = []
        for a, row in ROW.findall(text(messages)):
            cited = LESSON.findall(row.split("| lessons:")[-1]) if a in follow_lessons_for else []
            calls.append({"account_id": a, "label": "likely_genuine", "proposed_action": "review" if cited else "lien",
                          "signals": ["account_age_days", "distinct_payers"],
                          "rationale": "Follows the lesson for this account." if cited else "Received the money directly.",
                          "lessons_applied": cited, "flag_entity_ids": [], "info_request": None})
        return {"accounts": calls}
    return answer


def verify(reject=()):
    def answer(messages):
        out = []
        for d, acc, row in DEC.findall(text(messages)):
            lessons = LESSON.findall(row.split("| lessons that apply:")[-1])
            if acc in reject:
                out.append({"decision_id": d, "verdict": "reject", "reason": "A lesson at this bank applies here.",
                            "lessons_cited": lessons})
            else:
                out.append({"decision_id": d, "verdict": "approve", "reason": "The evidence is specific."})
        return {"verdicts": out}
    return answer


def run_case(cfg, handoff, complaint, adjudicator, verifier, gate, tracer_model=None):
    inv = handoff.start(complaint)["investigation_id"]
    job = handoff.claim("tracer")
    assert job["investigation_id"] == inv
    tracer_model = tracer_model or M.FakeModel([follow] * 10)
    assert worker.run_tracer(cfg, handoff, job, model=tracer_model)["status"] == "traced"
    job = handoff.claim("adjudicator")
    adj_model = M.FakeModel([adjudicator])
    assert worker.run_adjudicator(cfg, handoff, job, model=adj_model)["status"] == "proposed"
    job = handoff.claim("verifier")
    v = worker.Verifier(cfg, handoff, model_factory=lambda j: M.FakeModel([verifier]))
    v.start(job)
    detail = handoff.detail(inv)
    decisions = gate(detail)
    handoff.gate_decide(inv, decisions, "placeholder")
    assert v.resume_ready()[0]["status"] in ("executed", "closed")
    return inv, tracer_model, adj_model


@pytest.fixture(scope="module")
def demo(servers, tool_ctx, tmp_path_factory):              # noqa: F811
    base, have_bank = servers
    if not have_bank:
        pytest.skip("the mock bank API needs fastapi")
    cfg = load()
    cfg["tool_server_url"] = base
    cfg["verifier"]["checkpoint_db"] = str(tmp_path_factory.mktemp("gate") / "checkpoints.sqlite")
    mem = tool_ctx.memory(BANK)
    mem.query("MATCH (n) DETACH DELETE n")                    # a fresh memory for bank 20
    handoff = clients.HandoffClient(base, BANK)

    def reject_shared(detail):
        did = next(d["id"] for d in detail["decisions"] if d["account_id"] == SHARED)
        return [{"decision_id": did, "approve": False,
                 "reason": "The holder showed the money was the sale of a flat; ask for documents, do not lien."}]
    inv1, _, _ = run_case(cfg, handoff, C1, adjudicate(), verify(), reject_shared)
    lesson_id = f"LES-{inv1[4:]}-a1-{SHARED}"
    before = store.get_lesson(mem, lesson_id)
    inv2, tracer2, adj2 = run_case(cfg, handoff, C2, adjudicate(follow_lessons_for=(SHARED,)),
                                   verify(reject=(PHONE_SHARER,)), lambda d: [])
    return {"handoff": handoff, "mem": mem, "inv1": inv1, "inv2": inv2, "lesson_id": lesson_id, "before": before,
            "tracer2": tracer2, "adj2": adj2}


def test_complaint_1_leaves_decisions_and_a_lesson(demo):
    d1 = demo["handoff"].detail(demo["inv1"])
    shared = next(d for d in d1["decisions"] if d["account_id"] == SHARED)
    assert shared["approved"] is False and shared["decided_by"] == "human" and shared["gate_mode"] == "placeholder"
    assert all(d["decided_by"] == "verifier_agent" for d in d1["decisions"] if d["account_id"] != SHARED)
    l = demo["before"]
    assert l["stance"] == "release" and l["source"] == "human" and l["confidence"] == 0.7
    assert f"account:{SHARED}" in l["applies_to"] and "phone:PH-0167084" in l["applies_to"]


def test_complaint_2s_first_step_recalls_complaint_1(demo):
    steps = demo["handoff"].detail(demo["inv2"])["steps"]
    assert steps[0]["agent"] == "tracer" and steps[0]["node"] == "recall" and "recall_context" in steps[0]["tool"]
    judge = text(demo["tracer2"].prompts[0][1])
    shared_row = next(r for a, r in ROW.findall(judge) if a == SHARED)
    phone_row = next(r for a, r in ROW.findall(judge) if a == PHONE_SHARER)
    assert f"memory: {C1} ({demo['inv1']}): same account; decided likely_genuine -> lien, rejected by human" in shared_row
    assert f"shares phone PH-0167084 with {SHARED}" in phone_row


def test_the_lesson_changes_a_lien(demo):
    adj_prompt = text(demo["adj2"].prompts[0][1])
    for acc in (SHARED, PHONE_SHARER):
        row = next(r for a, r in ROW.findall(adj_prompt) if a == acc)
        assert demo["lesson_id"] in row                                   # the lesson reached the table for both
    prop = demo["handoff"].read(demo["inv2"], "proposal")["payload"]
    by = {a["account_id"]: a for a in prop["accounts"]}
    assert by[SHARED]["proposed_action"] == "review" and by[SHARED]["lessons_applied"] == [demo["lesson_id"]]
    assert by[PHONE_SHARER]["proposed_action"] == "lien"                  # not followed by the Adjudicator ...
    assert all(by[a]["proposed_action"] == "lien" for a in by if a != SHARED)   # the rubric liens the rest
    detail = demo["handoff"].detail(demo["inv2"])
    assert not any(l["account_id"] == SHARED for l in detail["liens"])    # no lien where complaint 1 lifted one
    verdict = demo["handoff"].read(demo["inv2"], "verdict")["payload"]
    phone = next(v for v in verdict["verdicts"] if by[PHONE_SHARER]["lien_proposal_id"] == v["decision_id"])
    assert phone["verdict"] == "reject" and demo["lesson_id"] in phone["lessons_cited"]   # ... but by the Verifier


def test_lesson_confidence_rises_after_the_confirming_decision(demo):
    after = store.get_lesson(demo["mem"], demo["lesson_id"])
    assert after["confidence"] == pytest.approx(demo["before"]["confidence"] + 0.1)
    assert after["times_confirmed"] == 1 and after["history"][-1]["event"] == "confirmed"
    made = demo["handoff"].detail(demo["inv2"])["lessons_made"]
    assert [l["stance"] for l in made] == ["release"] and made[0]["source"] == "verifier_agent"


def test_switching_bank_shows_separate_memory(demo, servers):              # noqa: F811
    base, _ = servers
    m20 = demo["handoff"].memory()
    assert demo["lesson_id"] in {l["id"] for l in m20["lessons"]}
    for other in ("11", "12"):
        m = clients.HandoffClient(base, other).memory()
        assert demo["lesson_id"] not in {l["id"] for l in m["lessons"]}
        assert not {i["id"] for i in m["investigations"]} & {demo["inv1"], demo["inv2"]}


def test_clearing_the_review_in_complaint_2_confirms_the_lesson(demo):
    """The analyst clears the review that replaced 23-8001B8810's lien in complaint 2: the account is genuine, as the
    lesson said, so its confidence rises. Complaint 2's own lesson (on the phone-sharing account, reached through
    PH-0167084) is not scored by a review in its own case, and a review resolves, and scores, once."""
    handoff, mem, lid = demo["handoff"], demo["mem"], demo["lesson_id"]
    review = next(r for r in handoff.reviews("open") if r["investigation_id"] == demo["inv2"] and r["account_id"] == SHARED)
    before = store.get_lesson(mem, lid)
    done = handoff.resolve_review(review["id"], "cleared", "Sale deed and bank statement show the flat sale.")
    assert done["resolved"] and done["review"]["status"] == "cleared"
    assert [(u["lesson_id"], u["effect"]) for u in done["lessons"]] == [(lid, "confirmed")]
    after = store.get_lesson(mem, lid)
    assert after["confidence"] == pytest.approx(before["confidence"] + 0.1)
    assert after["times_confirmed"] == before["times_confirmed"] + 1
    assert after["history"][-1]["event"] == "confirmed" and after["history"][-1]["action_id"] == review["id"]
    again = handoff.resolve_review(review["id"], "escalated", "Changed my mind.")
    assert again["resolved"] is False and again["lessons"] == []
    assert store.get_lesson(mem, lid)["confidence"] == after["confidence"]
