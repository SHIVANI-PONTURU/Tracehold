"""Phase 4 steps 3 and 5: the four methods, the scoring, and the overlap rule. Pure functions, no database.

Every method decides, per account on a complaint's trail, "lien" (with an amount in paise), "review" or nothing.
The three baselines (project plan, section 6) work on the same code-only trail, the Tracer's loop with every branch
followed, so they differ only in what they decide:

  full_freeze   freezes every account the money touched: its whole balance
  greedy        marks up to the disputed amount at each hop, like today's money hunt: min(balance, disputed)
  rules_only    fixed thresholds on the account's signal record give a label, the Appendix G rubric gives the
                action, and the amount is TraceHold's (the remaining tainted amount, capped by the balance).
                Thresholds are chosen on dev complaints only (calibrate), never on test or the held-out pattern
  tracehold     the three agents, read from a real run: liens the Verifier approved (gate in placeholder mode,
                no human override) and the reviews the Adjudicator opened, on TraceHold's own trail

Scoring (the user's pick on 17 Oct: both money measures, IBM's label first): per complaint, rupees frozen on
accounts IBM labels genuine (innocent money), rupees frozen beyond the stolen money each account holds, stolen money
held, mule precision (liened accounts that are mules) and recall (trail mules with a lien or a review), and the
review queue. The held-out pattern (CYCLE, never used while the prompts were written) is reported as its own group.

Overlap rule (step 3, the user's pick): earlier report first. Complaints are processed in report order; each takes
at most what earlier complaints' liens leave of the account's balance (mcp_server/tools.lien_cap does the same in
the live system). overlap_check proves the two limits on every overlapping set.
"""
from __future__ import annotations

import itertools
import random
import statistics

from agents.adjudicator.graph import rubric_action

HELD_OUT = "CYCLE"
GROUPS = ("shallow", "medium", "deep", "held_out")
METHODS = ("full_freeze", "greedy", "rules_only", "tracehold")
SEED = 20261017
MAX_JUDGED = 40                     # the Adjudicator judges at most this many accounts, largest tainted first
HOLD_SHARE = 0.5                    # agents/config.yaml adjudicator.lien_whatever_label_share
HOLD_PART = 0.25                    # agents/config.yaml adjudicator.lien_whatever_label_part_of_disputed
SEED_B = 20261018                   # the second sample, disjoint from the first (version 2 of the rubric)

RULES = {"mule_pass_through": 0.9, "mule_max_payers": 5, "young_days": 180,
         "genuine_min_payers": 50, "old_days": 730, "low_share": 0.05}
RULES_GRID = {"mule_pass_through": [0.9, 0.8, 0.95], "mule_max_payers": [5, 3, 10], "young_days": [180, 90, 365],
              "genuine_min_payers": [50, 20], "old_days": [730, 365]}


def group_of(truth_row, held_out=HELD_OUT):
    return "held_out" if truth_row["pattern_type"] == held_out else truth_row["depth_bucket"]


def sample(truth, seed=SEED, per_bucket=12, held_out_n=10, split="test", held_out=HELD_OUT, exclude=()):
    """The complaints TraceHold runs on: per_bucket from each depth bucket (held-out pattern excluded) and
    held_out_n of the held-out pattern, from one split, drawn with a fixed seed, leaving out `exclude`.
    {complaint_id: group}."""
    rng = random.Random(seed)
    out = {}
    for g in GROUPS:
        ids = sorted(c for c, r in truth.items() if r["split"] == split and group_of(r, held_out) == g
                     and c not in exclude)
        n = held_out_n if g == "held_out" else per_bucket
        out.update({c: g for c in rng.sample(ids, min(n, len(ids)))})
    return dict(sorted(out.items()))


def sample_set(truth, name="a"):
    """Sample "a" (seed SEED, the first run) or "b" (seed SEED_B, sharing no complaint with "a"; it scores version 2
    of the rubric on complaints that did not shape it)."""
    first = sample(truth)
    return first if name == "a" else sample(truth, SEED_B, exclude=set(first))


def judged(accounts, n=MAX_JUDGED):
    """The accounts a decision is made on: those holding tainted money, largest first (as the Adjudicator)."""
    held = [a for a in accounts if a["tainted_paise"] > 0]
    return sorted(held, key=lambda a: (-a["tainted_paise"], a["account_id"]))[:n]


# ---- the baselines -------------------------------------------------------------------------------
def full_freeze(accounts):
    return {a["account_id"]: {"action": "lien", "amount": a["balance_paise"]} for a in accounts}


def greedy(accounts, disputed):
    return {a["account_id"]: {"action": "lien", "amount": min(a["balance_paise"], disputed)} for a in accounts}


