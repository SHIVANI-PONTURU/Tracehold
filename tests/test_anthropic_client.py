"""Phase 2 step 6 for Claude, without spending money: the Anthropic Messages API client against a local stub of
POST /v1/messages. Structured answers, the lookup loop (thinking blocks passed back unchanged), no forced tool
choice, retries on 429, refusals, pricing, and agents.check_model. The real check is agents/check_model.py on
your own ANTHROPIC_API_KEY."""
import json
import socket
import threading
import time
from typing import Literal

import pytest
from pydantic import BaseModel
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route

from agents.common import model as M
from agents.common.config import load

SEEN = []
STATE = {"fail_429": 0}


class Verdict(BaseModel):
    """Answer."""
    decision: Literal["follow", "absorbed_genuine"]
    score: int


USAGE = {"input_tokens": 900, "cache_read_input_tokens": 100, "cache_creation_input_tokens": 0,
         "output_tokens": 200, "output_tokens_details": {"thinking_tokens": 150}}


def _reply(body):
    tools = [t["name"] for t in body.get("tools", [])]
    last = body["messages"][-1]
    has_result = isinstance(last["content"], list) and any(b.get("type") == "tool_result" for b in last["content"])
    choice = (body.get("tool_choice") or {}).get("type")
    fmt = (body.get("output_config") or {}).get("format")
    first_user = body["messages"][0]["content"]
    if "refuse me" in str(first_user):
        return {"type": "message", "role": "assistant", "content": [], "stop_reason": "refusal", "usage": USAGE}
    if "lookup" in tools and not has_result and choice == "auto":
        return {"type": "message", "role": "assistant", "stop_reason": "tool_use", "usage": USAGE,
                "content": [{"type": "thinking", "thinking": "check the account", "signature": "sig-1"},
                            {"type": "tool_use", "id": "toolu_1", "name": "lookup",
                             "input": {"account_id": "20-800283B60"}}]}
    if "RecordBranch" in tools and not fmt:
        return {"type": "message", "role": "assistant", "stop_reason": "tool_use", "usage": USAGE,
                "content": [{"type": "tool_use", "id": "toolu_2", "name": "RecordBranch",
                             "input": {"account_id": "20-800283B60", "decision": "follow"}}]}
    props = fmt["schema"]["properties"]
    answer = {"decision": "Follow", "score": 3} if "score" in props else \
        {"account_id": "20-800283B60", "decision": "follow"}
    return {"type": "message", "role": "assistant", "stop_reason": "end_turn", "usage": USAGE,
            "content": [{"type": "text", "text": json.dumps(answer)}]}


async def messages(request):
    raw = await request.body()
    body = json.loads(raw)
    SEEN.append({"body": body, "key": request.headers.get("x-api-key"), "raw": raw, "path": request.url.path,
                 "version": request.headers.get("anthropic-version"), "headers": dict(request.headers)})
    if STATE["fail_429"] > 0:
        STATE["fail_429"] -= 1
        return JSONResponse({"type": "error", "error": {"type": "rate_limit_error", "message": "slow down"}},
                            status_code=429, headers={"retry-after": "0"})
    return JSONResponse(_reply(body))


@pytest.fixture(scope="module")
def stub():
    import uvicorn
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    app = Starlette(routes=[Route("/v1/messages", messages, methods=["POST"]),
                            Route("/model/{model}/invoke", messages, methods=["POST"])])
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    threading.Thread(target=server.run, daemon=True).start()
    time.sleep(1)
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True


@pytest.fixture
def cfg(stub, monkeypatch):
    monkeypatch.setenv("TRACEHOLD_MODEL_ID", "claude-sonnet-5-5")       # the Anthropic API route
    monkeypatch.setenv("ANTHROPIC_BASE_URL", stub)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    return load()


class Ledger:
    def __init__(self, total=0.0):
        self.rows, self.total = [], total

    def spend(self, inv=None):
        run = sum(r["usd"] for r in self.rows if r["investigation_id"] == inv)
        return {"total": {"usd": self.total + sum(r["usd"] for r in self.rows)}, "investigation": {"usd": run}}

    def record_spend(self, entry):
        self.rows.append(entry)


