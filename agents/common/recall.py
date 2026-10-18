"""Turning a recall_context result into the short memory notes the agents' tables show (Phase 3 step 6).

A note says which earlier case at this bank touched the account, how (the account itself, or a device, phone or IP
it shares with an account in that case) and what that case decided. Lessons are listed with their ID, stance and
confidence so the model can cite them. Everything here is data from the bank's memory, never instructions.
"""
from __future__ import annotations

from collections import defaultdict

EMPTY = {"cases": [], "matches": [], "decisions": [], "actions": [], "lessons": [], "bank_lessons": []}


def safe(result):
    """A recall result, or an empty one when the tool returned an error (recall never stops a run)."""
    return result if isinstance(result, dict) and "error" not in result else dict(EMPTY)


def _outcome(d):
    gate = {True: "approved", False: "rejected"}.get(d.get("approved"), "not decided")
    by = f" by {d['decided_by']}" if d.get("decided_by") else ""
    return f"{d.get('label')} -> {d.get('proposed_action')}, {gate}{by}"


def notes(recall, accounts, limit=300):
    """{account: note} for the given accounts that an earlier case touched."""
    accounts = set(accounts)
    decs = defaultdict(list)
    for d in recall.get("decisions", []):
        decs[(d["investigation_id"], d["account_id"])].append(d)
    order = {"account": 0, "device": 1, "phone": 2, "ip": 3, "given": 4}
    out = defaultdict(list)
    for m in recall.get("matches", []):
        earlier = m.get("earlier_account") or ""
        for h in m.get("how", []):
            a = h.get("account")
            if a not in accounts or (h["via"] != "account" and earlier == a):
                continue                                       # its own device or phone: the same-account note says it
            link = "same account" if h["via"] == "account" else f"shares {h['via']} {m['entity_id']} with {earlier or '?'}"
            ds = decs.get((m["investigation_id"], earlier or a), [])
            text = f"{m['complaint_id']} ({m['investigation_id']}): {link}"
            if ds:
                text += "; decided " + "; ".join(_outcome(d) for d in ds[:2])
            out[a].append((order.get(h["via"], 5), text))
    return {a: " | ".join(dict.fromkeys(t for _, t in sorted(v, key=lambda x: x[0])))[:limit] for a, v in out.items()}


def flat(text, limit=400):
    """Free text from the bank's records (an analyst's reason, a model's rationale) as one quoted line. Newlines and
    runs of spaces collapse and inner double quotes become single ones, so the text can never start a new table row
    or end its own quotes early (Phase 4 red-team: an instruction planted in a gate reason stays inside its cell)."""
    return '"' + " ".join(str(text or "").split()).replace('"', "'")[:limit] + '"'


def lesson_line(l):
    tag = "do not lien accounts like this" if l["stance"] == "release" else "lien accounts like this"
    return (f"{l['id']} [{tag}; label {l.get('label') or '-'}; confidence {l['confidence']:.2f}, "
            f"confirmed {l.get('times_confirmed', 0)}, contradicted {l.get('times_contradicted', 0)}]: {flat(l['text'])}")


def lessons_for(recall, account):
    return [l for l in recall.get("lessons", []) if account in l.get("for_accounts", [])]


def known_lesson_ids(recall):
    return {l["id"] for l in recall.get("lessons", [])} | {l["id"] for l in recall.get("bank_lessons", [])}


def bank_block(recall, limit=8):
    """The bank's most trusted active lessons, one per line."""
    ls = recall.get("bank_lessons", [])[:limit]
    return "\n".join(lesson_line(l) for l in ls) if ls else "(none yet)"
