"""Phase 2 step 6 without spending money: the OpenAI-compatible model client (the Bedrock fallback) against a
local stub with the same /chat/completions shape as Bedrock Mantle. The Claude client is in test_anthropic_client.py. A structured decision comes back through a tool call, its usage is
priced and recorded, the spend caps refuse further calls, and decide_with_tools runs a lookup then answers.
The real check (one tool call to the configured model on Bedrock) is agents/check_model.py on your own key."""
import json
import socket
import threading
import time

import pytest

pytest.importorskip("langchain_openai")
from pydantic import BaseModel                           # noqa: E402
from starlette.applications import Starlette             # noqa: E402
from starlette.responses import JSONResponse             # noqa: E402
from starlette.routing import Route                      # noqa: E402

from agents.common import model as M                     # noqa: E402
from agents.common.config import load                    # noqa: E402

SEEN = []


class Verdict(BaseModel):
    """Answer."""
    decision: str
    score: int


def _said(request_json):
    return " ".join(str(m.get("content") or "") for m in request_json["messages"])


def _text_reply(request_json, text):
    return {"id": "chatcmpl-t", "object": "chat.completion", "created": int(time.time()), "model": request_json["model"],
            "choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": text}}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120}}


def _reply(request_json):
    said = _said(request_json)
    if "MODE:text-json" in said:                         # gpt-oss writing the answer as text, not a tool call
        return _text_reply(request_json, 'My answer: {"decision": "follow", "score": 3}')
    if "MODE:never" in said:
        return _text_reply(request_json, "no idea")
    if "MODE:nudge" in said and "Answer now by calling" not in said:
        return _text_reply(request_json, "I would follow it.")
    tools = request_json.get("tools", [])
    names = [t["function"]["name"] for t in tools]
    has_tool_result = any(m.get("role") == "tool" for m in request_json["messages"])
    if "lookup" in names and not has_tool_result:
        call = {"name": "lookup", "arguments": json.dumps({"account_id": "20-800283B60"})}
    else:
        call = {"name": "Verdict", "arguments": json.dumps({"decision": "follow", "score": 3})}
    return {"id": "chatcmpl-1", "object": "chat.completion", "created": int(time.time()),
            "model": request_json["model"],
            "choices": [{"index": 0, "finish_reason": "tool_calls",
                         "message": {"role": "assistant", "content": None,
                                     "tool_calls": [{"id": f"call_{len(SEEN)}", "type": "function", "function": call}]}}],
            "usage": {"prompt_tokens": 1000, "completion_tokens": 200, "total_tokens": 1200,
                      "prompt_tokens_details": {"cached_tokens": 400},
                      "completion_tokens_details": {"reasoning_tokens": 150}}}


async def completions(request):
    body = await request.json()
    SEEN.append({"model": body["model"], "auth": request.headers.get("authorization"),
                 "tools": [t["function"]["name"] for t in body.get("tools", [])],
                 "effort": body.get("reasoning_effort")})
    return JSONResponse(_reply(body))


@pytest.fixture(scope="module")
def stub():
    import uvicorn
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    app = Starlette(routes=[Route("/openai/v1/chat/completions", completions, methods=["POST"])])
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    threading.Thread(target=server.run, daemon=True).start()
    time.sleep(1)
    yield f"http://127.0.0.1:{port}/openai/v1"
    server.should_exit = True


class Ledger:
    """Stands in for the tool server's spend ledger."""
    def __init__(self, total=0.0):
        self.rows, self.total = [], total

    def spend(self, inv=None):
        run = sum(r["usd"] for r in self.rows if r["investigation_id"] == inv)
        return {"total": {"usd": self.total + sum(r["usd"] for r in self.rows)}, "investigation": {"usd": run}}

    def record_spend(self, entry):
        self.rows.append(entry)


@pytest.fixture(autouse=True)
def bedrock_model(monkeypatch):
    monkeypatch.setenv("TRACEHOLD_MODEL_ID", "openai.gpt-oss-120b")


