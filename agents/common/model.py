"""Phase 2 step 6: the model client, with the spend guard that caps model spend.

All three agents use one model, chosen in agents/config.yaml (model.id, or TRACEHOLD_MODEL_ID in .env):
- Claude (providers "anthropic_aws", Claude Platform on AWS, the default with Opus 5.5; "bedrock_anthropic",
  Claude on Amazon Bedrock; "anthropic", the Anthropic API): the Messages API, through
  agents/common/anthropic_api.py. Answers come back as structured outputs (JSON constrained to the schema);
  lookups are ordinary tool calls with tool_choice "auto", because Claude 5.5 models reject forced tool choice.
- provider "openai_compatible" (OpenAI models on Amazon Bedrock): ChatOpenAI from langchain-openai, with a
  forced tool call for the answer.
Outside a sandbox the key comes from .env; inside one OpenShell's gateway adds it on the way out (step 12), so
the sandbox never holds it.

Every call is priced from its token usage and recorded in the tool server's spend ledger. Before each call
the guard checks this run's spend and the ledger's total against agents/config.yaml (budget) and refuses to
call the model past either cap. Tests use FakeModel, which costs nothing.
"""
from __future__ import annotations

import os
import time

from pydantic import BaseModel


class BudgetExceeded(RuntimeError):
    pass


def price(usage, prices):
    """US dollars for one call: uncached input, cache reads, cache writes and output (thinking is billed as
    output). input_tokens counts all three kinds of input."""
    inp = int(usage.get("input_tokens", 0))
    details = usage.get("input_token_details") or {}
    read = int(details.get("cache_read", 0) or 0)
    write = int(details.get("cache_creation", 0) or 0)
    out = int(usage.get("output_tokens", 0))
    return ((inp - read - write) * prices["input"] + read * prices["cached_input"]
            + write * prices.get("cache_write", prices["input"]) + out * prices["output"]) / 1e6


class SpendGuard:
    def __init__(self, cfg, handoff, investigation_id, agent):
        self.cfg, self.handoff, self.inv, self.agent = cfg, handoff, investigation_id, agent

    def check(self):
        t = self.handoff.spend(self.inv)
        run = t.get("investigation", {}).get("usd", 0.0)
        if run >= self.cfg["budget"]["per_run_usd"]:
            raise BudgetExceeded(f"this investigation has spent ${run:.2f}, its cap is ${self.cfg['budget']['per_run_usd']:.2f}")
        if t["total"]["usd"] >= self.cfg["budget"]["total_usd"]:
            raise BudgetExceeded(f"model spend reached ${t['total']['usd']:.2f} of ${self.cfg['budget']['total_usd']:.2f}; "
                                 f"raise budget.total_usd in agents/config.yaml to continue")

    def record(self, usage, model_id):
        usd = price(usage, self.cfg["model"]["price_usd_per_million"])
        self.handoff.record_spend({
            "agent": self.agent, "investigation_id": self.inv, "model": model_id,
            "input_tokens": int(usage.get("input_tokens", 0)),
            "cached_input_tokens": int((usage.get("input_token_details") or {}).get("cache_read", 0) or 0),
            "output_tokens": int(usage.get("output_tokens", 0)),
            "reasoning_tokens": int((usage.get("output_token_details") or {}).get("reasoning", 0) or 0),
            "usd": round(usd, 6)})
        return usd


def chat_model(cfg):
    """ChatOpenAI pointed at the chosen OpenAI-compatible endpoint (cfg["model"], resolved by config.load)."""
    from langchain_openai import ChatOpenAI
    m = cfg["model"]
    base = m.get("base_url")
    if not base:
        raise RuntimeError(f"no base URL for {m['id']}: set it in agents/config.yaml or {m['base_url_env']}")
    key = os.environ.get(m["api_key_env"]) or "set-by-the-gateway"
    return ChatOpenAI(model=m.get("model_id") or m["id"], base_url=base, api_key=key, reasoning_effort=m.get("reasoning_effort"),
                      max_completion_tokens=m["max_output_tokens"], timeout=m["timeout_s"],
                      max_retries=int(m.get("max_retries", 6)),
                      stream_usage=True)


