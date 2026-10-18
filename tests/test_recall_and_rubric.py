"""Phase 3 steps 6-8 without a database: memory notes from a recall result, which lessons apply to an account, the
chat answer's evidence IDs, and how a cited "do not lien" lesson changes a likely-genuine lien."""
from agents.adjudicator.graph import rubric_action
from agents.common import recall as R
from handoff.api import cited_in

RECALL = {
    "cases": [{"investigation_id": "INV-0283-aaa", "complaint_id": "CMP-0283", "status": "executed"}],
    "matches": [
        {"entity_id": "10.114.237.136", "entity_kind": "ip", "investigation_id": "INV-0283-aaa",   # its own IP, sorted first
         "complaint_id": "CMP-0283", "role_then": "identifier", "earlier_account": "23-8001B8810",
         "how": [{"account": "23-8001B8810", "via": "ip"}]},
        {"entity_id": "23-8001B8810", "entity_kind": "account", "investigation_id": "INV-0283-aaa",
         "complaint_id": "CMP-0283", "role_then": "traced", "earlier_account": "23-8001B8810",
         "how": [{"account": "23-8001B8810", "via": "account"}]},
        {"entity_id": "PH-0167084", "entity_kind": "phone", "investigation_id": "INV-0283-aaa",
         "complaint_id": "CMP-0283", "role_then": "identifier", "earlier_account": "23-8001B8810",
         "how": [{"account": "23-8001B8810", "via": "phone"}, {"account": "12-800063990", "via": "phone"}]}],
    "decisions": [{"investigation_id": "INV-0283-aaa", "decision_id": "DEC-0283-aaa-a1-23-8001B8810",
                   "account_id": "23-8001B8810", "label": "likely_genuine", "proposed_action": "lien",
                   "approved": False, "decided_by": "human", "reason": "The customer showed it was a sale."}],
    "lessons": [{"id": "LES-0283-aaa-a1-23-8001B8810", "text": "The customer showed it was a sale.",
                 "stance": "release", "label": "likely_genuine", "confidence": 0.7, "times_confirmed": 0,
                 "times_contradicted": 0, "matched": ["23-8001B8810", "PH-0167084"],
                 "for_accounts": ["12-800063990", "23-8001B8810"]}],
    "bank_lessons": [{"id": "LES-0283-aaa-a1-23-8001B8810", "text": "The customer showed it was a sale.",
                      "stance": "release", "label": "likely_genuine", "confidence": 0.7}],
}


def test_notes_say_which_case_touched_the_account_and_how():
    n = R.notes(RECALL, ["23-8001B8810", "12-800063990", "22-8001D6FF0"])
    assert set(n) == {"23-8001B8810", "12-800063990"}
    assert n["23-8001B8810"].startswith("CMP-0283 (INV-0283-aaa): same account; decided likely_genuine -> lien, "
                                        "rejected by human")
    assert "shares phone PH-0167084 with 23-8001B8810" in n["12-800063990"]


def test_lessons_reach_the_accounts_they_apply_to():
    assert [l["id"] for l in R.lessons_for(RECALL, "12-800063990")] == ["LES-0283-aaa-a1-23-8001B8810"]
    assert R.lessons_for(RECALL, "22-8001D6FF0") == []
    assert "do not lien accounts like this" in R.lesson_line(RECALL["lessons"][0])
    assert R.known_lesson_ids(RECALL) == {"LES-0283-aaa-a1-23-8001B8810"}
    assert R.safe({"error": {"code": "timeout"}}) == R.EMPTY


def test_a_cited_do_not_lien_lesson_turns_a_likely_genuine_lien_into_a_review():
    floor = 500000
    assert rubric_action("likely_genuine", "review", 1_000_000, floor) == "lien"            # no lesson: the rubric liens
    assert rubric_action("likely_genuine", "review", 1_000_000, floor, release_lesson=True) == "review"
    assert rubric_action("likely_genuine", "lien", 1_000_000, floor, release_lesson=True) == "lien"
    assert rubric_action("mule", "review", 1_000_000, floor, release_lesson=True) == "lien"  # a mule is still liened
    assert rubric_action("likely_genuine", "lien", 100, floor) == "review"
    assert rubric_action("uncertain", "lien", 1_000_000, floor) == "review_and_request_info"


