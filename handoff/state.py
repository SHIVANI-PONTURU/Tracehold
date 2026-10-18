"""Phase 3 step 2: the run state machine. Investigation.status changes only through the handoff API, and only along
these transitions; memory.store.move refuses anything else.

  new --tracer claims--> tracing --trace--> traced --adjudicator claims--> adjudicating --proposal-->
      checks pass:               proposed --verifier claims--> verifying --verdict--> at_gate --gate--> executed
      checks fail (first time):  checks_failed --adjudicator claims--> adjudicating (one retry)
      checks fail again:         proposed, marked failed with no liens --> verifier --> at_gate --> closed
  tracing, adjudicating, verifying --> themselves: a crashed worker's claim taken back (lease expired, or the
                                       worker restarted)
  any status before the gate's end --> failed (a run guard stopped the run; the reason is recorded)
  any status before the gate's end --> closed (an analyst closed the case)

The plan names the gate status awaiting_gate; TraceHold keeps at_gate, and adds executed for a run that placed liens.
"""
STATUSES = ["new", "tracing", "traced", "adjudicating", "proposed", "checks_failed", "verifying", "at_gate",
            "executed", "closed", "failed"]
FINAL = {"executed", "closed", "failed"}
WORKING = {"tracer": "tracing", "adjudicator": "adjudicating", "verifier": "verifying"}

_FORWARD = {
    "new": {"tracing"},
    "tracing": {"traced", "tracing"},
    "traced": {"adjudicating"},
    "adjudicating": {"proposed", "checks_failed", "adjudicating"},
    "checks_failed": {"adjudicating"},
    "proposed": {"verifying"},
    "verifying": {"at_gate", "verifying"},
    "at_gate": {"executed", "closed"},
}
TRANSITIONS = {s: (_FORWARD.get(s, set()) | ({"failed", "closed"} if s not in FINAL else set())) for s in STATUSES}


class IllegalTransition(ValueError):
    pass


def allowed(frm, to):
    return to in TRANSITIONS.get(frm, set())


def check(from_statuses, to):
    """The statuses in `from_statuses` that may move to `to`; raises IllegalTransition when there are none."""
    if to not in STATUSES:
        raise IllegalTransition(f"unknown status {to!r}")
    ok = [f for f in from_statuses if allowed(f, to)]
    if not ok:
        raise IllegalTransition(f"no transition from {sorted(from_statuses)} to {to}")
    return ok
