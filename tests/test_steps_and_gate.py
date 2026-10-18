"""Phase 3 steps 3 and 5 without a database: one Step per agent node, and the HITL gate replayed from the stored
handoffs when its checkpoint is gone (the Verifier's sandbox was recreated)."""
import pytest

pytest.importorskip("langgraph")

from agents import worker                                   # noqa: E402
from agents.common import model as M                        # noqa: E402
from agents.common.config import load                       # noqa: E402
from agents.common.steps import StepRecorder                # noqa: E402
from agents.verifier import graph as V                      # noqa: E402

INV = "INV-0283-abc123def456"
D1, D2 = f"DEC-0283-abc123def456-a1-23-8001B8810", f"DEC-0283-abc123def456-a1-12-800063990"


def proposal():
    acc = lambda a, d: {"account_id": a, "label": "mule", "proposed_action": "lien", "lien_proposal_id": d,
                        "lien_amount_paise": 1000, "evidence": {"transfer_ids": ["T000000001"],
                                                                "signals": ["account_age_days", "distinct_payers"]},
                        "lessons_applied": [], "rationale": "Holds the money."}
    return {"investigation_id": INV, "attempt": 1, "trace_id": "t", "accounts": [acc("23-8001B8810", D1),
                                                                                 acc("12-800063990", D2)],
            "checks": {"passed": True, "results": [{"check": i, "name": "", "passed": True, "account_ids": []}
                                                   for i in range(1, 8)], "failed": []}}


class FakeHandoff:
    bank_id = "20"

    def __init__(self):
        self.steps, self.records, self.executed, self.verdict, self.decisions = [], [], [], None, None

    def read(self, inv, kind):
        if kind == "proposal":
            return {"payload": proposal()}
        if kind == "trace":
            return {"payload": {"complaint_id": "CMP-0283", "disputed_paise": 5000}}
        if kind == "verdict":
            return {"payload": self.verdict} if self.verdict else {}
        return {}

    def submit(self, kind, payload):
        self.verdict = payload
        return {"status": "at_gate"}

    def step(self, inv, step):
        self.steps.append(step)

    def gate_record(self, inv, did, approved, by, mode, reason):
        self.records.append((did, approved, by, mode, reason))
        return {"recorded": True, "learned": None}

    def gate_execute(self, inv, did):
        self.executed.append(did)
        return {"lien_id": f"LREQ-{did[-4:]}"}

    def gate_finish(self, inv):
        return {"status": "executed" if self.executed else "closed"}

    def investigations(self):
        return [{"id": INV, "status": "at_gate", "trace_id": "t", "complaint_id": "CMP-0283"}]

    def gate(self, inv):
        return {"decisions": self.decisions, "gate_mode": "placeholder"}


def verdicts(messages):
    return {"verdicts": [{"decision_id": D1, "verdict": "approve", "reason": "Specific evidence."},
                         {"decision_id": D2, "verdict": "reject", "reason": "Too little evidence.",
                          "lessons_cited": ["LES-not-real"]}]}


def test_one_step_per_node_and_the_gate_resumes_from_its_checkpoint():
    from langgraph.checkpoint.memory import MemorySaver
    from langgraph.types import Command
    cfg, h = load(), FakeHandoff()
    model = M.FakeModel([verdicts], tokens_per_call=1234)
    rec = StepRecorder(h, "verifier", INV, None, model)
    g = V.build(cfg, h, model, checkpointer=MemorySaver(), recorder=rec)
    conf = V.config(cfg, INV)
    out = g.invoke({"bank_id": "20", "investigation_id": INV, "trace_id": "t"}, conf)
    assert "__interrupt__" in out
    assert [s["node"] for s in h.steps] == ["load", "review", "submit"]      # the interrupt node records no Step
    review = h.steps[1]
    assert review["tokens"] == 1234 and review["tool"] == "model" and review["status"] == "ok"
    assert h.steps[0]["id"].startswith(f"STEP-0283-abc123def456-verifier-{rec.run}-")
    assert {v["decision_id"]: v["lessons_cited"] for v in h.verdict["verdicts"]} == {D1: [], D2: []}   # unknown ID dropped
    g.invoke(Command(resume={"decisions": [], "gate_mode": "placeholder"}), conf)
    assert [s["node"] for s in h.steps] == ["load", "review", "submit", "execute"]
    assert h.executed == [D1] and [r[1] for r in h.records] == [True, False]


def test_a_gate_without_its_checkpoint_is_replayed_from_the_stored_handoffs(tmp_path):
    cfg, h = load(), FakeHandoff()
    cfg["verifier"]["checkpoint_db"] = str(tmp_path / "fresh.sqlite")          # the recreated sandbox: no checkpoint
    h.verdict = {"investigation_id": INV, "trace_id": "t", "checks_failed": False, "verdicts": [
        {"decision_id": D1, "verdict": "approve", "reason": "Specific evidence.", "decided_by": "verifier_agent"},
        {"decision_id": D2, "verdict": "approve", "reason": "Specific evidence.", "decided_by": "verifier_agent"}]}
    v = worker.Verifier(cfg, h)
    assert v.resume_ready() == []                                              # no gate decision yet: nothing to do
    h.decisions = [{"decision_id": D2, "approve": False, "reason": "Customer showed the money was a sale."}]
    done = v.resume_ready()
    assert done == [{"investigation_id": INV, "status": "executed", "how": "replayed"}]
    assert h.executed == [D1]                                                  # D2 overridden by the human
    assert h.records[1] == (D2, False, "human", "placeholder", "Customer showed the money was a sale.")
    assert [s["node"] for s in h.steps] == ["execute"] and h.steps[0]["agent"] == "gate"


def test_confirm_mode_places_nothing_the_human_did_not_confirm():
    h = FakeHandoff()
    vs = [{"decision_id": D1, "verdict": "approve", "reason": "ok"}]
    out = V.apply_gate(load(), h, INV, vs, {"decisions": [], "gate_mode": "confirm"})
    assert h.executed == [] and h.records[0][1:3] == (False, "human") and out["status"] == "closed"


def test_a_step_is_recorded_for_a_failing_node_and_recording_never_stops_a_run():
    class Broken(FakeHandoff):
        def step(self, inv, step):
            raise ConnectionError("tool server down")
    rec = StepRecorder(FakeHandoff(), "tracer", INV)
    with pytest.raises(ValueError):
        rec.wrap("judge", lambda s: (_ for _ in ()).throw(ValueError("bad answer")))({"a": 1})
    assert rec.posted[0]["status"] == "error" and "bad answer" in rec.posted[0]["output_summary"]
    quiet = StepRecorder(Broken(), "tracer", INV)
    assert quiet.wrap("plan", lambda s: {"plan": [1, 2]})({}) == {"plan": [1, 2]}
    assert quiet.posted[0]["output_summary"] == "plan=[2]"
