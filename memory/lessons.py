"""Phase 3 steps 7-8: lessons, the bank's procedural memory. Pure rules here; memory/store.py reads and writes them.

A lesson is made at the HITL gate from a decision with a reason:
- a lien rejected with a reason (the Verifier's rejection that stands, or a human's) -> stance "release": liens
  like this one should not be placed;
- a human approval that overrides the Verifier's rejection -> stance "hold": liens like this one should be placed.
A Verifier approval that nobody overrides teaches nothing new and makes no lesson.

Each lesson keeps what it applies to: the account, that account's devices and phones (so a later case reaches it
through a shared identifier), and the account's label. Later gate decisions it applies to, on accounts with the same
label, confirm or contradict it:
  confirmed:     confidence + confirm_step, times_confirmed + 1
  contradicted:  confidence - contradict_step, times_contradicted + 1; below deactivate_below it stops being active
A new lesson with the opposite stance on the same entities supersedes the old one (SUPERSEDED_BY), which stops
being active. Every change is kept in the lesson's history.

An analyst resolving a review in a later case scores the lessons on that account the same way (decided 17 Oct, 18:30):
"cleared" (the account looks genuine) counts like a rejected lien, "escalated" like an approved one. A review never
makes a lesson; lessons still come only from the gate.
"""
from __future__ import annotations

DEFAULTS = {"initial_confidence": {"human": 0.7, "verifier_agent": 0.5}, "confirm_step": 0.1,
            "contradict_step": 0.2, "deactivate_below": 0.3, "max_scope_identifiers": 12}


def settings(cfg=None):
    s = dict(DEFAULTS)
    s.update((cfg or {}).get("lessons", {}) or {})
    return s


NO_REASON = {                         # texts code writes when nobody gave a reason: never a lesson
    "A deterministic check failed for this account, so it cannot be approved.", "The Verifier gave no verdict on it.",
    "Not confirmed at the gate.", "Decided at the gate.", "Overridden at the gate.", "Confirmed at the gate.",
    "No reason given."}


def lesson_from_gate(approved, decided_by, reason, verifier_verdict):
    """(stance, source) for the lesson a gate decision makes, or None when it makes none."""
    reason = (reason or "").strip()
    if not reason or reason in NO_REASON:
        return None
    if not approved:
        return "release", decided_by
    if decided_by == "human" and verifier_verdict == "reject":
        return "hold", "human"
    return None


def effect(stance, approved):
    """+1 when a gate decision agrees with the lesson, -1 when it goes against it."""
    return 1 if (stance == "hold") == bool(approved) else -1


def review_effect(stance, outcome):
    """+1 or -1 for a resolved review: "escalated" reads as an approved lien, "cleared" as a rejected one."""
    return effect(stance, approved=outcome == "escalated")


def review_counts(lesson, inv_id, review_ts, label):
    """A resolved review scores a lesson only from a later case: another investigation than the one that made the
    lesson, a review opened after the lesson was made, and an account with the lesson's label."""
    return (not lesson["id"].startswith(f"LES-{inv_id[4:]}-") and review_ts is not None
            and int(review_ts) >= int(lesson.get("created_ts") or 0) and scored_by(lesson, label))


def apply(lesson, eff, decision_id, ts, cfg=None, key="decision_id"):
    """The lesson's new fields after one confirming (+1) or contradicting (-1) gate decision, or resolved review
    (key="action_id": the review's ID goes in the history)."""
    s = settings(cfg)
    c = float(lesson.get("confidence", 0.5))
    if eff > 0:
        c = min(1.0, c + s["confirm_step"])
        upd = {"times_confirmed": int(lesson.get("times_confirmed", 0)) + 1}
    else:
        c = max(0.0, c - s["contradict_step"])
        upd = {"times_contradicted": int(lesson.get("times_contradicted", 0)) + 1}
    c = round(c, 4)
    active = bool(lesson.get("active", True)) and c >= s["deactivate_below"]
    hist = list(lesson.get("history") or [])
    hist.append({"ts": ts, "event": "confirmed" if eff > 0 else "contradicted", key: decision_id, "confidence": c})
    if lesson.get("active", True) and not active:
        hist.append({"ts": ts, "event": "deactivated", key: decision_id, "confidence": c})
    return {**upd, "confidence": c, "active": active, "history": hist[-50:]}


def scored_by(lesson, label):
    """A gate decision confirms or contradicts a lesson only when the decided account has the lesson's label: a lesson
    about a likely-genuine account is not judged by a lien on a mule that shares its phone (decided 17 Oct, after the
    live run). A lesson without a label takes every decision."""
    return not lesson.get("label") or lesson.get("label") == (label or "")


def supersedes(new_stance, old):
    """A new lesson supersedes an active older one with the opposite stance on the same entities."""
    return bool(old.get("active")) and old.get("stance") != new_stance


def scope(account_id, identifiers, label, cfg=None):
    """applies_to: the account, its devices and phones (capped), and its label."""
    s = settings(cfg)
    ids = [f"account:{account_id}"] + [f"{k}:{v}" for k, v in identifiers][: s["max_scope_identifiers"]]
    return ids + ([f"label:{label}"] if label else [])


def entity_ids(applies_to):
    """The entity IDs in a scope (accounts, devices, phones), without the label."""
    return [x.split(":", 1)[1] for x in applies_to if not x.startswith("label:")]