def make_model(cfg, guard, callbacks=None):
    """The agents' model for cfg["model"]["provider"]."""
    from agents.common.anthropic_api import PROVIDERS
    return ClaudeModel(cfg, guard) if cfg["model"]["provider"] in PROVIDERS else Model(cfg, guard, callbacks)


class ClaudeModel:
    """Structured decisions from Claude (Anthropic API), priced, capped and traced as Langfuse generations."""

    def __init__(self, cfg, guard: SpendGuard, api=None):
        from agents.common import anthropic_api
        self.cfg, self.guard, self.A = cfg, guard, anthropic_api
        m = cfg["model"]
        self.api = api or anthropic_api.Claude(m)
        self.spent = 0.0
        self.tokens = 0                                        # input + output, for the Step nodes

    def _call(self, name, system, msgs, **kw):
        from agents.common import tracing
        m = self.cfg["model"]
        self.guard.check()
        with tracing.generation(name, m["id"], {"effort": m.get("effort"), "max_tokens": m["max_output_tokens"]},
                                {"system": system, "messages": msgs}) as gen:
            resp = self.api.create(system, msgs, **kw)
            u = self.A.usage(resp)
            usd = self.guard.record(u, m["id"])
            self.spent += usd
            self.tokens += int(u.get("input_tokens", 0)) + int(u.get("output_tokens", 0))
            tracing.end_generation(gen, resp.get("content"), u, usd)
        return resp

    def decide(self, messages, schema: type[BaseModel], name="decide"):
        """One call; the answer is JSON constrained to `schema` (structured outputs)."""
        system, msgs = self.A.to_messages(messages)
        js = schema.model_json_schema()
        resp = self._call(name, system, msgs, schema=js)
        return self._parse(resp, schema, js)

    def _parse(self, resp, schema, js):
        try:
            data = self.A.parse_json(resp)
        except ValueError as e:
            raise ValueError(f"the model's answer did not fit {schema.__name__}: {e}") from e
        return schema.model_validate(self.A.fix_enums(data, self.A.strict_schema(js)))

    def decide_with_tools(self, messages, tools, schema: type[BaseModel], max_calls, name="decide"):
        """Let the model look things up with `tools` (at most max_calls calls), then answer with `schema`.

        tool_choice is "auto" while lookups remain and "none" after, and every call carries the answer schema,
        so the turn that does not call a tool is the structured answer.
        """
        system, msgs = self.A.to_messages(messages)
        by_name = {t.name: t for t in tools}
        defs = [self.A.tool_def(t) for t in tools]
        js = schema.model_json_schema()
        used = 0
        while True:
            left = used < max_calls
            resp = self._call(name, system, msgs, tools=defs, schema=js,
                              tool_choice={"type": "auto"} if left else {"type": "none"})
            calls = [b for b in resp.get("content", []) if b.get("type") == "tool_use"]
            if not (calls and left and resp.get("stop_reason") == "tool_use"):
                return self._parse(resp, schema, js), used
            # The reply goes back as it came (thinking blocks kept as they are), less any empty text block: Claude
            # sometimes writes one before a tool call, and Bedrock refuses it when it is sent back ("text content
            # blocks must be non-empty"; Phase 4 sample run, CMP-0271).
            msgs.append({"role": "assistant", "content": [b for b in resp["content"]
                                                          if b.get("type") != "text" or (b.get("text") or "").strip()]})
            results = []
            for c in calls:
                used += 1
                tool = by_name.get(c["name"])
                if not tool or used > max_calls:
                    out = {"error": {"code": "lookup_budget" if tool else "unknown_tool",
                                     "message": "no more lookups; answer now" if tool else f"no tool {c['name']}"}}
                else:
                    try:
                        out = tool.invoke(c["input"])
                    except Exception as e:                    # bad arguments: tell the model, do not stop the run
                        out = {"error": {"code": "bad_input", "message": str(e)[:300]}}
                results.append({"type": "tool_result", "tool_use_id": c["id"], "content": str(out)[:6000]})
            msgs.append({"role": "user", "content": results})