def test_a_structured_answer_is_parsed_priced_and_never_forces_a_tool(cfg):
    ledger = Ledger()
    model = M.make_model(cfg, M.SpendGuard(cfg, ledger, "INV-0011-test", "tracer"))
    assert isinstance(model, M.ClaudeModel)
    v = model.decide([("system", "decide"), ("human", "go")], Verdict)
    assert v == Verdict(decision="follow", score=3)                  # "Follow" mapped back to the enum's case
    req = SEEN[-1]
    assert req["key"] == "test-key" and req["version"] == "2023-06-01"
    body = req["body"]
    assert body["model"] == "claude-sonnet-5-5" and body["system"] == "decide"
    assert body["messages"] == [{"role": "user", "content": "go"}]
    assert body["thinking"] == {"type": "adaptive"} and body["output_config"]["effort"] == "medium"
    schema = body["output_config"]["format"]["schema"]
    assert body["output_config"]["format"]["type"] == "json_schema" and schema["additionalProperties"] is False
    assert "tool_choice" not in body and "temperature" not in body
    row = ledger.rows[-1]
    p = cfg["model"]["price_usd_per_million"]                        # 900 uncached, 100 cache reads, 200 output
    assert row["usd"] == pytest.approx((900 * p["input"] + 100 * p["cached_input"] + 200 * p["output"]) / 1e6)
    assert (row["input_tokens"], row["cached_input_tokens"], row["reasoning_tokens"]) == (1000, 100, 150)


def test_lookups_run_then_the_answer_comes_back(cfg):
    from langchain_core.tools import StructuredTool

    class One(BaseModel):
        account_id: str
    looked = []
    lookup = StructuredTool.from_function(func=lambda account_id: looked.append(account_id) or '{"ok": true}',
                                          name="lookup", description="look an account up", args_schema=One)
    ledger = Ledger()
    n = len(SEEN)
    v, used = M.make_model(cfg, M.SpendGuard(cfg, ledger, "INV-0011-t2", "adjudicator")).decide_with_tools(
        [("system", "decide"), ("human", "go")], [lookup], Verdict, max_calls=3)
    assert v.decision == "follow" and used == 1 and looked == ["20-800283B60"] and len(ledger.rows) == 2
    first, second = SEEN[n]["body"], SEEN[n + 1]["body"]
    assert first["tool_choice"] == {"type": "auto"} and first["tools"][0]["name"] == "lookup"
    assert first["tools"][0]["input_schema"]["additionalProperties"] is False
    assistant, results = second["messages"][-2], second["messages"][-1]
    assert assistant["role"] == "assistant" and assistant["content"][0] == {
        "type": "thinking", "thinking": "check the account", "signature": "sig-1"}     # passed back unchanged
    assert results["content"][0]["type"] == "tool_result" and results["content"][0]["tool_use_id"] == "toolu_1"


def test_no_lookups_left_means_tool_choice_none(cfg):
    from langchain_core.tools import StructuredTool

    class One(BaseModel):
        account_id: str
    lookup = StructuredTool.from_function(func=lambda account_id: "{}", name="lookup", description="d", args_schema=One)
    v, used = M.make_model(cfg, M.SpendGuard(cfg, Ledger(), "INV-0011-t3", "verifier")).decide_with_tools(
        [("human", "go")], [lookup], Verdict, max_calls=0)
    assert used == 0 and v.score == 3 and SEEN[-1]["body"]["tool_choice"] == {"type": "none"}


def test_rate_limits_are_retried_and_refusals_are_errors(cfg):
    STATE["fail_429"] = 2
    n = len(SEEN)
    v = M.make_model(cfg, M.SpendGuard(cfg, Ledger(), "INV-0011-t4", "tracer")).decide([("human", "go")], Verdict)
    assert v.score == 3 and len(SEEN) - n == 3
    with pytest.raises(ValueError, match="refused"):
        M.make_model(cfg, M.SpendGuard(cfg, Ledger(), "INV-0011-t5", "tracer")).decide([("human", "refuse me")], Verdict)


def test_the_budget_cap_stops_claude_too(cfg):
    with pytest.raises(M.BudgetExceeded):
        M.make_model(cfg, M.SpendGuard(cfg, Ledger(total=cfg["budget"]["total_usd"]), "INV-0011-t6", "tracer")).decide(
            [("human", "go")], Verdict)