def rules_label(sig, th=RULES):
    """A label from fixed thresholds on the signal record (Appendix G's typical signals, no model)."""
    mule = genuine = 0
    pt, payers, age = sig.get("pass_through_ratio"), sig.get("distinct_payers"), sig.get("account_age_days")
    share = sig.get("tainted_share")
    if pt is not None and pt >= th["mule_pass_through"]:
        mule += 1
    if payers is not None and payers <= th["mule_max_payers"]:
        mule += 1
    if age is not None and age <= th["young_days"]:
        mule += 1
    if (sig.get("devices_shared_with_known_mules") or 0) + (sig.get("phones_shared_with_known_mules") or 0) > 0:
        mule += 1
    if sig.get("registered_merchant"):
        genuine += 2
    if payers is not None and payers >= th["genuine_min_payers"]:
        genuine += 1
    if age is not None and age >= th["old_days"]:
        genuine += 1
    if share is not None and share <= th["low_share"]:
        genuine += 1
    if mule >= 2:
        return "mule" if genuine == 0 else "uncertain"
    return "likely_genuine"


def rules_only(accounts, signals, floor, th=RULES, hold_share=HOLD_SHARE, disputed=None, hold_part=HOLD_PART):
    out = {}
    for a in judged(accounts):
        label = rules_label(signals.get(a["account_id"], {}), th)
        share = a["tainted_paise"] / a["balance_paise"] if a["balance_paise"] else None
        part = a["tainted_paise"] / disputed if disputed else None
        action = rubric_action(label, None, a["tainted_paise"], floor, share=share, hold_share=hold_share,
                               part=part, hold_part=hold_part)
        if action == "lien" and a["lien_amount_paise"] > 0:
            out[a["account_id"]] = {"action": "lien", "amount": a["lien_amount_paise"], "label": label}
        else:
            out[a["account_id"]] = {"action": "review", "amount": 0, "label": label}
    return out


def tracehold(proposal, verdict):
    """TraceHold's decisions from a run's proposal and verdict handoffs: a lien only where the Verifier approved
    (placeholder gate, no override); a review where the Adjudicator opened one; nothing after a second check failure."""
    approved = {v["decision_id"] for v in (verdict or {}).get("verdicts", []) if v["verdict"] == "approve"}
    failed = bool((verdict or {}).get("checks_failed"))
    out = {}
    for a in (proposal or {}).get("accounts", []):
        acc, act = a["account_id"], a["proposed_action"]
        if act == "lien" and not failed and a.get("lien_proposal_id") in approved:
            out[acc] = {"action": "lien", "amount": int(a["lien_amount_paise"]), "label": a["label"]}
        elif act in ("review", "review_and_request_info"):      # a rejected lien is no action at all
            out[acc] = {"action": "review", "amount": 0, "label": a["label"]}
        else:
            out[acc] = {"action": "none", "amount": 0, "label": a["label"]}
    return out


# ---- scoring -------------------------------------------------------------------------------------
def score_case(decisions, accounts, labels, mules_on_trail):
    """One complaint, one method. labels: {account_id: label}, genuine when missing; mules_on_trail: the true trail's
    mules (eval/labels/trail_accounts.csv)."""
    tainted = {a["account_id"]: a["tainted_paise"] for a in accounts}
    liened = {a: d["amount"] for a, d in decisions.items() if d["action"] == "lien"}
    reviewed = {a for a, d in decisions.items() if d["action"] == "review"}
    genuine = {a for a in liened if labels.get(a, "genuine") != "mule"}
    return {"frozen": sum(liened.values()),
            "innocent": sum(liened[a] for a in genuine),
            "innocent_accounts": len(genuine),
            "beyond_taint": sum(max(0, v - tainted.get(a, 0)) for a, v in liened.items()),
            "stolen_held": sum(min(v, tainted.get(a, 0)) for a, v in liened.items()),
            "liens": len(liened), "liened_mules": len(liened) - len(genuine), "reviews": len(reviewed),
            "trail_mules": len(mules_on_trail), "caught": len(mules_on_trail & (set(liened) | reviewed)),
            "mules_liened": len(mules_on_trail & set(liened))}


SUMS = ("frozen", "innocent", "innocent_accounts", "beyond_taint", "stolen_held", "liens", "liened_mules", "reviews",
        "trail_mules", "caught", "mules_liened", "disputed")


def _ratio(a, b):
    return round(a / b, 4) if b else None


