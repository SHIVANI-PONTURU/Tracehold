"""Phase 2 step 1: the taint engine against the four hand-worked examples of Appendix D, plus its edge cases.

All amounts are paise. Each expected value is worked by hand in the comment above it.
"""
import random

import pytest

from taint.engine import TaintError, Transfer, compute

V = "V-victim"
T0 = 1_662_000_000          # an arbitrary start time (UTC seconds)
MIN = 60


def tr(i, src, dst, amount, minute, fmt="ACH", self_loop=False):
    return Transfer(f"T{i:09d}", src, dst, amount, T0 + minute * MIN, fmt, self_loop)


def test_single_hop():
    # Victim sends Rs 10,000 to mule M, which held Rs 500 and sends nothing on.
    # M: B = 50,000 + 10,00,000 = 10,50,000; T = 10,00,000. Lien = Rs 10,000.
    fraud = [tr(1, V, "M", 1_000_000, 0)]
    r = compute(fraud, {"M": 50_000}, [])
    m = r.accounts["M"]
    assert (m.balance, m.tainted, m.lien_amount, m.layer) == (1_050_000, 1_000_000, 1_000_000, 1)
    assert r.conservation_ok() and r.unrecoverable == 0 and r.frontier == {}


def test_merchant_example():
    # Appendix D: a shop holds Rs 2,22,000 of genuine takings, receives Rs 18,000 of tainted money (7.5% of
    # Rs 2,40,000), then pays a supplier Rs 60,000, which carries 7.5% = Rs 4,500 of taint.
    # Merchant: B = 1,80,000, T = 13,500. Supplier (held Rs 50,000): B = 1,10,000, T = 4,500.
    fraud = [tr(1, V, "SHOP", 1_800_000, 0)]
    history = [tr(2, "SHOP", "SUPPLIER", 6_000_000, 10)]
    r = compute(fraud, {"SHOP": 22_200_000, "SUPPLIER": 5_000_000}, history)
    shop, sup = r.accounts["SHOP"], r.accounts["SUPPLIER"]
    assert (shop.balance, shop.tainted, shop.lien_amount) == (18_000_000, 1_350_000, 1_350_000)
    assert (sup.balance, sup.tainted, sup.layer) == (11_000_000, 450_000, 2)
    assert shop.tainted_share == pytest.approx(0.075)
    assert r.transfers_used("SUPPLIER") == ["T000000002"]
    assert r.conservation_ok()


def test_split_and_recombined_with_rounding():
    # Victim sends Rs 10,000 to M1, which held Rs 3 (300 paise). M1 splits it: Rs 4,000 to A, then
    # Rs 6,000 to B. A and B (both empty) forward everything to C (held Rs 1,000).
    # M1 after the inflow: B = 10,00,300, T = 10,00,000.
    #   to A: floor(4,00,000 x 10,00,000 / 10,00,300) = floor(3,99,880.04) = 3,99,880; M1 T = 6,00,120, B = 6,00,300
    #   to B: floor(6,00,000 x 6,00,120 / 6,00,300) = floor(5,99,820.09) = 5,99,820;  M1 T = 300, B = 300
    # A forwards 4,00,000 with all its taint (3,99,880); B forwards 6,00,000 with 5,99,820.
    # C: B = 1,00,000 + 10,00,000 = 11,00,000; T = 9,99,700. Held: 300 + 9,99,700 = 10,00,000.
    fraud = [tr(1, V, "M1", 1_000_000, 0)]
    history = [tr(2, "M1", "A", 400_000, 5), tr(3, "M1", "B", 600_000, 6),
               tr(4, "A", "C", 400_000, 20), tr(5, "B", "C", 600_000, 21)]
    r = compute(fraud, {"M1": 300, "A": 0, "B": 0, "C": 100_000}, history)
    acc = r.accounts
    assert (acc["M1"].tainted, acc["M1"].balance) == (300, 300)
    assert acc["A"].tainted_in == 399_880 and acc["B"].tainted_in == 599_820
    assert (acc["A"].tainted, acc["B"].tainted) == (0, 0)
    assert (acc["C"].balance, acc["C"].tainted, acc["C"].layer) == (1_100_000, 999_700, 3)
    assert r.conservation_ok()


def test_cycle():
    # Victim sends Rs 10,000 to M (empty). M sends it all to N; N sends it all back to M; M then pays
    # Rs 5,000 to X, an account not tracked. Each transfer is processed once, in time order.
    # M: 10,00,000 / 10,00,000 -> 0 / 0; N: 10,00,000 / 10,00,000 -> 0 / 0; M again 10,00,000 / 10,00,000;
    # to X: 5,00,000 of taint (100%). M keeps 5,00,000 of 5,00,000; X holds 5,00,000 as frontier taint.
    fraud = [tr(1, V, "M", 1_000_000, 0)]
    history = [tr(2, "M", "N", 1_000_000, 5), tr(3, "N", "M", 1_000_000, 9), tr(4, "M", "X", 500_000, 12)]
    r = compute(fraud, {"M": 0, "N": 0}, history)
    assert (r.accounts["M"].tainted, r.accounts["M"].balance) == (500_000, 500_000)
    assert r.accounts["N"].tainted == 0
    assert r.frontier == {"X": 500_000} and r.frontier_layer == {"X": 2}
    assert r.accounts["M"].layer == 1 and r.accounts["N"].layer == 2
    assert r.conservation_ok()


