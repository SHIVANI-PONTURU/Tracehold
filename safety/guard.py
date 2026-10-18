"""The execute_lien guard (implementation plan, Appendix G): code, not prompt.

A lien runs only when the Decision exists, belongs to this investigation, is approved, records who decided
and has passed the HITL gate (only the gate code ever sets `approved`), carries a positive amount no larger
than the disputed sum, and has not been executed before (the lien ID makes it idempotent).
"""
from memory import store

GATE_MODES = ("confirm", "placeholder")


class Refused(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code, self.message = code, message


def evaluate(decision, investigation_id, complaint):
    """The guard's rules on one decision (no database): raises Refused, or returns None when it may run."""
    d = decision
    if not d:
        raise Refused("no_decision", "no such decision")
    if d.get("investigation_id") != investigation_id:
        raise Refused("wrong_investigation", f"{d.get('id')} belongs to {d.get('investigation_id')}")
    if d.get("approved") is not True:
        raise Refused("not_approved", f"{d.get('id')} is not approved"
                      + (" (rejected)" if d.get("approved") is False else " (no gate decision yet)"))
    if not d.get("decided_by"):
        raise Refused("no_decider", f"{d.get('id')} does not record who decided")
    if d.get("gate_mode") not in GATE_MODES or not d.get("gate_ts"):
        raise Refused("gate_not_passed", f"{d.get('id')} has not passed the HITL gate")
    if d.get("proposed_action") != "lien":
        raise Refused("not_a_lien", f"{d.get('id')} does not propose a lien")
    amount = int(d.get("lien_amount") or 0)
    if amount <= 0 or amount > int(complaint["disputed_paise"]):
        raise Refused("bad_amount", f"{d.get('id')}: amount {amount} is outside 1..disputed amount")
    if d.get("account_id") == complaint["victim_account_id"]:
        raise Refused("victim", "no lien on the victim's account")


def check_execute(mem, decision_id, investigation_id, complaint):
    """Return (decision, existing_lien): existing_lien is set when it already ran. Refusals become ToolError."""
    from mcp_server.tools import ToolError
    d = store.get_decision(mem, decision_id)
    try:
        evaluate(d, investigation_id, complaint)
    except Refused as r:
        raise ToolError(f"refused_{r.code}", r.message)
    return d, store.lien_for(mem, decision_id)