class Model:
    """Structured decisions from an OpenAI-compatible model (Bedrock), priced and capped. callbacks: e.g.
    Langfuse's LangChain handler."""

    def __init__(self, cfg, guard: SpendGuard, callbacks=None, llm=None):
        self.cfg, self.guard, self.callbacks = cfg, guard, callbacks or []
        self.llm = llm or chat_model(cfg)
        self.spent = 0.0
        self.tokens = 0

    def _invoke(self, runnable, messages, name):
        self.guard.check()
        msg = runnable.invoke(messages, config={"callbacks": self.callbacks, "run_name": name})
        raw = msg["raw"] if isinstance(msg, dict) else msg
        usage = getattr(raw, "usage_metadata", None) or {}
        self.spent += self.guard.record(usage, self.cfg["model"]["id"])
        self.tokens += int(usage.get("input_tokens", 0)) + int(usage.get("output_tokens", 0))
        return msg

    def decide(self, messages, schema: type[BaseModel], name="decide"):
        """`schema` through a tool call. The request forces the tool, but the prompt also asks for it, because
        Bedrock's gpt-oss can reply in text instead; an answer written as JSON in the text is accepted, and a
        reply without one gets one reminder before the run fails with what the model sent."""
        runnable = self.llm.with_structured_output(schema, method="function_calling", include_raw=True)
        msgs = list(messages) + [_human(f"Answer only by calling the {schema.__name__} tool.")]
        for attempt in (1, 2):
            r = self._invoke(runnable, msgs, name)
            if r.get("parsed") is not None and not r.get("parsing_error"):
                return r["parsed"]
            found = _from_text(r["raw"], schema)
            if found is not None:
                return found
            problem = r.get("parsing_error") or _describe(r["raw"])
            if attempt == 2:
                raise ValueError(f"the model's answer did not fit {schema.__name__} (2 tries): {problem}")
            msgs = msgs + _retry(r["raw"], f"That answer could not be used ({str(problem)[:200]}). "
                                           f"Answer now by calling the {schema.__name__} tool.")

    def decide_with_tools(self, messages, tools, schema: type[BaseModel], max_calls, name="decide"):
        """Let the model look things up with `tools` (at most max_calls calls), then answer with `schema`.

        tools: LangChain tools. The answer is itself a tool named after the schema; the loop ends when the
        model calls it, or the lookup budget is used up and the answer is forced.
        """
        from langchain_core.messages import ToolMessage
        from pydantic import ValidationError
        by_name = {t.name: t for t in tools}
        bound = self.llm.bind_tools(list(tools) + [schema])
        msgs = list(messages) + [_human(f"Look things up with the tools if you need to, then answer by calling "
                                        f"the {schema.__name__} tool.")]
        used, forced = 0, 0
        while True:
            force = used >= max_calls
            forced += force
            runnable = self.llm.bind_tools([schema], tool_choice=schema.__name__) if force else bound
            ai = self._invoke(runnable, msgs, name)
            calls = ai.tool_calls or []
            final = [c for c in calls if c["name"] == schema.__name__]
            if final:
                try:
                    return schema.model_validate(final[0]["args"]), used
                except ValidationError as e:
                    if forced >= 2:
                        raise ValueError(f"the model's answer did not fit {schema.__name__} (2 tries): {e}") from e
                    msgs.append(ai)
                    msgs += [ToolMessage(content=f"error: this answer does not fit the schema: {str(e)[:400]}. "
                                                 f"Call {schema.__name__} again with a valid answer.",
                                         tool_call_id=c["id"]) for c in calls]
                    used = max_calls
                    continue
            if not calls:
                found = _from_text(ai, schema)
                if found is not None:
                    return found, used
                if forced >= 2:
                    raise ValueError(f"the model did not answer with {schema.__name__} (2 tries): {_describe(ai)}")
                msgs += _retry(ai, f"Answer now by calling the {schema.__name__} tool.")
                used = max_calls
                continue
            if forced >= 2:
                raise ValueError(f"the model kept calling other tools instead of {schema.__name__}: {_describe(ai)}")
            msgs.append(ai)
            for c in calls:
                used += 1
                tool = by_name.get(c["name"])
                out = tool.invoke(c["args"]) if tool and used <= max_calls else \
                    {"error": {"code": "lookup_budget", "message": "no more lookups; answer now"}}
                msgs.append(ToolMessage(content=str(out)[:6000], tool_call_id=c["id"]))