def test_cash_out_is_unrecoverable():
    # M (empty) receives Rs 10,000 and withdraws Rs 2,500 in cash: that taint leaves the graph.
    fraud = [tr(1, V, "M", 1_000_000, 0)]
    history = [tr(2, "M", "ATM", 250_000, 3, fmt="Cash"), tr(3, "M", "Y", 250_000, 4, fmt="Bitcoin")]
    r = compute(fraud, {"M": 0}, history)
    assert r.unrecoverable == 500_000 and r.frontier == {}
    assert r.accounts["M"].tainted == 500_000 and r.accounts["M"].unrecoverable_out == 500_000
    assert r.conservation_ok()


def test_self_loops_are_skipped():
    fraud = [tr(1, V, "M", 1_000_000, 0)]
    history = [tr(2, "M", "M", 900_000, 1, self_loop=True), tr(3, "M", "N", 500_000, 2)]
    r = compute(fraud, {"M": 1_000_000}, history)
    # M: B = 20,00,000, T = 10,00,000 (50%); the self-loop changes nothing; 5,00,000 out carries 2,50,000.
    assert r.frontier == {"N": 250_000} and r.accounts["M"].tainted == 750_000
    assert r.conservation_ok()


def test_several_fraud_transfers_are_summed():
    fraud = [tr(1, V, "M", 300_000, 0), tr(2, V, "M", 200_000, 1), tr(3, V, "K", 100_000, 2)]
    r = compute(fraud, {"M": 0}, [])
    assert r.disputed == 600_000 and r.accounts["M"].tainted == 500_000 and r.frontier == {"K": 100_000}
    assert r.conservation_ok()


def test_same_minute_orders_by_transfer_id():
    # M receives the fraud transfer and pays out in the same minute. T000000002 (the outflow) has a lower ID
    # than T000000003 (the fraud inflow), so the outflow goes first and carries no taint.
    fraud = [tr(3, V, "M", 1_000_000, 7)]
    history = [tr(2, "M", "N", 400_000, 7)]
    r = compute(fraud, {"M": 400_000}, history)
    assert r.frontier == {} and r.accounts["M"].tainted == 1_000_000
    # With the IDs the other way round the inflow goes first: B = 14,00,000, T = 10,00,000, and the outflow
    # carries floor(4,00,000 x 10,00,000 / 14,00,000) = 2,85,714.
    r2 = compute([tr(2, V, "M", 1_000_000, 7)], {"M": 400_000}, [tr(3, "M", "N", 400_000, 7)])
    assert r2.frontier == {"N": 285_714}


def test_incomplete_history_is_refused():
    fraud = [tr(1, V, "M", 100_000, 0)]
    with pytest.raises(TaintError, match="history is incomplete"):
        compute(fraud, {"M": 0}, [tr(2, "M", "N", 500_000, 1)])


def test_victim_cannot_be_tracked():
    with pytest.raises(TaintError, match="victim"):
        compute([tr(1, V, "M", 100, 0)], {V: 10, "M": 0}, [])


def test_as_of_ignores_later_transfers():
    fraud = [tr(1, V, "M", 1_000_000, 0)]
    r = compute(fraud, {"M": 0}, [tr(2, "M", "N", 1_000_000, 60)], as_of=T0 + 30 * MIN)
    assert r.accounts["M"].tainted == 1_000_000 and r.frontier == {}


@pytest.mark.parametrize("seed", range(25))
def test_random_networks_conserve_and_never_exceed_balance(seed):
    """Random histories: the books balance to the paisa and no account holds more taint than money."""
    rng = random.Random(seed)
    names = [f"A{i}" for i in range(8)]
    bal = {a: rng.randint(0, 5_000_000) for a in names}
    opening = dict(bal)
    fraud = [tr(1, V, rng.choice(names), rng.randint(1, 3_000_000), 0) for _ in range(rng.randint(1, 3))]
    fraud = [Transfer(f"T{i:09d}", f.src, f.dst, f.amount, f.ts, f.format) for i, f in enumerate(fraud, 1)]
    for f in fraud:
        bal[f.dst] += f.amount
    history, i = [], 100
    for minute in range(1, 200):
        src = rng.choice(names)
        if bal[src] == 0:
            continue
        dst = rng.choice(names + ["OUT1", "OUT2"])
        amt = rng.randint(1, bal[src])
        fmt = rng.choice(["ACH", "Wire", "Cash"]) if dst.startswith("A") else "ACH"
        history.append(tr(i, src, dst, amt, minute, fmt, self_loop=(src == dst)))
        if src != dst:
            bal[src] -= amt
            if dst in bal:
                bal[dst] += amt
        i += 1
    r = compute(fraud, opening, history)
    assert r.conservation_ok()
    for a in r.accounts.values():
        assert 0 <= a.tainted <= a.balance
        assert a.balance == bal[a.account_id]
