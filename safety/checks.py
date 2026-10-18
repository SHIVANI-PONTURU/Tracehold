"""The seven deterministic checks (implementation plan, Appendix G), run by code on the Adjudicator's proposals
before the Verifier agent sees them.

 1. Every claim cites at least one transfer ID, and each exists in the network graph.
 2. Every amount in a claim equals the tool-calculated value for that account.
 3. Every proposed lien is on an account the trace reached, with a matching decision.
 4. Total proposed liens do not exceed the disputed amount.
 5. The conservation check in Appendix D holds to the paisa.
 6. Every rationale cites only signals present in that account's record (and at least two of them).
 7. No lien is proposed on the victim's account.
"""
from __future__ import annotations

import re

from mcp_server import network
from mcp_server import tools as T
from memory import store

NAMES = {1: "every claim cites existing transfers", 2: "amounts equal the tool-calculated values",
         3: "liens only on traced accounts with a matching decision", 4: "total liens within the disputed amount",
         5: "taint conservation to the paisa", 6: "rationales cite only signals in the account's record",
         7: "no lien on the victim's account"}
MIN_SIGNALS = 2


def _present(record, name):
    """A signal is in the record when it has a value (0 and False are values; None and [] are not)."""
    v = record.get(name)
    return v is not None and v != []


def run(ctx, bank_id, investigation_id, proposal):
    """Run all seven checks; returns {"passed": bool, "results": [...], "failed": [{check, account_ids}]}."""
    mem, inv, comp = T.investigation(ctx, bank_id, investigation_id)
    trace = store.latest_handoff(mem, investigation_id, "trace")
    traced = [a["account_id"] for a in trace["payload"]["accounts"]] if trace else []
    accounts = proposal["accounts"]
    bad = {i: [] for i in range(1, 8)}

    # 1: transfer IDs exist
    cited = sorted({t for a in accounts for t in a["evidence"]["transfer_ids"]})
    existing = {t.id for t in network.transfers_by_id(ctx.net, cited)} if cited else set()
    for a in accounts:
        ids = a["evidence"]["transfer_ids"]
        if not ids or any(t not in existing for t in ids):
            bad[1].append(a["account_id"])

    # 2, 3, 4, 5: amounts against the engine and the decisions
    res, _ = T._taint_for(ctx, comp, traced) if traced else (None, None)
    total = 0
    for a in accounts:
        if a["proposed_action"] != "lien":
            continue
        acc = a["account_id"]
        d = store.get_decision(mem, a["lien_proposal_id"]) if a.get("lien_proposal_id") else None
        try:                                    # the engine's amount after the overlap rule (earlier report first)
            expected = T.lien_cap(ctx, investigation_id, acc, res.accounts[acc]) if res and acc in res.accounts else None
        except Exception:                       # the bank could not say what is already held: not checkable
            expected = None
        if expected is None or a.get("lien_amount_paise") != expected or not d or int(d["lien_amount"]) != expected:
            bad[2].append(acc)
        if acc not in traced or not d or d["account_id"] != acc or d["investigation_id"] != investigation_id:
            bad[3].append(acc)
        total += int(a.get("lien_amount_paise") or 0)
        if acc == comp["victim_account_id"]:
            bad[7].append(acc)
    if total > comp["disputed_paise"]:
        bad[4] = [a["account_id"] for a in accounts if a["proposed_action"] == "lien"]
    if res is None or not res.conservation_ok():
        bad[5] = [a["account_id"] for a in accounts]

    # 6: cited signals exist in the account's record
    communities = {}
    if traced:
        try:
            communities = {r["account_id"]: r for r in T.mule_community(ctx, bank_id, investigation_id, traced)["accounts"]}
        except Exception:
            communities = {}
    for a in accounts:
        acc = a["account_id"]
        try:
            record = T._signal_record(ctx, mem, comp, investigation_id, acc)
        except Exception:
            bad[6].append(acc)
            continue
        size = communities.get(acc, {}).get("community_size", 0)
        record["mule_community"] = record["key_account"] = (size if size >= 2 else None)
        cited = a["evidence"]["signals"]
        in_text = {s for s in T.SIGNALS if re.search(rf"\b{s}\b", a["rationale"])}
        if (len(set(cited)) < MIN_SIGNALS or any(s not in T.SIGNALS or not _present(record, s) for s in cited)
                or any(not _present(record, s) for s in in_text)):
            bad[6].append(acc)

    results = [{"check": i, "name": NAMES[i], "passed": not bad[i], "account_ids": sorted(set(bad[i]))}
               for i in range(1, 8)]
    failed = [{"check": r["check"], "account_ids": r["account_ids"]} for r in results if not r["passed"]]
    return {"passed": not failed, "results": results, "failed": failed}