def _human(text):
    from langchain_core.messages import HumanMessage
    return HumanMessage(content=text)


def _text(ai):
    """The visible text of a reply (reasoning blocks left out)."""
    c = getattr(ai, "content", "") or ""
    if isinstance(c, list):
        c = "".join(b.get("text", "") if isinstance(b, dict) else str(b) for b in c
                    if not (isinstance(b, dict) and b.get("type") in ("reasoning", "thinking")))
    return str(c)


def _from_text(ai, schema):
    """An answer written as JSON in the reply's text instead of a tool call, or None."""
    import json

    from pydantic import ValidationError
    text, dec = _text(ai), json.JSONDecoder()
    start = text.find("{")
    while start != -1:
        try:
            obj, _ = dec.raw_decode(text[start:])
        except ValueError:
            obj = None
        if isinstance(obj, dict):
            for cand in (obj, obj.get("arguments"), obj.get("parameters"), obj.get(schema.__name__)):
                if isinstance(cand, str):
                    try:
                        cand = json.loads(cand)
                    except ValueError:
                        continue
                if isinstance(cand, dict):
                    try:
                        return schema.model_validate(cand)
                    except ValidationError:
                        pass
        start = text.find("{", start + 1)
    return None


def _describe(ai):
    """What a reply contained, for an error message."""
    meta = getattr(ai, "response_metadata", None) or {}
    calls = [c["name"] for c in (getattr(ai, "tool_calls", None) or [])]
    bad = [c.get("name") for c in (getattr(ai, "invalid_tool_calls", None) or [])]
    return (f"finish_reason {meta.get('finish_reason')}, tool calls {calls or 'none'}"
            + (f", unreadable tool calls {bad}" if bad else "") + f", text {_text(ai)[:300]!r}")


def _retry(ai, text):
    """Messages that show the model its unusable reply (as plain text, so no tool call is left unanswered)
    and ask again."""
    from langchain_core.messages import AIMessage
    said = _text(ai).strip()
    return ([AIMessage(content=said[:2000])] if said else []) + [_human(text)]


class FakeModel:
    """Scripted stand-in for tests: returns the queued answers in order and records zero spend."""

    def __init__(self, answers, tokens_per_call=0):
        self.answers = list(answers)
        self.prompts = []
        self.spent = 0.0
        self.tokens = 0
        self.tokens_per_call = tokens_per_call

    def decide(self, messages, schema, name="decide"):
        self.prompts.append((name, messages))
        self.tokens += self.tokens_per_call
        a = self.answers.pop(0)
        a = a(messages) if callable(a) else a
        return a if isinstance(a, schema) else schema.model_validate(a)

    def decide_with_tools(self, messages, tools, schema, max_calls, name="decide"):
        return self.decide(messages, schema, name), 0


def wall_clock(started, timeout_s, what):
    if time.time() - started > timeout_s:
        raise TimeoutError(f"{what} ran longer than {timeout_s} s")
