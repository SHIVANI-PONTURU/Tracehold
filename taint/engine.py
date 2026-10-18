"""Phase 2 step 1: the pro-rata taint engine (implementation plan, Appendix D). Plain Python, no database.

Every outgoing payment carries the same tainted share as the account's balance at that moment. For each
tracked account the engine holds the balance B and the tainted amount T, in integer paise:

    inflow a, of which a_t is tainted:   B += a;  T += a_t
    outflow o:                           t_out = floor(o * T / B);  T -= t_out;  B -= o

and t_out arrives at the receiving account as its tainted inflow. Rounding down keeps the remainder on the
sending account, so the totals still balance to the paisa.

Rules fixed here (Appendix D edge cases):
- Order: strictly by timestamp, then by transfer ID. IBM's times have one-minute resolution, so transfers in
  the same minute are ordered by transfer ID, which is IBM's row order. Phase 1 measured how often this
  matters: 0.03% of inflows to mules (0.40% for genuine accounts) are forwarded within the same minute.
  Any order within a minute keeps balances non-negative, because the overlay's opening balances already
  cover outflows-before-inflows inside every minute.
- Every transfer is processed once, so money that comes back to an account (a cycle) is just another inflow.
- Same-account transfers (self_loop) move no money and are skipped.
- Several fraud transfers: each carries its whole amount as taint, and an account receiving more than one
  sums them.
- Exits: the tainted share of an outflow in an exit format (Cash, Bitcoin) leaves the banking graph and is
  recorded as unrecoverable; the receiving side gets no taint.
- Untracked accounts: taint that reaches an account whose history was not supplied is held there as
  "frontier" taint. The Tracer decides whether to follow it (tiny branches stop below a threshold).

Conservation (also deterministic check 5): tainted still held by tracked accounts + frontier taint +
unrecoverable taint = disputed amount, to the paisa.
"""
from __future__ import annotations

from dataclasses import dataclass, field

EXIT_FORMATS = frozenset({"Cash", "Bitcoin"})


class TaintError(ValueError):
    """The inputs cannot be processed (for example an account's history is incomplete)."""


@dataclass(frozen=True)
class Transfer:
    id: str
    src: str
    dst: str
    amount: int                 # paise, at least 1
    ts: int                     # UTC epoch seconds
    format: str = ""
    self_loop: bool = False


@dataclass
class AccountTaint:
    account_id: str
    opening_balance: int
    balance: int = 0            # after the last processed transfer
    tainted: int = 0            # T: tainted amount still held
    tainted_in: int = 0         # tainted inflow received in total
    tainted_out: int = 0        # tainted amount passed on to other accounts
    unrecoverable_out: int = 0  # tainted amount that left through an exit format
    layer: int | None = None    # fewest hops from the victim at which taint arrived
    first_tainted_ts: int | None = None
    last_ts: int | None = None  # time of the last transfer processed

    @property
    def tainted_share(self) -> float:
        return self.tainted / self.balance if self.balance else 0.0

    @property
    def lien_amount(self) -> int:
        """Remaining tainted amount, never more than the current balance (Appendix D)."""
        return max(0, min(self.tainted, self.balance))


@dataclass(frozen=True)
class TaintedTransfer:
    """A transfer that carried taint, with the tainted part it carried."""
    transfer_id: str
    src: str
    dst: str
    amount: int
    tainted: int
    ts: int
    layer: int                  # layer of the receiving side (1 = the fraud transfers)
    exit: bool = False


@dataclass
class TaintResult:
    disputed: int
    accounts: dict[str, AccountTaint]
    frontier: dict[str, int] = field(default_factory=dict)          # untracked account -> taint received
    frontier_layer: dict[str, int] = field(default_factory=dict)
    frontier_first_ts: dict[str, int] = field(default_factory=dict)            # when taint first arrived
    frontier_from: dict[str, set] = field(default_factory=dict)                # who sent it
    unrecoverable: int = 0
    carried: list[TaintedTransfer] = field(default_factory=list)

    @property
    def held(self) -> int:
        return sum(a.tainted for a in self.accounts.values())

    def conservation_gap(self) -> int:
        """Disputed amount minus everything accounted for; 0 when the books balance."""
        return self.disputed - (self.held + sum(self.frontier.values()) + self.unrecoverable)

    def conservation_ok(self) -> bool:
        return self.conservation_gap() == 0

    def transfers_used(self, account_id: str) -> list[str]:
        """IDs of the transfers that carried taint into or out of an account, in time order."""
        return [c.transfer_id for c in self.carried if account_id in (c.src, c.dst)]


