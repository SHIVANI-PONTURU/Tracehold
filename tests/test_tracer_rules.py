"""The Tracer's absorb rule: the model may stop a branch as "absorbed by a genuine account" only when the account
still holds most of the tainted money it received (agents/tracer/graph.py, absorb_rule)."""
from agents.tracer.graph import absorb_rule, held_share


def acc(received, held):
    return {"tainted_in_paise": received, "tainted_paise": held}


def test_a_stop_on_an_account_that_passed_the_money_on_is_overridden():
    # complaint 1's first mule on 17 Oct: received Rs 8,67,196.01, still holds Rs 1,064.98 (the rest passed on or
    # cashed out); the model called it absorbed
    share = held_share(acc(86_719_601, 106_498))
    decision, reason, overridden = absorb_rule("absorbed_genuine", "large genuine flows", share, 0.5)
    assert (decision, overridden) == ("follow", True)
    assert "0.1%" in reason and "large genuine flows" in reason


def test_a_stop_on_an_account_that_kept_the_money_stands():
    share = held_share(acc(1_800_000, 1_350_000))          # the plan's merchant: keeps 75% of the taint it received
    assert absorb_rule("absorbed_genuine", "merchant", share, 0.5) == ("absorbed_genuine", "merchant", False)


def test_follow_is_never_changed_and_the_threshold_is_inclusive():
    assert absorb_rule("follow", "mule", 0.99, 0.5) == ("follow", "mule", False)


def test_a_stop_on_an_account_that_passed_on_a_large_amount_is_followed():
    """Version 2 (test complaint CMP-0206): the account kept 64% of what it received, but the Rs 78 crore it passed on
    reached seven more mules. A stop stands only if what it passed on is under a tenth of the disputed amount."""
    cap = 21_718_170_000                     # a tenth of the disputed Rs 217.18 crore, in paise
    decision, reason, overridden = absorb_rule("absorbed_genuine", "large account", 0.64, 0.5,
                                               passed_on=78_000_000_000, max_passed_on=cap)
    assert decision == "follow" and overridden and "passed on Rs 780,000,000.00" in reason
    assert absorb_rule("absorbed_genuine", "shop", 0.64, 0.5, passed_on=1_000, max_passed_on=cap)[0] == "absorbed_genuine"
    assert absorb_rule("absorbed_genuine", "shop", 0.64, 0.5, passed_on=10**12)[0] == "absorbed_genuine"   # cap off
    assert absorb_rule("absorbed_genuine", "x", 0.5, 0.5)[0] == "absorbed_genuine"


def test_an_account_that_received_no_taint_counts_as_holding_it_all():
    assert held_share(acc(0, 0)) == 1.0