def test_check_model_passes_for_claude(cfg, tmp_path, monkeypatch):
    import yaml

    from agents import check_model
    from agents.common.config import PATH
    from handoff import spend
    monkeypatch.setattr(spend, "DB_PATH", str(tmp_path / "spend.sqlite"))
    with open(PATH, encoding="utf-8") as f:
        cfg["_model_cfg"] = yaml.safe_load(f)["model"]
    ok, line = check_model.check_one(cfg, "claude-sonnet-5-5")
    assert ok, line
    assert "tool call {'account_id': '20-800283B60', 'decision': 'follow'}" in line
    assert spend.totals()["total"]["calls"] == 2
    monkeypatch.delenv("BEDROCK_API_KEY", raising=False)
    ok, line = check_model.check_one(cfg, "openai.gpt-oss-120b")
    assert ok is None and "SKIP" in line


def test_check_model_explains_anthropic_errors():
    from agents import check_model
    from agents.common.anthropic_api import APIError
    assert "credit balance" in check_model.explain(APIError(400, "invalid_request_error",
                                                            "Your credit balance is too low to access the Anthropic API."))
    assert "key was refused" in check_model.explain(APIError(401, "authentication_error", "invalid x-api-key"))
    assert "overloaded" in check_model.explain(APIError(529, "overloaded_error", "Overloaded"))
    assert "use-case form" in check_model.explain(APIError(404, "ResourceNotFoundException",
        "Model use case details have not been submitted for this account. Fill out the Anthropic use case "
        "details form before using the model."))
    assert "pick a workspace" in check_model.explain(APIError(400, "invalid_request_error",
        "anthropic-workspace-id is required when authenticating with an identity-linked API key"))
    line = check_model.explain(APIError(403, "AccessDeniedException",
        "Model access is denied due to INVALID_PAYMENT_INSTRUMENT: A valid payment instrument must be provided."))
    assert "Marketplace subscription" in line and "INVALID_PAYMENT_INSTRUMENT" in line
    assert "not allowed" in check_model.explain(APIError(403, "AccessDeniedException", "not allowed"))


def _reverify(req, service, region, secret):
    """Re-sign the request the stub received, from its own headers, and compare signatures."""
    import datetime

    from agents.common import sigv4
    h = req["headers"]
    auth = h["authorization"]
    names = auth.split("SignedHeaders=")[1].split(",")[0].split(";")
    when = datetime.datetime.strptime(h["x-amz-date"], "%Y%m%dT%H%M%SZ").replace(tzinfo=datetime.timezone.utc)
    url = f"http://{h['host']}{req['path']}"
    again = sigv4.sign("POST", url, {n: h[n] for n in names if n not in ("host", "x-amz-date")}, req["raw"],
                       access_key="AKIDEXAMPLE", secret_key=secret, region=region, service=service, now=when)
    return again["Authorization"] == auth


def test_claude_platform_on_aws_with_an_api_key_or_access_keys(stub, monkeypatch):
    monkeypatch.setenv("TRACEHOLD_MODEL_ID", "aws/claude-opus-5-5")
    monkeypatch.setenv("ANTHROPIC_AWS_BASE_URL", stub)
    monkeypatch.setenv("ANTHROPIC_AWS_WORKSPACE_ID", "wrkspc_test")
    monkeypatch.setenv("ANTHROPIC_AWS_API_KEY", "aws-console-key")
    cfg = load()
    assert cfg["model"]["provider"] == "anthropic_aws"
    v = M.make_model(cfg, M.SpendGuard(cfg, Ledger(), "INV-0011-a1", "tracer")).decide([("human", "go")], Verdict)
    req = SEEN[-1]
    assert v.score == 3 and req["path"] == "/v1/messages" and req["key"] == "aws-console-key"
    assert req["headers"]["anthropic-workspace-id"] == "wrkspc_test" and req["body"]["model"] == "claude-opus-5-5"
    # no API key: AWS access keys sign the request (SigV4, service aws-external-anthropic)
    monkeypatch.delenv("ANTHROPIC_AWS_API_KEY")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIDEXAMPLE")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "secret-example")
    cfg = load()
    M.make_model(cfg, M.SpendGuard(cfg, Ledger(), "INV-0011-a2", "tracer")).decide([("human", "go")], Verdict)
    req = SEEN[-1]
    assert req["key"] is None
    assert req["headers"]["authorization"].startswith(
        "AWS4-HMAC-SHA256 Credential=AKIDEXAMPLE/") and "/us-east-2/aws-external-anthropic/aws4_request" in \
        req["headers"]["authorization"]
    assert _reverify(req, "aws-external-anthropic", "us-east-2", "secret-example")


