"""Phase 3 step 2: the run state machine refuses illegal transitions."""
import pytest

from handoff import state as S


def test_the_happy_path_and_the_retry_are_legal():
    path = ["new", "tracing", "traced", "adjudicating", "checks_failed", "adjudicating", "proposed", "verifying",
            "at_gate", "executed"]
    for a, b in zip(path, path[1:]):
        assert S.allowed(a, b), (a, b)
    assert S.allowed("at_gate", "closed")


def test_illegal_transitions_are_refused():
    for a, b in [("new", "at_gate"), ("traced", "proposed"), ("proposed", "executed"), ("tracing", "adjudicating"),
                 ("executed", "closed"), ("failed", "tracing"), ("closed", "new"), ("at_gate", "verifying")]:
        assert not S.allowed(a, b), (a, b)
        with pytest.raises(S.IllegalTransition):
            S.check([a], b)
    with pytest.raises(S.IllegalTransition):
        S.check(["new"], "awaiting_gate")                    # not a status here (the plan's name; TraceHold uses at_gate)


def test_working_statuses_can_be_taken_back_and_any_open_run_can_fail_or_close():
    for w in S.WORKING.values():
        assert S.allowed(w, w)
    for s in S.STATUSES:
        if s in S.FINAL:
            assert not S.TRANSITIONS[s]
        else:
            assert S.allowed(s, "failed") and S.allowed(s, "closed")


def test_check_keeps_only_the_legal_sources():
    assert S.check(["traced", "checks_failed", "new"], "adjudicating") == ["traced", "checks_failed"]
