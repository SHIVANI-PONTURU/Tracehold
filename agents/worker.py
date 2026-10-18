"""The agent workers. One process per agent, each in its own sandbox; each serves every bank in `bank_ids`.

  python -m agents.worker tracer          claim complaints, trace them, submit trace handoffs
  python -m agents.worker adjudicator     claim trace handoffs, decide, submit proposals
  python -m agents.worker verifier        claim proposals, submit verdicts, hold liens at the HITL gate, resume when
                                          the gate is decided (also after a restart), answer case-chat questions
  add --once to handle one piece of work and exit

The tool server must be running (python -m mcp_server.server), and the mock bank (python -m uvicorn
bank_api.app:app --port 8100). A failure, a runaway guard or the spend cap marks the investigation failed.

Phase 3 step 3, harness reliability:
- A worker that starts takes back its agent's unfinished work first (claim with resume): the run starts again from
  the stored handoffs, and the act tools are idempotent, so nothing is done twice.
- Every Step renews the claim's lease; a claim whose lease runs out is taken back by the next claim.
- The tool server being briefly unreachable no longer stops the worker: requests retry with backoff
  (agents/common/clients.py), and the loop waits and tries again.
- A gate decision whose checkpoint is gone (the sandbox was recreated) is replayed from the stored handoffs.
"""
from __future__ import annotations

import argparse
import logging
import time
import traceback

from agents.common import clients, lookups, model as M
from agents.common.config import load
from agents.common.steps import StepRecorder

log = logging.getLogger("tracehold.worker")


def make_model(cfg, handoff, inv, agent, callbacks):
    return M.make_model(cfg, M.SpendGuard(cfg, handoff, inv, agent), callbacks=callbacks)


def traced(cfg, agent, job, bank_id=None):
    """Langfuse (step 13): one agent observation in the investigation's trace; [] when tracing is off."""
    from agents.common import tracing
    return tracing.agent_run(agent, bank_id or cfg["bank_id"], job["investigation_id"], job.get("complaint_id", ""),
                             job.get("trace_id"))


def run_tracer(cfg, handoff, job, model=None):
    from agents.tracer import graph
    bank = handoff.bank_id
    tools = clients.ToolClient(cfg["tool_server_url"], "tracer", bank, job["investigation_id"])
    with traced(cfg, "tracer", job, bank) as cb:
        model = model or make_model(cfg, handoff, job["investigation_id"], "tracer", cb)
        rec = StepRecorder(handoff, "tracer", job["investigation_id"], tools, model)
        state = graph.run(cfg, tools, model, bank, job["investigation_id"], job["complaint_id"],
                          job["trace_id"], callbacks=cb, recorder=rec)
    return handoff.submit("trace", state["handoff"])


def run_adjudicator(cfg, handoff, job, model=None):
    from agents.adjudicator import graph
    bank = handoff.bank_id
    tools = clients.ToolClient(cfg["tool_server_url"], "adjudicator", bank, job["investigation_id"])
    with traced(cfg, "adjudicator", job, bank) as cb:
        model = model or make_model(cfg, handoff, job["investigation_id"], "adjudicator", cb)
        look = lookups.lookup_tools(tools, ["shared_attribute_cluster"]) if not isinstance(model, M.FakeModel) else ()
        rec = StepRecorder(handoff, "adjudicator", job["investigation_id"], tools, model)
        state = graph.run(cfg, tools, handoff, model, bank, job["investigation_id"], job["trace_id"],
                          lookup_tools=look, callbacks=cb, recorder=rec)
    return state["result"]


