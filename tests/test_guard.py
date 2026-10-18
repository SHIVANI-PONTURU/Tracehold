"""Phase 2 step 3 check: execute_lien refuses without an approved decision (unit test, no database)."""
import pytest

from mcp_server import tools as T
from safety import guard

INV = "INV-0011-abc123def456"
COMPLAINT = {"complaint_id": "CMP-0011", "victim_account_id": "11-800178CC0", "fraud_transfer_ids": ["T000418368"],
             "disputed_paise": 86_719_601, "reported_at": 0}
APPROVED = {"id": "DEC-0011-abc123def456-1", "investigation_id": INV, "account_id": "20-800283B60",
            "label": "mule", "proposed_action": "lien", "lien_amount": 106_498, "approved": True,
            "decided_by": "verifier_agent", "gate_mode": "placeholder", "gate_ts": 1_700_000_000}


@pytest.mark.parametrize("change, code", [
    ({"approved": None}, "not_approved"),
    ({"approved": False}, "not_approved"),
    ({"decided_by": None}, "no_decider"),
    ({"gate_ts": None}, "gate_not_passed"),
    ({"gate_mode": "auto"}, "gate_not_passed"),
    ({"investigation_id": "INV-0011-other"}, "wrong_investigation"),
    ({"proposed_action": "review"}, "not_a_lien"),
    ({"lien_amount": 0}, "bad_amount"),
    ({"lien_amount": 86_719_602}, "bad_amount"),
    ({"account_id": "11-800178CC0"}, "victim"),
])
def test_the_guard_refuses(change, code):
    with pytest.raises(guard.Refused) as e:
        guard.evaluate({**APPROVED, **change}, INV, COMPLAINT)
    assert e.value.code == code


def test_the_guard_refuses_a_missing_decision():
    with pytest.raises(guard.Refused) as e:
        guard.evaluate(None, INV, COMPLAINT)
    assert e.value.code == "no_decision"


def test_an_approved_decision_that_passed_the_gate_may_run():
    assert guard.evaluate(APPROVED, INV, COMPLAINT) is None


class FakeCtx:
    """Just enough of the tool context for execute_lien; the bank API must never be reached."""
    bank_api_url = "http://bank-api.invalid"
    net = None

    def memory(self, bank_id):
        return object()


def test_execute_lien_without_an_approved_decision_is_refused(monkeypatch):
    unapproved = {k: v for k, v in APPROVED.items() if k not in ("approved", "decided_by", "gate_mode", "gate_ts")}
    monkeypatch.setattr(T, "investigation", lambda ctx, b, i: (object(), {"id": INV}, COMPLAINT))
    monkeypatch.setattr(guard.store, "get_decision", lambda mem, did: dict(unapproved))
    monkeypatch.setattr(guard.store, "lien_for", lambda mem, did: None)
    monkeypatch.setattr(T, "_bank_post", lambda *a, **k: pytest.fail("the bank API must not be called"))
    r = T.run_tool(T.execute_lien, FakeCtx(), "11", INV, APPROVED["id"])
    assert r["error"]["code"] == "refused_not_approved"


def test_execute_lien_is_not_served_to_any_agent():
    assert all("execute_lien" not in tools for tools in T.SCOPES.values())
    assert "propose_lien" not in T.SCOPES["tracer"] and "propose_lien" not in T.SCOPES["verifier"]
