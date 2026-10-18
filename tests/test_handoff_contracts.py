"""Phase 2 step 5: the handoff contracts refuse unknown fields, over-long text and inconsistent amounts."""
import pytest
from pydantic import ValidationError

from handoff import contracts as C

INV = "INV-0011-abc123def456"


def trace(**over):
    base = {"investigation_id": INV, "complaint_id": "CMP-0011", "disputed_paise": 1000,
            "trail": [{"layer": 1, "account_ids": ["20-800283B60"], "transfer_ids": ["T000418368"]}],
            "accounts": [{"account_id": "20-800283B60", "layer": 1, "tainted_paise": 700, "balance_paise": 5000}],
            "unrecoverable_paise": 200, "not_followed_paise": 100, "trace_id": INV}
    base.update(over)
    return base


def proposal_account(**over):
    base = {"account_id": "20-800283B60", "label": "mule", "proposed_action": "lien",
            "lien_proposal_id": "DEC-0011-abc123def456-1", "lien_amount_paise": 700,
            "evidence": {"transfer_ids": ["T000418368"], "signals": ["pass_through_ratio", "account_age_days"]},
            "rationale": "Forwards nearly everything within hours. The account is two days old."}
    base.update(over)
    return base


def test_a_valid_trace_handoff_parses():
    h = C.parse("trace", trace())
    assert h.accounts[0].tainted_paise == 700 and h.kind == "trace"


def test_an_extra_field_is_refused():
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        C.parse("trace", trace(reasoning="the model's private notes"))
    acc = proposal_account()
    acc["raw_tool_output"] = "{...}"
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        C.parse("proposal", {"investigation_id": INV, "accounts": [acc], "trace_id": INV})


def test_an_over_long_rationale_is_refused():
    with pytest.raises(ValidationError, match="at most 400 characters"):
        C.parse("proposal", {"investigation_id": INV, "accounts": [proposal_account(rationale="x" * 401)],
                             "trace_id": INV})
    with pytest.raises(ValidationError, match="at most two sentences"):
        C.parse("proposal", {"investigation_id": INV, "accounts": [proposal_account(
            rationale="One. Two. Three.")], "trace_id": INV})


def test_trace_amounts_must_add_up_to_the_disputed_amount():
    with pytest.raises(ValidationError, match="conservation"):
        C.parse("trace", trace(unrecoverable_paise=201))


def test_a_lien_needs_its_code_calculated_amount_and_nothing_else_carries_one():
    with pytest.raises(ValidationError, match="proposal ID and code-calculated amount"):
        C.parse("proposal", {"investigation_id": INV, "accounts": [proposal_account(lien_amount_paise=None)],
                             "trace_id": INV})
    with pytest.raises(ValidationError, match="only a lien"):
        C.parse("proposal", {"investigation_id": INV, "accounts": [proposal_account(proposed_action="review")],
                             "trace_id": INV})


def test_bad_ids_and_unknown_kinds_are_refused():
    with pytest.raises(ValidationError):
        C.parse("trace", trace(complaint_id="complaint 11"))
    with pytest.raises(KeyError):
        C.parse("chat", {})


def test_verdicts_and_check_notes():
    v = C.parse("verdict", {"investigation_id": INV, "trace_id": INV, "verdicts": [
        {"decision_id": "DEC-0011-abc123def456-1", "verdict": "approve", "reason": "Evidence is specific.",
         "decided_by": "verifier_agent"}]})
    assert v.verdicts[0].verdict == "approve"
    with pytest.raises(ValidationError):
        C.parse("verdict", {"investigation_id": INV, "trace_id": INV, "verdicts": [
            {"decision_id": "DEC-0011-abc123def456-1", "verdict": "approve", "reason": "ok",
             "decided_by": "verifier_agent", "new_amount_paise": 5}]})
    n = C.CheckFailureNote(investigation_id=INV, failed=[C.FailedCheck(check=2, account_ids=["20-800283B60"])])
    assert n.failed[0].check == 2


def test_a_verdict_may_cite_lessons():
    from handoff.contracts import LienVerdict
    v = LienVerdict(decision_id="DEC-0283-abc123def456-a1-23-8001B8810", verdict="reject", reason="A lesson applies.",
                    decided_by="verifier_agent", lessons_cited=["LES-0283-abc123def456-a1-23-8001B8810"])
    assert v.lessons_cited == ["LES-0283-abc123def456-a1-23-8001B8810"]
    assert LienVerdict(decision_id="DEC-0283-abc-a1-1", verdict="approve", reason="ok.",
                       decided_by="verifier_agent").lessons_cited == []
