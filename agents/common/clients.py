"""How an agent reaches the tool server: its own MCP path for tools, and the handoff API for everything else.

Agents never touch FalkorDB, the mock bank or another agent directly (the sandboxes enforce it). Each tool
result is a dict: the tool's data or {"error": {"code", "message"}}, which the agent records and handles.

Phase 3 step 3: a tool server that is briefly unreachable no longer stops a worker. Requests are retried with
backoff (BACKOFF): reads on any transport error or a 502/503/504, writes only when the request never reached the
server (a refused or timed-out connection), so a write is never sent twice by the client.
"""
from __future__ import annotations

import asyncio
import json
import logging
import threading
import time

import httpx2
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client

log = logging.getLogger("tracehold.clients")
BACKOFF = (0.5, 1, 2, 4, 8)                       # seconds between tries; then the error goes to the caller
RETRY_STATUS = {502, 503, 504}


def with_retries(send, safe, what, backoff=BACKOFF, sleep=time.sleep):
    """send() -> response. `safe` (a read): retry any transport error and 502/503/504; otherwise only errors raised
    before the request reached the server."""
    for i, wait in enumerate(list(backoff) + [None]):
        try:
            r = send()
            if safe and r.status_code in RETRY_STATUS and wait is not None:
                log.warning("%s answered %s; retrying in %ss", what, r.status_code, wait)
                sleep(wait)
                continue
            return r
        except (httpx2.ConnectError, httpx2.ConnectTimeout, httpx2.PoolTimeout) as e:
            if wait is None:
                raise
            log.warning("%s: %s; retrying in %ss", what, type(e).__name__, wait)
        except httpx2.TransportError as e:
            if not safe or wait is None:
                raise
            log.warning("%s: %s; retrying in %ss", what, type(e).__name__, wait)
        sleep(wait)


class _Loop:
    """One background event loop for MCP calls, so sync graph nodes can call async clients from any thread."""
    _loop = None
    _lock = threading.Lock()

    @classmethod
    def run(cls, coro, timeout):
        with cls._lock:
            if cls._loop is None:
                cls._loop = asyncio.new_event_loop()
                threading.Thread(target=cls._loop.run_forever, daemon=True, name="mcp-loop").start()
        return asyncio.run_coroutine_threadsafe(coro, cls._loop).result(timeout)


class ToolClient:
    """The agent's MCP path (/mcp/<agent>/). bank_id and investigation_id are filled in by code, never a model."""

    def __init__(self, base_url, agent, bank_id, investigation_id=None, timeout_s=120, backoff=(1, 2, 4)):
        self.url = f"{base_url}/mcp/{agent}/"
        self.agent, self.bank_id, self.investigation_id, self.timeout_s = agent, str(bank_id), investigation_id, timeout_s
        self.backoff = backoff                                 # read tools and idempotent act tools: safe to repeat
        self.calls = []                                        # (tool, arguments, result) for the run's record

    async def _session_call(self, fn):
        async with httpx2.AsyncClient(timeout=httpx2.Timeout(30, read=self.timeout_s)) as http:
            async with streamable_http_client(self.url, http_client=http, terminate_on_close=False) as streams:
                async with ClientSession(streams[0], streams[1]) as s:
                    await s.initialize()
                    return await fn(s)

    def list_tools(self):
        async def go(s):
            return [{"name": t.name, "description": t.description, "input_schema": t.input_schema}
                    for t in (await s.list_tools()).tools]
        return _Loop.run(self._session_call(go), self.timeout_s + 30)

    def call(self, tool, **args):
        full = {"bank_id": self.bank_id, "investigation_id": self.investigation_id, **args}

        async def go(s):
            res = await s.call_tool(tool, full)
            text = "".join(getattr(c, "text", "") for c in res.content)
            if getattr(res, "is_error", False):
                return {"error": {"code": "tool_error", "message": text[:500]}}
            try:
                return json.loads(text)
            except ValueError:
                return {"error": {"code": "bad_output", "message": text[:500]}}
        out = None
        for wait in list(self.backoff) + [None]:
            try:
                out = _Loop.run(self._session_call(go), self.timeout_s + 30)
                break
            except Exception as e:                             # transport failure: retried, then recorded, not hidden
                out = {"error": {"code": "unreachable", "message": f"{type(e).__name__}: {str(e)[:300]}"}}
                if wait is None:
                    break
                log.warning("%s on %s: %s; retrying in %ss", tool, self.url, type(e).__name__, wait)
                time.sleep(wait)
        self.calls.append((tool, args, out))
        return out