def test_claude_on_bedrock_uses_invoke_with_the_messages_body(stub, monkeypatch):
    monkeypatch.setenv("TRACEHOLD_MODEL_ID", "bedrock/claude-opus-4-6")
    monkeypatch.setenv("BEDROCK_RUNTIME_URL", stub)
    monkeypatch.setenv("BEDROCK_API_KEY", "bedrock-key")
    cfg = load()
    v = M.make_model(cfg, M.SpendGuard(cfg, Ledger(), "INV-0011-b1", "tracer")).decide([("human", "go")], Verdict)
    req = SEEN[-1]
    assert v.score == 3 and req["path"] == "/model/us.anthropic.claude-opus-4-6-v1/invoke"
    assert req["headers"]["authorization"] == "Bearer bedrock-key" and "x-api-key" not in req["headers"]
    assert req["body"]["anthropic_version"] == "bedrock-2023-05-31" and "model" not in req["body"]
    assert req["body"]["output_config"]["format"]["type"] == "json_schema"
    monkeypatch.delenv("BEDROCK_API_KEY")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIDEXAMPLE")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "secret-example")
    cfg = load()
    M.make_model(cfg, M.SpendGuard(cfg, Ledger(), "INV-0011-b2", "tracer")).decide([("human", "go")], Verdict)
    req = SEEN[-1]
    assert "/us-east-2/bedrock/aws4_request" in req["headers"]["authorization"]
    assert _reverify(req, "bedrock", "us-east-2", "secret-example")


def test_check_model_says_what_is_missing_for_the_aws_route(monkeypatch):
    import yaml

    from agents import check_model
    from agents.common.config import PATH
    for v in ("ANTHROPIC_AWS_API_KEY", "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "ANTHROPIC_AWS_WORKSPACE_ID",
              "TRACEHOLD_MODEL_ID"):
        monkeypatch.delenv(v, raising=False)
    cfg = load()
    with open(PATH, encoding="utf-8") as f:
        cfg["_model_cfg"] = yaml.safe_load(f)["model"]
    ok, line = check_model.check_one(cfg, "aws/claude-opus-5-5")
    assert ok is None and "ANTHROPIC_AWS_API_KEY (or AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY)" in line
    monkeypatch.setenv("ANTHROPIC_AWS_API_KEY", "k")
    ok, line = check_model.check_one(cfg, "aws/claude-opus-5-5")
    assert ok is None and "ANTHROPIC_AWS_WORKSPACE_ID" in line
    from agents.common.anthropic_api import APIError
    assert "enable-outbound-web-identity-federation" in check_model.explain(
        APIError(403, "permission_error", "Outbound web identity federation is disabled for your account"))


def test_an_empty_text_block_before_a_lookup_is_not_sent_back():
    """Phase 4 sample run (CMP-0271): Claude wrote an empty text block before a tool call, the client sent the reply
    back unchanged, and Bedrock refused the next request ("text content blocks must be non-empty")."""
    from langchain_core.tools import StructuredTool
    from pydantic import BaseModel as BM
    from agents.common import model as Mo

    class One(BM):
        account_id: str

    class Answer(BM):
        decision: str

    sent = []

    class FakeApi:
        def create(self, system, msgs, **kw):
            sent.append([dict(m) for m in msgs])
            for m in msgs:                                    # what Bedrock checks
                for b in (m["content"] if isinstance(m["content"], list) else []):
                    assert b.get("type") != "text" or b.get("text"), "empty text block sent"
            if len(sent) == 1:
                return {"stop_reason": "tool_use", "usage": {"input_tokens": 10, "output_tokens": 5},
                        "content": [{"type": "thinking", "thinking": "look it up", "signature": "s"},
                                    {"type": "text", "text": ""},
                                    {"type": "tool_use", "id": "t1", "name": "lookup", "input": {"account_id": "x"}}]}
            return {"stop_reason": "end_turn", "usage": {"input_tokens": 10, "output_tokens": 5},
                    "content": [{"type": "text", "text": '{"decision": "follow"}'}]}

    class Guard:
        def check(self):
            pass

        def record(self, usage, model_id):
            return 0.0
    lookup = StructuredTool.from_function(func=lambda account_id: '{"ok": true}', name="lookup",
                                          description="look an account up", args_schema=One)
    m = Mo.ClaudeModel(load(), Guard(), api=FakeApi())
    answer, used = m.decide_with_tools([("system", "s"), ("human", "go")], [lookup], Answer, max_calls=2)
    assert answer.decision == "follow" and used == 1
    echoed = sent[1][1]["content"]
    assert [b["type"] for b in echoed] == ["thinking", "tool_use"]        # thinking kept, the empty text dropped