class Verifier:
    """Runs the Verifier graph up to the gate, resumes paused gates when a decision arrives (or replays them from
    the stored handoffs when the checkpoint is gone), and answers case-chat questions."""

    def __init__(self, cfg, handoff, model_factory=None):
        from agents.verifier import graph
        self.cfg, self.handoff, self.graph_mod = cfg, handoff, graph
        self.saver = graph.checkpointer(cfg["verifier"]["checkpoint_db"])
        self.model_factory = model_factory

    def _model(self, job, cb, agent="verifier"):
        return self.model_factory(job) if self.model_factory else \
            make_model(self.cfg, self.handoff, job["investigation_id"], agent, cb)

    def start(self, job):
        if job.get("resumed"):                            # a crashed run: start it again from its handoffs
            try:
                self.saver.delete_thread(job["investigation_id"])
            except Exception:
                pass
        bank = self.handoff.bank_id
        with traced(self.cfg, "verifier", job, bank) as cb:
            model = self._model(job, cb)
            tools = clients.ToolClient(self.cfg["tool_server_url"], "verifier", bank, job["investigation_id"])
            look = lookups.lookup_tools(tools, ["account_signals", "compute_taint"]) \
                if not isinstance(model, M.FakeModel) else ()
            rec = StepRecorder(self.handoff, "verifier", job["investigation_id"], tools, model)
            g = self.graph_mod.build(self.cfg, self.handoff, model, look, checkpointer=self.saver, tools=tools,
                                     recorder=rec)
            return g.invoke({"bank_id": bank, "investigation_id": job["investigation_id"],
                             "trace_id": job["trace_id"], "started": time.time()},
                            self.graph_mod.config(self.cfg, job["investigation_id"], cb))

    def _gate_graph(self, recorder=None):
        return self.graph_mod.build(self.cfg, self.handoff, M.FakeModel([]), (), checkpointer=self.saver,
                                    recorder=recorder)

    def paused(self):
        """Investigations waiting at the gate that have a checkpoint here."""
        out = []
        for inv in self.handoff.investigations():
            if inv["status"] != "at_gate":
                continue
            st = self._gate_graph().get_state({"configurable": {"thread_id": inv["id"]}})
            if st and st.next and "gate" in st.next:
                out.append(inv)
        return out

    def resume_ready(self):
        """Finish every gate whose decision has arrived: resume its checkpoint, or replay it from the stored
        verdict handoff when there is no checkpoint. Returns the investigations finished."""
        from langgraph.types import Command
        done = []
        for inv in self.handoff.investigations():
            if inv["status"] != "at_gate":
                continue
            g = self.handoff.gate(inv["id"])
            if g.get("decisions") is None:
                continue
            job = {"investigation_id": inv["id"], "trace_id": inv["trace_id"], "complaint_id": inv["complaint_id"]}
            rec = StepRecorder(self.handoff, "gate", inv["id"])
            graph = self._gate_graph(rec)
            st = graph.get_state({"configurable": {"thread_id": inv["id"]}})
            with traced(self.cfg, "gate", job, self.handoff.bank_id) as cb:
                if st and st.next and "gate" in st.next:
                    state = graph.invoke(Command(resume={"decisions": g["decisions"], "gate_mode": g.get("gate_mode")}),
                                         self.graph_mod.config(self.cfg, inv["id"], cb))
                    how = "resumed"
                else:
                    verdict = self.handoff.read(inv["id"], "verdict")
                    replay = rec.wrap("execute", lambda s: self.graph_mod.apply_gate(
                        self.cfg, self.handoff, inv["id"], (verdict or {}).get("payload", {}).get("verdicts", []),
                        {"decisions": g["decisions"], "gate_mode": g.get("gate_mode")}, inv["trace_id"]))
                    state = replay({"investigation_id": inv["id"], "replayed_from": "verdict handoff"})
                    how = "replayed"
            done.append({"investigation_id": inv["id"], "status": state.get("status"), "how": how})
        return done

    def answer_chat(self):
        """Answer the oldest waiting case-chat question at this bank, if any. Returns the outcome or None."""
        q = self.handoff.chat_claim()
        if not q:
            return None
        job = {"investigation_id": q["investigation_id"], "trace_id": q["trace_id"], "complaint_id": q["complaint_id"]}
        with traced(self.cfg, "chat", job, self.handoff.bank_id) as cb:
            model = self._model(job, cb, "chat")         # the ledger books it as chat, as the Steps and Langfuse do
            rec = StepRecorder(self.handoff, "chat", q["investigation_id"], None, model)
            ask = rec.wrap("answer", lambda s: self.graph_mod.answer_question(model, q["question"], q["context"],
                                                                             s.get("feedback")))
            a = ask({"question": q["question"]})
            r = self.handoff.chat_answer(q["message_id"], a.answer, a.cited_ids)
            if not r.get("accepted", True) and r.get("unknown_ids") and "message_id" not in r:
                a = ask({"question": q["question"], "feedback": r["unknown_ids"]})
                r = self.handoff.chat_answer(q["message_id"], a.answer, a.cited_ids, final=True)
        return {"message_id": q["message_id"], **r}


def handle(cfg, agent, handoff, ver, job):
    inv = job["investigation_id"]
    log.info("bank %s: claimed %s (%s)%s", handoff.bank_id, inv, job["complaint_id"],
             " to resume it" if job.get("resumed") else "")
    try:
        if agent == "tracer":
            r = run_tracer(cfg, handoff, job)
        elif agent == "adjudicator":
            r = run_adjudicator(cfg, handoff, job)
        else:
            ver.start(job)
            r = {"status": "at_gate"}
        log.info("%s -> %s", inv, r.get("status"))
    except Exception as e:
        log.error("%s failed: %s\n%s", inv, e, traceback.format_exc())
        try:
            handoff.fail(inv, agent, f"{type(e).__name__}: {e}")
        except Exception:
            pass


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("agent", choices=["tracer", "adjudicator", "verifier"])
    ap.add_argument("--once", action="store_true", help="handle one piece of work, then exit")
    ap.add_argument("--poll", type=float, default=2.0, help="seconds between claims")
    a = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    from dotenv import load_dotenv
    load_dotenv()
    cfg = load()
    banks = {b: clients.HandoffClient(cfg["tool_server_url"], b) for b in cfg["bank_ids"]}
    vers = {b: Verifier(cfg, h) for b, h in banks.items()} if a.agent == "verifier" else {}
    resume = set(banks)                                     # first claim per bank takes back unfinished work
    log.info("%s worker for banks %s on %s", a.agent, ", ".join(banks), cfg["tool_server_url"])
    while True:
        busy = False
        for b, handoff in banks.items():
            try:
                if a.agent == "verifier":
                    for d in vers[b].resume_ready():
                        log.info("bank %s: gate decided for %s: %s (%s)", b, d["investigation_id"], d["status"], d["how"])
                        busy = True
                        if a.once:
                            return
                    c = vers[b].answer_chat()
                    if c:
                        log.info("bank %s: answered %s (accepted %s)", b, c["message_id"], c.get("accepted"))
                        busy = True
                job = handoff.claim(a.agent, resume=b in resume)
                resume.discard(b)
            except Exception as e:                          # the tool server is down: wait and try again
                log.warning("bank %s: %s: %s; retrying", b, type(e).__name__, str(e)[:200])
                continue
            if job:
                busy = True
                handle(cfg, a.agent, handoff, vers.get(b), job)
                if a.once and a.agent != "verifier":
                    return
        if not busy:
            time.sleep(a.poll)


if __name__ == "__main__":
    main()
