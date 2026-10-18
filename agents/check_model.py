"""Phase 2 step 6 check: the agents' model answers a tool-calling request with a valid tool call.

  python -m agents.check_model                      the model the agents use (agents/config.yaml, model.id)
  python -m agents.check_model --model claude-opus-5-5
  python -m agents.check_model --all                every model in agents/config.yaml that has a key in .env

For Claude (Claude Platform on AWS, Amazon Bedrock or the Anthropic API) it makes two small calls: one tool call (tool_choice "auto"), and one structured
answer, which is how the agents get their decisions. For the OpenAI models on Bedrock, the agents' own decision
call (agents.common.model.Model.decide), with one reminder if the first reply is not a tool call. Each costs well
under one US cent (refused calls cost nothing), and is recorded in the spend ledger (work/spend.sqlite) that
the budget caps read. Exits 0 when the model the agents use passes.
"""
from __future__ import annotations

import argparse
import os
import sys
import time

from pydantic import BaseModel, Field


class RecordBranch(BaseModel):
    """Record your decision about one branch of a money trail."""
    account_id: str = Field(description="the account ID exactly as given")
    decision: str = Field(description="follow or absorbed_genuine")


SYSTEM = "You decide whether to keep following stolen money."
QUESTION = ("Account 20-800283B60 received Rs 8,67,196 of stolen money and passed 95% of it on within hours to "
            "accounts it had never paid before. Follow it or not?")


def explain(err: Exception) -> str:
    """A plain reason for a refused call."""
    text = str(err)
    status = getattr(err, "status_code", None)
    low = text.lower()
    if "not available for this account" in low:
        return ("AWS has not enabled this model for your account (an AWS-side release, not a key problem). "
                "Ask AWS Support or Sales to enable it, or use another model.")
    if "use case details" in low:
        return ("this AWS account has not used Anthropic models before: AWS asks for a one-time use-case form. "
                "Bedrock console (US East (Ohio)) > Model catalog > Claude Opus 4.6 > submit the form. "
                "Already submitted? AWS takes up to about 15 minutes to accept it: wait, then run again")
    if "anthropic-workspace-id is required" in low:
        return ("this Anthropic key is not tied to one workspace: create a new key in the Claude Console and "
                "pick a workspace for it")
    if "outbound web identity federation is disabled" in low:
        return ("Claude Platform on AWS needs a one-time account setting: run "
                "`aws iam enable-outbound-web-identity-federation` as the account owner")
    if "workspace" in low and status in (400, 403, 404):
        return f"the Claude Platform workspace was not accepted: check ANTHROPIC_AWS_WORKSPACE_ID and its region ({text[:150]})"
    if status == 403 and ("aws-external-anthropic" in low or "createinference" in low):
        return "these AWS credentials may not call Claude Platform on AWS: allow aws-external-anthropic:CreateInference"
    if "credit balance" in low:
        return "your Anthropic credit balance is too low: add credits in the Claude Console (Billing), then run again"
    if status == 401 or "AuthenticationError" in type(err).__name__ or "authentication_error" in low:
        return "the key was refused: check the key in .env (no spaces or quotes)"
    if status == 403 and ("marketplace" in low or "payment" in low or "agreement" in low or "subscription" in low):
        return ("AWS could not set up the Marketplace subscription this model needs (usually the account's plan "
                f"or payment method). AWS says: {text[:400]}")
    if status == 403:
        return f"permission denied for this key (check the key's workspace or IAM permissions). The service says: {text[:400]}"
    if status == 404:
        return "not found: wrong base URL or model ID for this endpoint"
    if status == 429:
        return "rate limited: wait a minute and run again"
    if status == 529:
        return "the API is overloaded: wait a minute and run again"
    if "Connection" in type(err).__name__ or "Timeout" in type(err).__name__ or "ConnectError" in type(err).__name__:
        return f"could not reach the endpoint (network, proxy or firewall): {text[:200]}"
    return f"{type(err).__name__}: {text[:300]}"


def _record(model_id, usage, usd):
    from handoff import spend
    spend.record({"agent": "check_model", "investigation_id": "check", "model": model_id,
                  "input_tokens": usage.get("input_tokens", 0),
                  "cached_input_tokens": (usage.get("input_token_details") or {}).get("cache_read", 0) or 0,
                  "output_tokens": usage.get("output_tokens", 0),
                  "reasoning_tokens": (usage.get("output_token_details") or {}).get("reasoning", 0) or 0,
                  "usd": round(usd, 6)})


