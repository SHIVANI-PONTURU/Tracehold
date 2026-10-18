"""Phase 3 steps 2, 4, 6, 7, 8 and 9 on FalkorDB: deterministic MERGE writers, the state machine in the store,
leases, recall through a shared phone on the real network graph, lessons made and changed at the gate, and each
bank's memory kept apart."""
import pytest

from handoff import api as A
from handoff import state as S
from memory import store

pytestmark = [pytest.mark.integration, pytest.mark.data]
SHARED, PHONE_SHARER, PHONE = "23-8001B8810", "12-800063990", "PH-0167084"   # bank 20's demo pair (CMP-0283, CMP-0354)


def count(mem, label):
    return mem.value(f"MATCH (n:{label}) RETURN count(n)")


@pytest.fixture
def mem(tool_ctx):
    m = tool_ctx.memory("999001")
    m.query("MATCH (n) DETACH DELETE n")
    return m


def test_every_writer_run_twice_leaves_one_node(mem):
    inv = store.create_investigation(mem, "CMP-0283", inv_id="INV-0283-twice0000001")
    store.create_investigation(mem, "CMP-0283", inv_id=inv)
    payload = {"kind": "trace", "accounts": [SHARED]}
    store.add_handoff(mem, inv, "trace", "tracer", "adjudicator", payload)
    store.add_handoff(mem, inv, "trace", "tracer", "adjudicator", payload)
    for _ in range(2):
        store.add_step(mem, inv, "tracer", "recall_context", "in", "out", 12, 0, step_id="STEP-0283-twice-tracer-r1-1")
        store.write_claims(mem, inv, 1, [{"account_id": SHARED, "label": "mule", "proposed_action": "lien",
                                          "rationale": "Holds it.", "evidence": {"transfer_ids": ["T000000001"]}}])
        store.write_decision(mem, inv, "DEC-0283-twice0000001-a1-" + SHARED, SHARED, "mule", "lien", 1000, "Holds it.")
        store.write_action(mem, inv, "review", "account", SHARED, "Look at it.")
        store.involve(mem, inv, [{"id": SHARED, "role": "traced"}, {"id": PHONE, "kind": "phone", "role": "identifier",
                                                                     "via": SHARED}])
        store.create_lesson(mem, "LES-0283-twice0000001-a1-" + SHARED, "DEC-0283-twice0000001-a1-" + SHARED, "A sale.",
                            "release", "human", "mule", ["account:" + SHARED, "phone:" + PHONE], 0.7)
        store.write_lien(mem, "DEC-0283-twice0000001-a1-" + SHARED, "LREQ-twice", SHARED, 1000, "requested", "LREQ-twice")
    for label in ("Investigation", "Handoff", "Step", "Claim", "Decision", "Action", "Lesson", "Lien"):
        assert count(mem, label) == 1, label
    assert mem.value("MATCH (:Investigation)-[r:INVOLVES]->() RETURN count(r)") == 2
    assert mem.value("MATCH (:Lesson)-[r:APPLIES_TO]->() RETURN count(r)") == 2


def test_the_store_refuses_illegal_transitions_and_takes_back_expired_claims(mem):
    inv = store.create_investigation(mem, "CMP-0283", inv_id="INV-0283-lease0000001")
    with pytest.raises(S.IllegalTransition):
        store.move(mem, inv, ["new"], "at_gate")
    assert store.claim_next(mem, "new", "tracing", "tracer", lease_s=900) == inv
    assert store.claim_stale(mem, "tracing", "tracer") is None                 # lease still running
    mem.query("MATCH (i:Investigation {id: $id}) SET i.lease_until = 0", {"id": inv})
    assert store.claim_stale(mem, "tracing", "tracer") == inv                  # the worker died: taken back
    assert store.claim_stale(mem, "tracing", "tracer", force=True) == inv      # a restarted worker takes its own back
    assert store.get_investigation(mem, inv)["reclaims"] == 2