def test_a_structured_decision_through_a_tool_call_is_priced(stub, monkeypatch):
    monkeypatch.setenv("BEDROCK_BASE_URL", stub)
    monkeypatch.setenv("BEDROCK_API_KEY", "test-key")
    cfg = load()
    ledger = Ledger()
    m = M.Model(cfg, M.SpendGuard(cfg, ledger, "INV-0011-test", "tracer"))
    v = m.decide([("system", "decide"), ("human", "go")], Verdict)
    assert v == Verdict(decision="follow", score=3)
    assert SEEN[-1]["model"] == cfg["model"]["id"] and SEEN[-1]["auth"] == "Bearer test-key"
    assert SEEN[-1]["tools"] == ["Verdict"] and SEEN[-1]["effort"] == cfg["model"]["reasoning_effort"]
    row = ledger.rows[-1]
    p = cfg["model"]["price_usd_per_million"]           # 600 uncached input, 400 cached input, 200 output
    assert row["usd"] == pytest.approx((600 * p["input"] + 400 * p["cached_input"] + 200 * p["output"]) / 1e6, abs=1e-9)
    assert (row["input_tokens"], row["cached_input_tokens"], row["output_tokens"], row["reasoning_tokens"]) == (1000, 400, 200, 150)


def test_the_spend_caps_refuse_further_calls(stub, monkeypatch):
    monkeypatch.setenv("BEDROCK_BASE_URL", stub)
    cfg = load()
    over_total = Ledger(total=cfg["budget"]["total_usd"])
    with pytest.raises(M.BudgetExceeded, match="raise budget.total_usd"):
        M.Model(cfg, M.SpendGuard(cfg, over_total, "INV-0011-test", "tracer")).decide([("human", "go")], Verdict)
    run = Ledger()
    run.rows.append({"investigation_id": "INV-0011-busy", "usd": cfg["budget"]["per_run_usd"]})
    with pytest.raises(M.BudgetExceeded, match="this investigation"):
        M.Model(cfg, M.SpendGuard(cfg, run, "INV-0011-busy", "tracer")).decide([("human", "go")], Verdict)


def test_decide_with_tools_looks_up_then_answers(stub, monkeypatch):
    from langchain_core.tools import StructuredTool

    class One(BaseModel):
        account_id: str
    looked = []
    lookup = StructuredTool.from_function(func=lambda account_id: looked.append(account_id) or '{"ok": true}',
                                          name="lookup", description="look an account up", args_schema=One)
    monkeypatch.setenv("BEDROCK_BASE_URL", stub)
    cfg = load()
    ledger = Ledger()
    v, used = M.Model(cfg, M.SpendGuard(cfg, ledger, "INV-0011-t2", "adjudicator")).decide_with_tools(
        [("human", "go")], [lookup], Verdict, max_calls=3)
    assert v.decision == "follow" and used == 1 and looked == ["20-800283B60"]
    assert len(ledger.rows) == 2


def test_decide_accepts_an_answer_written_as_text(stub, monkeypatch):
    monkeypatch.setenv("BEDROCK_BASE_URL", stub)
    cfg = load()
    ledger = Ledger()
    v = M.Model(cfg, M.SpendGuard(cfg, ledger, "INV-0011-t3", "tracer")).decide([("human", "go MODE:text-json")], Verdict)
    assert v == Verdict(decision="follow", score=3) and len(ledger.rows) == 1


def test_decide_reminds_once_when_the_reply_is_not_a_tool_call(stub, monkeypatch):
    monkeypatch.setenv("BEDROCK_BASE_URL", stub)
    cfg = load()
    ledger = Ledger()
    v = M.Model(cfg, M.SpendGuard(cfg, ledger, "INV-0011-t4", "tracer")).decide([("human", "go MODE:nudge")], Verdict)
    assert v == Verdict(decision="follow", score=3) and len(ledger.rows) == 2


def test_decide_fails_after_two_tries_with_what_the_model_sent(stub, monkeypatch):
    monkeypatch.setenv("BEDROCK_BASE_URL", stub)
    cfg = load()
    ledger = Ledger()
    with pytest.raises(ValueError, match="2 tries.*no idea"):
        M.Model(cfg, M.SpendGuard(cfg, ledger, "INV-0011-t5", "tracer")).decide([("human", "go MODE:never")], Verdict)
    assert len(ledger.rows) == 2


