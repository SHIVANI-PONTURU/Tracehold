"""Phase 3 steps 7-8: which gate decisions make lessons, and how confidence changes (memory/lessons.py)."""
from memory import lessons as L

CFG = {"lessons": {"initial_confidence": {"human": 0.7, "verifier_agent": 0.5}, "confirm_step": 0.1,
                   "contradict_step": 0.2, "deactivate_below": 0.3, "max_scope_identifiers": 3}}


def test_rejections_with_a_reason_and_human_overturns_make_lessons():
    assert L.lesson_from_gate(False, "verifier_agent", "Genuine merchant; review instead.", "reject") == \
        ("release", "verifier_agent")
    assert L.lesson_from_gate(False, "human", "Customer proved the funds.", "approve") == ("release", "human")
    assert L.lesson_from_gate(True, "human", "Stolen money is still stolen money.", "reject") == ("hold", "human")
    assert L.lesson_from_gate(True, "verifier_agent", "Evidence is specific.", "approve") is None   # nothing new
    assert L.lesson_from_gate(True, "human", "Agree.", "approve") is None
    assert L.lesson_from_gate(False, "human", "  ", "approve") is None                             # no reason
    assert L.lesson_from_gate(False, "human", "Not confirmed at the gate.", "approve") is None     # code's own text
    assert L.lesson_from_gate(False, "human", "Overridden at the gate.", "approve") is None


def test_confirmations_raise_and_contradictions_lower_confidence_until_inactive():
    l = {"id": "LES-1", "stance": "release", "confidence": 0.5, "active": True, "history": []}
    assert L.effect("release", approved=False) == 1 and L.effect("release", approved=True) == -1
    assert L.effect("hold", approved=True) == 1 and L.effect("hold", approved=False) == -1
    up = L.apply(l, 1, "DEC-a", 100, CFG)
    assert up["confidence"] == 0.6 and up["times_confirmed"] == 1 and up["active"]
    down = L.apply({**l, **up}, -1, "DEC-b", 200, CFG)
    assert down["confidence"] == 0.4 and down["times_contradicted"] == 1 and down["active"]
    off = L.apply({**l, **up, **down}, -1, "DEC-c", 300, CFG)
    assert off["confidence"] == 0.2 and not off["active"]
    assert [h["event"] for h in off["history"]] == ["confirmed", "contradicted", "contradicted", "deactivated"]
    capped = L.apply({**l, "confidence": 0.95}, 1, "DEC-d", 400, CFG)
    assert capped["confidence"] == 1.0


def test_only_a_decision_on_an_account_with_the_lessons_label_scores_it():
    genuine = {"id": "LES-1", "stance": "release", "label": "likely_genuine"}
    assert L.scored_by(genuine, "likely_genuine")
    assert not L.scored_by(genuine, "mule")              # a mule liened through a shared phone leaves it alone
    assert not L.scored_by(genuine, None)
    assert L.scored_by({"id": "LES-2", "stance": "hold", "label": ""}, "mule")      # no label: every decision


def test_a_resolved_review_from_a_later_case_scores_the_lessons_on_its_account():
    release = {"id": "LES-0283-41efeff5c503-a1-23-8001B8810", "stance": "release", "label": "likely_genuine",
               "created_ts": 100, "confidence": 0.7, "active": True, "history": []}
    assert L.review_effect("release", "cleared") == 1 and L.review_effect("release", "escalated") == -1
    assert L.review_effect("hold", "escalated") == 1 and L.review_effect("hold", "cleared") == -1
    later = "INV-0354-6ec246a8dea4"
    assert L.review_counts(release, later, 200, "likely_genuine")
    assert L.review_counts(release, later, 100, "likely_genuine")                 # opened in the same second
    assert not L.review_counts(release, "INV-0283-41efeff5c503", 200, "likely_genuine")   # the lesson's own case
    assert not L.review_counts(release, later, 99, "likely_genuine")              # opened before the lesson existed
    assert not L.review_counts(release, later, None, "likely_genuine")
    assert not L.review_counts(release, later, 200, "mule")                       # another label
    up = L.apply(release, L.review_effect("release", "cleared"), "ACT-31c79caf9ad8", 300, CFG, key="action_id")
    assert up["confidence"] == 0.8 and up["times_confirmed"] == 1
    assert up["history"] == [{"ts": 300, "event": "confirmed", "action_id": "ACT-31c79caf9ad8", "confidence": 0.8}]


def test_an_opposite_newer_lesson_supersedes_an_active_one():
    assert L.supersedes("hold", {"stance": "release", "active": True})
    assert not L.supersedes("release", {"stance": "release", "active": True})
    assert not L.supersedes("hold", {"stance": "release", "active": False})


def test_scope_keeps_the_account_its_identifiers_and_label():
    s = L.scope("23-8001B8810", [("device", "DV-1"), ("phone", "PH-1"), ("phone", "PH-2"), ("device", "DV-2")],
                "likely_genuine", CFG)
    assert s == ["account:23-8001B8810", "device:DV-1", "phone:PH-1", "phone:PH-2", "label:likely_genuine"]
    assert L.entity_ids(s) == ["23-8001B8810", "DV-1", "PH-1", "PH-2"]