def test_an_account_holding_mostly_stolen_money_is_liened_whatever_its_label():
    """Phase 4, dev complaint CMP-0099: 95.5% of the account's balance was the victim's money, Claude said uncertain,
    and a review alone left Rs 56.7 crore unheld. A human "do not lien" lesson still wins."""
    floor = 500_000
    assert rubric_action("uncertain", "review_and_request_info", 10**9, floor, share=0.955, hold_share=0.5) == "lien"
    assert rubric_action("uncertain", "review_and_request_info", 10**9, floor, share=0.3, hold_share=0.5) ==         "review_and_request_info"
    assert rubric_action("likely_genuine", "review", 10**9, floor, release_lesson=True, share=0.9, hold_share=0.5) == "review"
    assert rubric_action("likely_genuine", "lien", 100, floor, share=0.9, hold_share=0.5) == "review"   # below the floor
    assert rubric_action("uncertain", "review", 10**9, floor, share=0.9) == "review_and_request_info"  # rule switched off


def test_an_account_holding_a_large_part_of_the_loss_is_liened_whatever_its_label():
    """Version 2 (first test sample): IBM mules holding 47 to 90% of their complaint's loss, at a low share of their
    own balance, were labelled uncertain and only reviewed (Rs 138.6 crore in CMP-0206)."""
    floor = 500_000
    kw = {"share": 0.397, "hold_share": 0.5, "hold_part": 0.25}
    assert rubric_action("uncertain", "review_and_request_info", 10**9, floor, part=0.64, **kw) == "lien"
    assert rubric_action("uncertain", "review_and_request_info", 10**9, floor, part=0.10, **kw) == \
        "review_and_request_info"
    assert rubric_action("likely_genuine", "review", 10**9, floor, release_lesson=True, part=0.9, **kw) == "review"


def test_chat_answers_are_checked_for_every_id_they_write():
    ids = cited_in("Claim CLM-0283-aaa-a1-23-8001B8810 cites T000418368; the lien LREQ-29a4df7bbaa8 on 2591-80035FF20 "
                   "follows LES-0283-aaa-a1-23-8001B8810. Device DV-0218398 and phone PH-0167084.")
    assert ids["memory"] == ["CLM-0283-aaa-a1-23-8001B8810", "LES-0283-aaa-a1-23-8001B8810", "LREQ-29a4df7bbaa8"]
    assert ids["transfer"] == ["T000418368"]
    assert ids["account"] == ["2591-80035FF20"]                     # the account inside the claim ID is not counted again
    assert ids["entity"] == ["DV-0218398", "PH-0167084"]


def test_accounts_the_adjudicator_left_out_are_asked_for_once_more():
    """Phase 4 sample run, CMP-0129: Claude answered with no account at all for a 7-account table."""
    from agents.adjudicator.graph import ask_missing
    from agents.common.model import FakeModel
    call = lambda a, label: {"account_id": a, "label": label, "proposed_action": "review",            # noqa: E731
                             "signals": ["account_age_days", "distinct_payers"], "rationale": "x.",
                             "lessons_applied": [], "flag_entity_ids": [], "info_request": None}
    m = FakeModel([{"accounts": [call("B", "mule"), call("A", "uncertain"), call("Z", "mule")]}])
    out = ask_missing(m, [("system", "s"), ("human", "table")], ["A", "B", "C"], [call("A", "mule")])
    assert [(c["account_id"], c["label"]) for c in out] == [("A", "mule"), ("B", "mule")]   # first call kept; Z not asked
    assert "left out these accounts in the table: B, C" in m.prompts[0][1][-1][1]
    none = FakeModel([])
    assert ask_missing(none, [], ["A"], [call("A", "mule")]) == [call("A", "mule")] and none.prompts == []