def test_decide_with_tools_stops_when_the_model_never_answers(stub, monkeypatch):
    from langchain_core.tools import StructuredTool

    class One(BaseModel):
        account_id: str
    lookup = StructuredTool.from_function(func=lambda account_id: "{}", name="lookup", description="look up",
                                          args_schema=One)
    monkeypatch.setenv("BEDROCK_BASE_URL", stub)
    cfg = load()
    ledger = Ledger()
    with pytest.raises(ValueError, match="did not answer with Verdict"):
        M.Model(cfg, M.SpendGuard(cfg, ledger, "INV-0011-t6", "adjudicator")).decide_with_tools(
            [("human", "go MODE:never")], [lookup], Verdict, max_calls=3)
    assert len(ledger.rows) == 3                          # the first reply, then two reminders


def test_the_model_is_chosen_in_config_with_its_own_url_and_prices(monkeypatch):
    monkeypatch.delenv("BEDROCK_BASE_URL", raising=False)
    monkeypatch.delenv("ANTHROPIC_BASE_URL", raising=False)
    monkeypatch.delenv("TRACEHOLD_MODEL_ID", raising=False)
    monkeypatch.delenv("ANTHROPIC_AWS_REGION", raising=False)
    monkeypatch.delenv("ANTHROPIC_AWS_BASE_URL", raising=False)
    monkeypatch.delenv("BEDROCK_RUNTIME_URL", raising=False)
    cfg = load()
    m = cfg["model"]
    assert m["id"] == "bedrock/claude-opus-4-6" and m["provider"] == "bedrock_anthropic"
    assert m["model_id"] == "us.anthropic.claude-opus-4-6-v1" and m["api_key_env"] == "BEDROCK_API_KEY"
    assert m["base_url"] == "https://bedrock-runtime.us-east-2.amazonaws.com" and m["price_usd_per_million"]["output"] == 27.50
    monkeypatch.setenv("TRACEHOLD_MODEL_ID", "bedrock/claude-opus-4-6-global")       # same model, its own quota
    m = load()["model"]
    assert m["model_id"] == "global.anthropic.claude-opus-4-6-v1" and m["provider"] == "bedrock_anthropic"
    assert m["base_url"] == "https://bedrock-runtime.us-east-2.amazonaws.com" and m["price_usd_per_million"]["output"] == 25.00
    monkeypatch.setenv("TRACEHOLD_MODEL_ID", "aws/claude-opus-5-5")
    m = load()["model"]
    assert m["provider"] == "anthropic_aws" and m["model_id"] == "claude-opus-5-5"
    assert m["base_url"] == "https://aws-external-anthropic.us-east-2.api.aws" and m["api_key_env"] == "ANTHROPIC_AWS_API_KEY"
    monkeypatch.setenv("TRACEHOLD_MODEL_ID", "openai.gpt-oss-120b")
    m = load()["model"]
    assert m["provider"] == "openai_compatible" and m["api_key_env"] == "BEDROCK_API_KEY"
    assert m["base_url"] == "https://bedrock-mantle.us-east-2.api.aws/v1"
    monkeypatch.setenv("TRACEHOLD_MODEL_ID", "openai.gpt-5.4")
    m = load()["model"]
    assert m["base_url"].endswith("/openai/v1") and m["price_usd_per_million"]["output"] == 16.50
    assert m["reasoning_effort"] == "low"
    monkeypatch.setenv("TRACEHOLD_MODEL_ID", "openai.gpt-9")
    with pytest.raises(ValueError, match="not in agents/config.yaml"):
        load()


def test_check_model_passes_on_a_valid_tool_call_and_explains_refusals(stub, monkeypatch, tmp_path):
    import yaml

    from agents import check_model
    from agents.common.config import PATH
    from handoff import spend
    monkeypatch.setenv("BEDROCK_BASE_URL", stub)
    monkeypatch.setenv("BEDROCK_API_KEY", "test-key")
    monkeypatch.setattr(spend, "DB_PATH", str(tmp_path / "spend.sqlite"))
    cfg = load()
    with open(PATH, encoding="utf-8") as f:
        cfg["_model_cfg"] = yaml.safe_load(f)["model"]
    # the stub answers every model with its Verdict tool, so RecordBranch is missing: a clear FAIL, not a crash
    ok, line = check_model.check_one(cfg, "openai.gpt-oss-120b")
    assert not ok and "no valid tool call" in line
    assert spend.totals()["total"]["calls"] >= 1

    class Refused(Exception):
        status_code = 401
    assert "not enabled this model" in check_model.explain(Refused("openai.gpt-5.4 is not available for this account"))
    assert "key was refused" in check_model.explain(Refused("bad key"))