def _check_claude(m):
    """A tool call (auto) and a structured answer from Claude."""
    from agents.common import anthropic_api as A
    from agents.common import model as M
    api = A.Claude(m, fallback=False)               # check exactly this model, never its fallback
    t0, usd, tokens = time.time(), 0.0, [0, 0]
    tool = {"name": "RecordBranch", "description": RecordBranch.__doc__,
            "input_schema": A.strict_schema(RecordBranch.model_json_schema())}
    r1 = api.create(SYSTEM + " Answer only by calling the RecordBranch tool.", [{"role": "user", "content": QUESTION}],
                    tools=[tool])
    r2 = api.create(SYSTEM, [{"role": "user", "content": QUESTION}], schema=RecordBranch.model_json_schema())
    for r in (r1, r2):
        u = A.usage(r)
        cost = M.price(u, m["price_usd_per_million"])
        _record(m["id"], u, cost)
        usd += cost
        tokens[0] += u["input_tokens"]
        tokens[1] += u["output_tokens"]
    calls = [b for b in r1.get("content", []) if b.get("type") == "tool_use" and b.get("name") == "RecordBranch"]
    problems = []
    try:
        RecordBranch.model_validate(calls[0]["input"])
    except Exception:
        problems.append("no valid tool call")
    try:
        answer = RecordBranch.model_validate(A.parse_json(r2))
    except Exception as e:
        answer = None
        problems.append(f"no valid structured answer ({e})")
    detail = (f"{time.time() - t0:.1f} s for 2 calls, {tokens[0]} in / {tokens[1]} out, ${usd:.5f}; "
              f"tool call {calls[0]['input'] if calls else None}; answer {answer.model_dump() if answer else None}")
    return not problems, detail if not problems else "; ".join(problems) + ": " + detail


class _Ledger:
    """The spend guard's interface for the check: no caps, every call recorded in the spend ledger."""
    def __init__(self, m):
        self.m, self.usd, self.tokens = m, 0.0, [0, 0]

    def check(self):
        pass

    def record(self, usage, model_id, prices=None):
        from agents.common import model as M
        usd = M.price(usage, prices or self.m["price_usd_per_million"])
        _record(model_id, usage, usd)
        self.usd += usd
        self.tokens[0] += int(usage.get("input_tokens", 0))
        self.tokens[1] += int(usage.get("output_tokens", 0))
        return usd


def _check_openai(c, m):
    """The same decision call the agents make (agents.common.model.Model.decide), with the same prompt shape."""
    from agents.common import model as M
    t0, guard = time.time(), _Ledger(m)
    try:
        answer = M.Model(c, guard).decide([("system", SYSTEM), ("human", QUESTION)], RecordBranch, name="check")
        problem = None
    except ValueError as e:
        answer, problem = None, str(e)
    detail = (f"{time.time() - t0:.1f} s, {guard.tokens[0]} in / {guard.tokens[1]} out, ${guard.usd:.5f}; "
              f"tool call {answer.model_dump() if answer else None}")
    return answer is not None, detail if answer else f"no valid tool call ({problem[:300]}): " + detail


def check_one(cfg, model_id):
    """Returns (ok, line); ok is None when the model has no key in .env (skipped)."""
    from agents.common.config import resolve_model
    c = dict(cfg)
    c["model"] = resolve_model(cfg["_model_cfg"], model_id)
    m = c["model"]
    from agents.common import anthropic_api as A
    claude = m["provider"] in A.PROVIDERS
    gap = A.missing(m) if claude else (None if os.environ.get(m["api_key_env"]) else f"{m['api_key_env']} is empty in .env")
    if gap:
        return None, f"SKIP  {model_id:<26} {gap}"
    try:
        ok, detail = _check_claude(m) if claude else _check_openai(c, m)
    except Exception as e:                                   # refused or unreachable: costs nothing
        return False, f"FAIL  {model_id:<26} {explain(e)}"
    return ok, f"{'PASS' if ok else 'FAIL'}  {model_id:<26} {detail}"


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--model", help="test this model ID (one of model.models in agents/config.yaml)")
    ap.add_argument("--all", action="store_true", help="test every model in agents/config.yaml that has a key")
    a = ap.parse_args()
    from dotenv import load_dotenv
    load_dotenv()
    import yaml

    from agents.common.config import PATH, load
    cfg = load()
    with open(PATH, encoding="utf-8") as f:
        cfg["_model_cfg"] = yaml.safe_load(f)["model"]
    used = cfg["model"]["id"]
    ids = list(cfg["_model_cfg"]["models"]) if a.all else [a.model or used]
    print(f"The agents use {used} (agents/config.yaml model.id or TRACEHOLD_MODEL_ID).")
    results = {}
    for mid in ids:
        ok, line = check_one(cfg, mid)
        results[mid] = ok
        print(line)
    target = a.model or used
    if results.get(target) is None:
        print(f"FAIL: {target} has no credentials in .env yet (see the SKIP line above)")
        sys.exit(2)
    print("PASS: the model answered correctly" if results[target] else
          f"FAIL: {target} did not answer; if another model passed above, set TRACEHOLD_MODEL_ID=<that model> in .env")
    sys.exit(0 if results[target] else 1)


if __name__ == "__main__":
    main()