def aggregate(scores, extra=None):
    """Sums and rates over a list of per-complaint scores. Precision and recall are pooled over the complaints."""
    s = {k: sum(x.get(k, 0) for x in scores) for k in SUMS}
    n = len(scores)
    out = {"complaints": n, **s,
           "precision": _ratio(s["liened_mules"], s["liens"]), "recall": _ratio(s["caught"], s["trail_mules"]),
           "lien_recall": _ratio(s["mules_liened"], s["trail_mules"]),
           "innocent_per_case": round(s["innocent"] / n) if n else 0,
           "reviews_per_case": round(s["reviews"] / n, 2) if n else 0,
           "innocent_share_of_frozen": _ratio(s["innocent"], s["frozen"])}
    for k in ("usd", "seconds"):
        vals = [x[k] for x in scores if x.get(k) is not None]
        if vals:
            out[f"median_{k}"] = round(statistics.median(vals), 4)
            out[f"total_{k}"] = round(sum(vals), 4)
    out.update(extra or {})
    return out


def f1_of(scores):
    s = aggregate(scores)
    p, r = s["precision"] or 0, s["lien_recall"] or 0
    return round(2 * p * r / (p + r), 4) if p + r else 0.0


def calibrate(dev_cases, labels, mules, floor, grid=RULES_GRID, hold_share=HOLD_SHARE, hold_part=HOLD_PART):
    """The rules-only thresholds with the best lien F1 (precision of liens, recall of trail mules liened) on dev
    complaints. Ties keep the earlier grid point, so the defaults win a tie. Returns (thresholds, f1)."""
    keys = list(grid)
    best, best_f1 = dict(RULES), -1.0
    for combo in itertools.product(*(grid[k] for k in keys)):
        th = dict(RULES, **dict(zip(keys, combo)))
        scores = [score_case(rules_only(c["accounts"], c["signals"], floor, th, hold_share, c.get("disputed_paise"),
                                        hold_part), c["accounts"], labels,
                             mules.get(c["complaint_id"], set())) for c in dev_cases]
        f1 = f1_of(scores)
        if f1 > best_f1:
            best, best_f1 = th, f1
    return best, best_f1


# ---- overlapping complaints (step 3) -------------------------------------------------------------
def allocate(claims, balance):
    """Earlier report first on one account: claims are (reported_at, complaint_id, amount); each complaint gets at
    most what the earlier ones left of the balance. {complaint_id: allocated paise}."""
    left, out = balance, {}
    for _, cid, amount in sorted(claims):
        got = max(0, min(amount, left))
        out[cid] = out.get(cid, 0) + got
        left -= got
    return out


def overlap_check(cases, amount_key="lien_amount_paise"):
    """cases: {complaint_id: {"reported_at", "disputed_paise", "accounts": [...]}}, every complaint's trail.
    Applies the rule to every account more than one complaint reaches and checks both limits: an account's liens
    never exceed its balance, a complaint's liens never exceed its disputed amount."""
    by_acc = {}
    for cid, c in cases.items():
        for a in c["accounts"]:
            by_acc.setdefault(a["account_id"], []).append((c["reported_at"], cid, int(a[amount_key]), a["balance_paise"]))
    shared = {acc: v for acc, v in by_acc.items() if len({cid for _, cid, _, _ in v}) > 1}
    alloc = {cid: {a["account_id"]: int(a[amount_key]) for a in c["accounts"]} for cid, c in cases.items()}
    account_bad, over_before, cut = [], [], 0
    for acc, claims in shared.items():
        balance = max(b for *_, b in claims)
        asked = sum(x for _, _, x, _ in claims)
        if asked > balance:
            over_before.append({"account_id": acc, "claimed": asked, "balance": balance})
        got = allocate([(t, cid, x) for t, cid, x, _ in claims], balance)
        for cid, g in got.items():
            cut += alloc[cid][acc] - g
            alloc[cid][acc] = g
        if sum(got.values()) > balance:
            account_bad.append(acc)
    complaint_bad = [cid for cid, c in cases.items() if sum(alloc[cid].values()) > c["disputed_paise"]]
    links = {}                                          # overlapping sets: complaints joined by shared accounts
    for claims in shared.values():
        ids = sorted({cid for _, cid, _, _ in claims})
        for i in ids:
            links.setdefault(i, set()).update(ids)
    seen, sets = set(), []
    for start in sorted(links):
        if start in seen:
            continue
        comp, todo = set(), [start]
        while todo:
            x = todo.pop()
            if x not in comp:
                comp.add(x)
                todo.extend(links[x] - comp)
        seen |= comp
        sets.append(sorted(comp))
    return {"complaints": len(cases), "overlapping_sets": len(sets), "complaints_in_sets": len(seen),
            "largest_set": max((len(s) for s in sets), default=0), "shared_accounts": len(shared),
            "claims_over_balance_before_rule": len(over_before), "paise_cut_by_rule": cut,
            "account_limit_holds": not account_bad, "complaint_limit_holds": not complaint_bad,
            "account_violations": account_bad[:20], "complaint_violations": complaint_bad[:20],
            "over_before_examples": over_before[:10]}
