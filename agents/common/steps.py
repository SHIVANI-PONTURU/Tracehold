"""Phase 3 step 5: one Step node after every node of every agent graph (episodic memory).

Each graph node is wrapped: the wrapper times the node, notes the tools it called (from the ToolClient's record),
the model tokens and dollars it used (from the model's counters), a short summary of its input and output, and
posts the Step through the handoff API (POST /handoff/step), which also renews the investigation's lease. Step IDs
are <investigation>-<agent>-<run>-<n>, so a retried post is the same Step. Recording never stops a run.
"""
from __future__ import annotations

import logging
import time
import uuid

log = logging.getLogger("tracehold.steps")


def _short(v, depth=0):
    """A few words about a value: lists by length, dicts by their keys, strings cut."""
    if isinstance(v, dict):
        if depth:
            return f"{{{len(v)} keys}}"
        return ", ".join(f"{k}={_short(x, 1)}" for k, x in list(v.items())[:8])
    if isinstance(v, (list, tuple, set)):
        return f"[{len(v)}]"
    if isinstance(v, float):
        return f"{v:.3g}"
    s = str(v)
    return s if len(s) <= 40 else s[:37] + "..."


def summarise(state, keys=None):
    if not isinstance(state, dict):
        return _short(state)
    items = {k: state[k] for k in (keys or state) if k in state and k not in ("started",)}
    return _short(items)[:480]


class StepRecorder:
    def __init__(self, handoff, agent, investigation_id, tools=None, model=None):
        self.handoff, self.agent, self.inv = handoff, agent, investigation_id
        self.tools, self.model = tools, model
        self.run = uuid.uuid4().hex[:6]
        self.n = 0
        self.posted = []                                       # what was sent, for tests

    def _counters(self):
        m = self.model
        return (len(self.tools.calls) if self.tools is not None else 0, int(getattr(m, "tokens", 0) or 0),
                float(getattr(m, "spent", 0.0) or 0.0))

    def wrap(self, name, fn):
        def node(state):
            calls0, tok0, usd0 = self._counters()
            t0 = time.time()
            status, out = "ok", None
            try:
                out = fn(state)
                return out
            except Exception as e:
                status, out = "error", {"error": f"{type(e).__name__}: {str(e)[:200]}"}
                raise
            finally:
                self._post(name, state, out, status, t0, calls0, tok0, usd0)
        node.__name__ = name
        return node

    def _post(self, name, state, out, status, t0, calls0, tok0, usd0):
        calls1, tok1, usd1 = self._counters()
        made = self.tools.calls[calls0:calls1] if self.tools is not None else []
        tools = [c[0] + ("!" if isinstance(c[2], dict) and "error" in c[2] else "") for c in made]
        used = sorted(set(tools), key=tools.index)
        if tok1 > tok0:
            used.append("model")
        self.n += 1
        step = {"id": f"STEP-{self.inv[4:]}-{self.agent}-{self.run}-{self.n}", "agent": self.agent, "node": name,
                "tool": ", ".join(f"{t}x{tools.count(t)}" if tools.count(t) > 1 else t for t in used) or "-",
                "input_summary": summarise(state), "output_summary": summarise(out),
                "latency_ms": int((time.time() - t0) * 1000), "tokens": tok1 - tok0, "usd": round(usd1 - usd0, 6),
                "started_ms": int(t0 * 1000), "status": status}
        self.posted.append(step)
        try:
            self.handoff.step(self.inv, step)
        except Exception as e:                                 # recording never stops a run
            log.warning("could not record step %s: %s", step["id"], e)


def wrapper(recorder):
    """recorder.wrap, or a no-op when there is no recorder."""
    return recorder.wrap if recorder is not None else (lambda name, fn: fn)
