"""Phase 4 step 4: property tests for the taint engine. hypothesis generates thousands of money networks (chains of
any depth up to 20, splits, cycles, money that comes back, recombinations, cash and Bitcoin exits, untracked
accounts) with feasible opening balances, and the engine must keep its books to the paisa on every one of them.

Each opening balance is the account's minimum feasible balance plus a random buffer, the overlay's own rule
(Appendix C step 5), so no running balance goes negative.
"""
import random

import pytest

hypothesis = pytest.importorskip("hypothesis")
from hypothesis import HealthCheck, given, settings, strategies as st   # noqa: E402

from taint.engine import EXIT_FORMATS, Transfer, compute               # noqa: E402

V = "V-victim"
T0 = 1_662_000_000
FORMATS = ["ACH", "Wire", "Cheque", "Credit Card", "Cash", "Bitcoin"]
HEALTH = [HealthCheck.too_slow, HealthCheck.data_too_large]


def feasible_openings(accounts, transfers, buffers):
    """Minimum feasible opening balance per tracked account (its largest running deficit, in the engine's order)
    plus a buffer."""
    run = {a: 0 for a in accounts}
    low = dict(run)
    for t in sorted(transfers, key=lambda t: (t.ts, t.id)):
        if t.src == t.dst:
            continue
        if t.src in run:
            run[t.src] -= t.amount
            low[t.src] = min(low[t.src], run[t.src])
        if t.dst in run:
            run[t.dst] += t.amount
    return {a: -low[a] + b for a, b in zip(accounts, buffers)}


@st.composite
def networks(draw):
    """Random graphs over a few tracked accounts, two untracked ones and the victim: splits, cycles, money that
    comes back to an account or to the victim, recombinations and exits all occur."""
    accounts = [f"A{i}" for i in range(draw(st.integers(1, 9)))]
    outside = ["X0", "X1"]
    nf = draw(st.integers(1, 3))
    fraud = [Transfer(f"T{i:09d}", V, draw(st.sampled_from(accounts + outside)), draw(st.integers(1, 10**9)),
                      T0 + 60 * draw(st.integers(0, 40)), "ACH") for i in range(nf)]
    rest = []
    for j in range(draw(st.integers(0, 60))):
        src = draw(st.sampled_from(accounts + outside + [V]))
        dst = draw(st.sampled_from(accounts + outside + [V]))
        rest.append(Transfer(f"T{nf + j:09d}", src, dst, draw(st.integers(1, 10**9)), T0 + 60 * draw(st.integers(0, 120)),
                             draw(st.sampled_from(FORMATS)), src == dst))
    buffers = draw(st.lists(st.integers(0, 10**8), min_size=len(accounts), max_size=len(accounts)))
    return fraud, feasible_openings(accounts, fraud + rest, buffers), rest


@st.composite
def chains(draw):
    """A layered chain of 1 to 20 hops; each hop forwards a random share of its balance onwards, often split in two
    and recombined at the next hop, sometimes cashing part out, so taint must survive every depth."""
    depth = draw(st.integers(1, 20))
    layers = [[f"L{d}a", f"L{d}b"] if draw(st.booleans()) else [f"L{d}a"] for d in range(1, depth + 1)]
    accounts = [a for layer in layers for a in layer]
    amount = draw(st.integers(10_000, 10**10))
    fraud = [Transfer("T000000000", V, layers[0][0], amount, T0, "ACH")]
    rest, n, minute = [], 1, 1
    for d in range(len(layers) - 1):
        for src in layers[d]:
            for dst in layers[d + 1]:
                rest.append(Transfer(f"T{n:09d}", src, dst, draw(st.integers(1, amount)), T0 + 60 * minute,
                                     draw(st.sampled_from(FORMATS[:4]))))
                n += 1
            if draw(st.booleans()):                          # part of it leaves the banking graph
                rest.append(Transfer(f"T{n:09d}", src, "CASH-OUT", draw(st.integers(1, amount)), T0 + 60 * minute,
                                     draw(st.sampled_from(sorted(EXIT_FORMATS)))))
                n += 1
            if draw(st.booleans()):                          # genuine money arrives from outside
                rest.append(Transfer(f"T{n:09d}", "OUTSIDE", src, draw(st.integers(1, amount)), T0 + 60 * minute, "ACH"))
                n += 1
        minute += draw(st.integers(0, 3))                    # same-minute forwarding happens too
    buffers = draw(st.lists(st.integers(0, 10**9), min_size=len(accounts), max_size=len(accounts)))
    return fraud, feasible_openings(accounts, fraud + rest, buffers), rest


def check_books(fraud, opening, rest):
    r = compute(fraud, opening, rest)
    disputed = sum(f.amount for f in fraud)
    assert r.disputed == disputed
    assert r.held + sum(r.frontier.values()) + r.unrecoverable == disputed          # conservation, to the paisa
    assert r.conservation_ok() and r.held <= disputed
    moved = {a: opening[a] for a in opening}
    for t in fraud + rest:                                                           # balances are plain bookkeeping
        if t.src == t.dst:
            continue
        if t.src in moved:
            moved[t.src] -= t.amount
        if t.dst in moved:
            moved[t.dst] += t.amount
    for a, acc in r.accounts.items():
        assert acc.balance == moved[a] >= 0
        assert 0 <= acc.tainted <= acc.balance
        assert acc.lien_amount == min(acc.tainted, acc.balance)
        assert acc.tainted == acc.tainted_in - acc.tainted_out - acc.unrecoverable_out   # per-account flow
        if acc.tainted_in:
            assert acc.layer is not None and acc.layer >= 1
    shuffled = list(rest)
    random.Random(len(rest)).shuffle(shuffled)                                       # input order never matters
    again = compute(fraud, opening, shuffled)
    assert {a: (x.tainted, x.balance) for a, x in again.accounts.items()} == \
           {a: (x.tainted, x.balance) for a, x in r.accounts.items()}
    return r


@settings(max_examples=2000, deadline=None, suppress_health_check=HEALTH)
@given(networks())
def test_random_networks_keep_the_books(case):
    check_books(*case)


@settings(max_examples=1500, deadline=None, suppress_health_check=HEALTH)
@given(chains())
def test_chains_conserve_taint_at_every_depth(case):
    fraud, opening, rest = case
    r = check_books(fraud, opening, rest)
    reached = [a for a in r.accounts.values() if a.tainted_in]
    assert all(a.layer == int(a.account_id[1:-1]) for a in reached)                  # layer = hop count in a chain