def test_recall_finds_an_earlier_case_through_a_shared_phone(tool_ctx):
    from mcp_server import tools as T
    mem = tool_ctx.memory("20")
    for i in store.list_investigations(mem):
        mem.query("MATCH (i:Investigation {id: $id}) DETACH DELETE i", {"id": i["id"]})
    first = store.create_investigation(mem, "CMP-0283")
    second = store.create_investigation(mem, "CMP-0354")
    empty = T.recall_context(tool_ctx, "20", second, account_ids=[PHONE_SHARER])
    assert empty["cases"] == [] and empty["matches"] == []                     # empty on a fresh memory
    A.remember_trace(tool_ctx, mem, first, {"accounts": [{"account_id": SHARED, "layer": 1, "tainted_paise": 119016200}]},
                     "20-800195070")
    store.write_decision(mem, first, f"DEC-{first[4:]}-a1-{SHARED}", SHARED, "likely_genuine", "lien", 119016200, "x")
    r = T.recall_context(tool_ctx, "20", second, account_ids=[PHONE_SHARER, SHARED])
    assert [c["investigation_id"] for c in r["cases"]] == [first]
    via = {(m["entity_id"], h["account"], h["via"]) for m in r["matches"] for h in m["how"]}
    assert (SHARED, SHARED, "account") in via and (PHONE, PHONE_SHARER, "phone") in via
    assert r["decisions"][0]["account_id"] == SHARED
    sig = T.account_signals(tool_ctx, "20", second, PHONE_SHARER)
    assert sig["related_cases"] == 1


def gate_case(tool_ctx, mem, complaint, account, verdict, proposal_lessons=()):
    inv = store.create_investigation(mem, complaint)
    did = f"DEC-{inv[4:]}-a1-{account}"
    store.write_decision(mem, inv, did, account, "likely_genuine", "lien", 1000, "x")
    store.add_handoff(mem, inv, "proposal", "adjudicator", "verifier", {"accounts": [
        {"account_id": account, "lien_proposal_id": did, "lessons_applied": list(proposal_lessons)}]})
    store.add_handoff(mem, inv, "verdict", "verifier", "gate", {"verdicts": [
        {"decision_id": did, "verdict": verdict, "reason": "r", "lessons_cited": []}]})
    return inv, store.get_decision(mem, did)


def test_lessons_are_made_confirmed_contradicted_and_superseded_at_the_gate(tool_ctx):
    mem = tool_ctx.memory("20")
    mem.query("MATCH (l:Lesson) DETACH DELETE l")
    cfg = A.settings()
    # case 1: the human rejects the lien with a reason -> a "do not lien" lesson on the account and its phones
    inv1, d1 = gate_case(tool_ctx, mem, "CMP-0283", SHARED, "approve")
    store.record_gate(mem, d1["id"], False, "human", "placeholder", "The holder showed the money was a flat sale.")
    out = A.learn_at_gate(tool_ctx, mem, inv1, d1, False, "human", "The holder showed the money was a flat sale.", cfg)
    lesson = out["lesson"]
    assert lesson["stance"] == "release" and lesson["confidence"] == 0.7 and f"phone:{PHONE}" in lesson["applies_to"]
    # case 2: the Verifier's rejection of a lien on an account sharing the phone stands -> the lesson is confirmed
    inv2, d2 = gate_case(tool_ctx, mem, "CMP-0354", PHONE_SHARER, "reject")
    store.record_gate(mem, d2["id"], False, "verifier_agent", "placeholder", "Same pattern as the earlier sale.")
    out2 = A.learn_at_gate(tool_ctx, mem, inv2, d2, False, "verifier_agent", "Same pattern as the earlier sale.", cfg)
    l1 = store.get_lesson(mem, lesson["id"])
    assert l1["confidence"] == 0.8 and l1["times_confirmed"] == 1
    assert out2["updated"][0]["effect"] == "confirmed"
    # case 3: a human overturns the Verifier's rejection on the same account -> contradiction, and the new
    # "lien" lesson supersedes both older "do not lien" lessons that apply
    inv3, d3 = gate_case(tool_ctx, mem, "CMP-0354", SHARED, "reject")
    store.record_gate(mem, d3["id"], True, "human", "placeholder", "A second victim's money arrived here.")
    out3 = A.learn_at_gate(tool_ctx, mem, inv3, d3, True, "human", "A second victim's money arrived here.", cfg)
    l1 = store.get_lesson(mem, lesson["id"])
    assert l1["times_contradicted"] == 1 and l1["confidence"] == 0.6 and not l1["active"]
    assert l1["superseded_by"] == out3["lesson"]["id"] and out3["lesson"]["stance"] == "hold"
    assert [h["event"] for h in l1["history"]][-1] == "superseded"


def test_a_lesson_at_one_bank_is_invisible_to_the_other_two(tool_ctx):
    m20, m11, m12 = tool_ctx.memory("20"), tool_ctx.memory("11"), tool_ctx.memory("12")
    assert store.lessons_touching(m20, [SHARED, PHONE], active_only=False)
    assert store.lessons_touching(m11, [SHARED, PHONE], active_only=False) == []
    assert store.lessons_touching(m12, [SHARED, PHONE], active_only=False) == []
    assert all(l["id"] not in {x["id"] for x in store.all_lessons(m11) + store.all_lessons(m12)}
               for l in store.all_lessons(m20))