class HandoffClient:
    """The handoff API on the tool server (handoff/api.py)."""

    def __init__(self, base_url, bank_id, timeout_s=120, backoff=BACKOFF, transport=None):
        self.http = httpx2.Client(base_url=base_url, timeout=timeout_s, **({"transport": transport} if transport else {}))
        self.bank_id = str(bank_id)
        self.backoff = backoff

    def _post(self, path, body, ok=()):
        r = with_retries(lambda: self.http.post(path, json=body), False, f"POST {path}", self.backoff)
        data = r.json() if r.content else {}
        if r.status_code >= 400 and r.status_code not in ok:
            raise HandoffError(r.status_code, data.get("error", {}).get("code", "error"),
                               data.get("error", {}).get("message", r.text[:300]))
        return data

    def _get(self, path, params=None):
        r = with_retries(lambda: self.http.get(path, params=params), True, f"GET {path}", self.backoff)
        data = r.json() if r.content else {}
        if r.status_code >= 400:
            raise HandoffError(r.status_code, data.get("error", {}).get("code", "error"), r.text[:300])
        return data

    def start(self, complaint_id):
        return self._post("/handoff/investigations", {"bank_id": self.bank_id, "complaint_id": complaint_id})

    def claim(self, agent, resume=False, investigation_id=None):
        body = {"bank_id": self.bank_id, "agent": agent, "resume": bool(resume)}
        if investigation_id:
            body["investigation_id"] = investigation_id
        return self._post("/handoff/claim", body)

    def step(self, investigation_id, step):
        return self._post("/handoff/step", {"bank_id": self.bank_id, "investigation_id": investigation_id, "step": step})

    def chat_claim(self):
        return self._post("/handoff/chat/claim", {"bank_id": self.bank_id})

    def chat_answer(self, message_id, answer, cited_ids, final=False):
        return self._post("/handoff/chat/answer", {"bank_id": self.bank_id, "message_id": message_id, "answer": answer,
                                                   "cited_ids": cited_ids, "final": final}, ok=(422,))

    # the UI's calls (the service layer: the UI never talks to FalkorDB)
    def chat_ask(self, inv, text):
        return self._post("/handoff/chat/ask", {"bank_id": self.bank_id, "investigation_id": inv, "text": text})

    def chat(self, inv):
        return self._get(f"/handoff/chat/{self.bank_id}/{inv}")

    def undo(self, inv, action_id, reason):
        return self._post("/handoff/actions/undo", {"bank_id": self.bank_id, "investigation_id": inv,
                                                    "action_id": action_id, "reason": reason})

    def reviews(self, status="open"):
        return self._get("/handoff/reviews", {"bank_id": self.bank_id, "status": status})

    def resolve_review(self, action_id, outcome, note):
        return self._post("/handoff/reviews/resolve", {"bank_id": self.bank_id, "action_id": action_id,
                                                       "outcome": outcome, "note": note})

    def memory(self):
        return self._get("/handoff/memory", {"bank_id": self.bank_id})

    def trail(self, inv):
        return self._get(f"/handoff/trail/{self.bank_id}/{inv}")

    def close(self, inv, reason):
        return self._post("/handoff/close", {"bank_id": self.bank_id, "investigation_id": inv, "reason": reason})

    def banks(self, ids):
        return self._get("/handoff/banks", {"ids": ",".join(ids)})

    def submit(self, kind, payload):
        return self._post("/handoff/submit", {"bank_id": self.bank_id, "kind": kind, "payload": payload})

    def read(self, inv, kind):
        return self._get(f"/handoff/read/{self.bank_id}/{inv}/{kind}")

    def detail(self, inv):
        return self._get(f"/handoff/investigations/{self.bank_id}/{inv}")

    def investigations(self):
        return self._get("/handoff/investigations", {"bank_id": self.bank_id})

    def complaints(self):
        return self._get("/handoff/complaints", {"bank_id": self.bank_id})

    def gate(self, inv):
        return self._get(f"/handoff/gate/{self.bank_id}/{inv}")

    def gate_decide(self, inv, decisions, gate_mode):
        return self._post("/handoff/gate/decide", {"bank_id": self.bank_id, "investigation_id": inv,
                                                   "decisions": decisions, "gate_mode": gate_mode})

    def gate_record(self, inv, decision_id, approved, decided_by, gate_mode, reason):
        return self._post("/handoff/gate/record", {"bank_id": self.bank_id, "investigation_id": inv,
                                                   "decision_id": decision_id, "approved": approved,
                                                   "decided_by": decided_by, "gate_mode": gate_mode, "reason": reason})

    def gate_execute(self, inv, decision_id):
        body = {"bank_id": self.bank_id, "investigation_id": inv, "decision_id": decision_id}
        r = with_retries(lambda: self.http.post("/handoff/gate/execute", json=body), False, "POST /handoff/gate/execute",
                         self.backoff)
        return r.json()

    def gate_finish(self, inv):
        return self._post("/handoff/gate/finish", {"bank_id": self.bank_id, "investigation_id": inv})

    def fail(self, inv, agent, reason):
        return self._post("/handoff/fail", {"bank_id": self.bank_id, "investigation_id": inv, "agent": agent,
                                            "reason": reason[:500]})

    def spend(self, inv=None):
        return self._get("/handoff/spend", {"investigation_id": inv} if inv else None)

    def record_spend(self, entry):
        return self._post("/handoff/spend", entry)


class HandoffError(RuntimeError):
    def __init__(self, status, code, message):
        super().__init__(f"{status} {code}: {message}")
        self.status, self.code, self.message = status, code, message