def compute(fraud_transfers, opening_balances, transfers, *, exit_formats=EXIT_FORMATS, as_of=None):
    """Run the engine.

    fraud_transfers   the complaint's fraud transfers (Transfer), each fully tainted; they leave the victim
    opening_balances  {account_id: opening balance in paise} for every tracked account
    transfers         every transfer of every tracked account (Transfer), from its opening; duplicates and
                      transfers between untracked accounts are ignored
    as_of             optional: ignore transfers after this timestamp
    """
    fraud_transfers = list(fraud_transfers)
    if not fraud_transfers:
        raise TaintError("no fraud transfers")
    fraud_ids = {f.id for f in fraud_transfers}
    victims = {f.src for f in fraud_transfers}
    if len(victims) != 1:
        raise TaintError(f"fraud transfers leave more than one account: {sorted(victims)}")
    victim = next(iter(victims))
    if victim in opening_balances:
        raise TaintError("the victim's account cannot be tracked: its money is the source of the taint")

    accounts = {a: AccountTaint(a, int(b), balance=int(b)) for a, b in opening_balances.items()}
    for a in accounts.values():
        if a.balance < 0:
            raise TaintError(f"{a.account_id}: negative opening balance")

    seen, ordered = set(), []
    for t in [*fraud_transfers, *transfers]:
        if t.id in seen:
            continue
        seen.add(t.id)
        if t.self_loop or t.src == t.dst:
            continue
        if as_of is not None and t.ts > as_of:
            continue
        if t.id not in fraud_ids and t.src not in accounts and t.dst not in accounts:
            continue
        if t.amount <= 0:
            raise TaintError(f"{t.id}: amount must be at least 1 paisa")
        ordered.append(t)
    ordered.sort(key=lambda t: (t.ts, t.id))

    result = TaintResult(disputed=sum(f.amount for f in fraud_transfers), accounts=accounts)
    for t in ordered:
        carried, layer, is_exit = 0, None, False
        if t.id in fraud_ids:
            carried, layer = t.amount, 1
        elif t.src in accounts:
            s = accounts[t.src]
            if s.balance < t.amount:
                raise TaintError(f"{t.id}: {t.src} would go below zero (balance {s.balance}, outflow {t.amount}); "
                                 f"its history is incomplete")
            carried = t.amount * s.tainted // s.balance if s.tainted else 0
            s.tainted -= carried
            s.balance -= t.amount
            s.last_ts = t.ts
            layer = (s.layer + 1) if s.layer is not None else None
            if carried and t.format in exit_formats:
                s.unrecoverable_out += carried
                result.unrecoverable += carried
                is_exit = True
            elif carried:
                s.tainted_out += carried
        if t.dst in accounts:
            d = accounts[t.dst]
            d.balance += t.amount
            d.last_ts = t.ts
            if carried and not is_exit:
                d.tainted += carried
                d.tainted_in += carried
                d.layer = layer if d.layer is None else min(d.layer, layer)
                if d.first_tainted_ts is None:
                    d.first_tainted_ts = t.ts
        elif carried and not is_exit:
            result.frontier[t.dst] = result.frontier.get(t.dst, 0) + carried
            prev = result.frontier_layer.get(t.dst)
            result.frontier_layer[t.dst] = layer if prev is None else min(prev, layer)
            result.frontier_first_ts.setdefault(t.dst, t.ts)
            result.frontier_from.setdefault(t.dst, set()).add(t.src)
        if carried:
            result.carried.append(TaintedTransfer(t.id, t.src, t.dst, t.amount, carried, t.ts, layer or 0, is_exit))

    for a in accounts.values():
        if a.tainted > a.balance:                       # cannot happen with floor rounding; checked anyway
            raise TaintError(f"{a.account_id}: tainted {a.tainted} exceeds balance {a.balance}")
    gap = result.conservation_gap()
    if gap:
        raise TaintError(f"conservation fails by {gap} paise")
    return result
