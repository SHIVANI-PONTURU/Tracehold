"""Phase 4 step 9: failure injection. Every run either completes or ends as failed with a reason; none hangs.

Without a database: a model timeout, a tool-server (MCP) error and a crash in each agent's run end the investigation
as failed with the reason (worker.handle); a tool error stops the Tracer's graph at once with the tool named; the
wall-clock guard fails a run that runs too long. Covered elsewhere: rate limits (HTTP 429) are retried with backoff
(test_anthropic_client.py), the spend caps refuse further calls (test_model_client.py), the clients retry a tool
server that is briefly unreachable (test_clients_retry.py).

On the server (integration): the Tracer and the Verifier are killed half way through a run and the restarted worker
takes the run back and finishes it with nothing done twice (the Adjudicator's case is in test_harness.py).
"""
import time

import pytest

pytest.importorskip("langgraph")

from agents import worker                                   # noqa: E402
from agents.common import clients, model as M               # noqa: E402
from agents.common.config import load                       # noqa: E402
from agents.common.model import wall_clock                  # noqa: E402
from agents.tracer import graph as TG                       # noqa: E402
from eval import load_test as LT                            # noqa: E402

INV = "INV-0184-abc123def456"


class RecordingHandoff:
    bank_id = "11"

    def __init__(self):
        self.failed = []

    def fail(self, inv, agent, reason):
        self.failed.append((inv, agent, reason))
        return {"failed": True}


def boom(exc):
    def f(*a, **k):
        raise exc
    return f


@pytest.mark.parametrize("agent,exc,reason", [
    ("tracer", TimeoutError("the model did not answer within 300 s"), "TimeoutError: the model did not answer"),
    ("adjudicator", RuntimeError("compute_taint failed: unreachable: ConnectError"), "RuntimeError: compute_taint failed"),
    ("verifier", ConnectionResetError("the sandbox was deleted"), "ConnectionResetError: the sandbox was deleted"),
])
def test_a_failing_run_ends_as_failed_with_its_reason(monkeypatch, agent, exc, reason):
    h = RecordingHandoff()
    monkeypatch.setattr(worker, "run_tracer", boom(exc))
    monkeypatch.setattr(worker, "run_adjudicator", boom(exc))

    class Ver:
        start = staticmethod(boom(exc))
    worker.handle(load(), agent, h, Ver(), {"investigation_id": INV, "complaint_id": "CMP-0184"})
    assert len(h.failed) == 1 and h.failed[0][:2] == (INV, agent) and h.failed[0][2].startswith(reason)


def test_a_tool_error_stops_the_tracer_at_once_naming_the_tool():
    class DownTools:
        calls = 0

        def call(self, tool, **args):
            DownTools.calls += 1
            return {"error": {"code": "unreachable", "message": "ConnectError: tool server down"}}
    t0 = time.time()
    with pytest.raises(RuntimeError, match="compute_taint failed: unreachable"):
        TG.run(load(), DownTools(), M.FakeModel([]), "11", INV, "CMP-0184", "t")
    assert DownTools.calls == 1 and time.time() - t0 < 5


def test_the_wall_clock_guard_fails_a_run_that_runs_too_long():
    wall_clock(time.time(), 900, "the Tracer")                     # within the limit: nothing happens
    with pytest.raises(TimeoutError, match="the Tracer ran longer than 900 s"):
        wall_clock(time.time() - 901, 900, "the Tracer")


# ---- on the server: kill a worker half way, restart it ---------------------------------------------
class Killed(RuntimeError):
    """The worker process dying mid-run."""


@pytest.mark.integration
@pytest.mark.data
def test_a_killed_tracer_and_verifier_are_taken_back_and_finish(servers, tool_ctx, monkeypatch, tmp_path):   # noqa: F811
    from memory import store
    base, have_bank = servers
    if not have_bank:
        pytest.skip("the mock bank API needs fastapi")
    cfg = load()
    cfg["tool_server_url"] = base
    cfg["verifier"]["checkpoint_db"] = str(tmp_path / "gate.sqlite")
    mem = tool_ctx.memory("11")
    for i in store.list_investigations(mem):                     # leave only this test's run in flight
        if i["status"] not in ("closed", "failed", "executed"):
            store.move(mem, i["id"], [i["status"]], "closed", closed_reason="test cleanup")
    h = clients.HandoffClient(base, "11")
    inv = h.start("CMP-0184")["investigation_id"]
    original = clients.ToolClient.call

    def dies_at(name):
        def call(self, tool, **args):
            if tool == name:
                raise Killed(f"killed at {name}")
            return original(self, tool, **args)
        return call

    monkeypatch.setattr(clients.ToolClient, "call", dies_at("trace_funds"))      # the Tracer dies in its first judge
    job = h.claim("tracer")
    with pytest.raises(Killed):
        worker.run_tracer(cfg, h, job, model=M.FakeModel([LT.follow] * 10))
    assert h.detail(inv)["investigation"]["status"] == "tracing"
    monkeypatch.setattr(clients.ToolClient, "call", original)
    again = h.claim("tracer", resume=True)                                         # the restarted worker
    assert again["investigation_id"] == inv and again["resumed"] is True
    assert worker.run_tracer(cfg, h, again, model=M.FakeModel([LT.follow] * 10))["status"] == "traced"

    assert worker.run_adjudicator(cfg, h, h.claim("adjudicator"), model=M.FakeModel([LT.adjudicate]))["status"] == "proposed"
    monkeypatch.setattr(clients.ToolClient, "call", dies_at("recall_context"))   # the Verifier dies while loading
    v = worker.Verifier(cfg, h, model_factory=lambda j: M.FakeModel([LT.verify]))
    job = h.claim("verifier")
    with pytest.raises(Killed):
        v.start(job)
    assert h.detail(inv)["investigation"]["status"] == "verifying"
    monkeypatch.setattr(clients.ToolClient, "call", original)
    v2 = worker.Verifier(cfg, h, model_factory=lambda j: M.FakeModel([LT.verify]))
    again = h.claim("verifier", resume=True)
    assert again["investigation_id"] == inv and again["resumed"] is True
    v2.start(again)
    detail = h.detail(inv)
    assert detail["investigation"]["status"] == "at_gate" and detail["investigation"]["reclaims"] >= 2
    kinds = sorted((a["kind"], a["entity_id"]) for a in detail["actions"])
    assert len(kinds) == len(set(kinds)) and len(detail["decisions"]) == 1          # nothing done twice
    assert {v["verdict"] for v in h.read(inv, "verdict")["payload"]["verdicts"]} == {"approve"}
    h.close(inv, "test finished")


from tests.test_tool_server import servers  # noqa: E402,F401  (fixture: tool server + mock bank)
