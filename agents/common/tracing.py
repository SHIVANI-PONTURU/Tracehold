"""Phase 2 step 13: Langfuse tracing.

One trace per investigation across all three agents: the trace ID is derived from the investigation ID (the
same SHA-256 rule as Langfuse.create_trace_id(seed=...)), set by the tool server when the investigation starts,
and carried in every handoff. Each agent run is one agent observation in that trace, with the Langfuse
LangChain callback inside it, so the graph's nodes nest under it; each Claude call is a generation (with its
tokens and cost) under the same agent observation. Session = investigation,
user = bank. A mask function hides account and customer numbers before anything leaves the process.

Tracing is on when LANGFUSE_PUBLIC_KEY, LANGFUSE_SECRET_KEY and LANGFUSE_HOST (or LANGFUSE_BASE_URL) are set;
otherwise runs go untraced. It never stops a run.
"""
from __future__ import annotations

import contextlib
import hashlib
import os
import re

ACCOUNT = re.compile(r"\b([0-9]{1,6})-([0-9A-F]{5})([0-9A-F]{4})\b")
ENTITY = re.compile(r"\b(?<![-A-Z0-9])([0-9A-F]{5})([0-9A-F]{4})(?![-A-Z0-9])\b")
_client = None


def trace_id_for(investigation_id):
    """32 hex characters, equal to Langfuse.create_trace_id(seed=investigation_id)."""
    return hashlib.sha256(investigation_id.encode("utf-8")).digest()[:16].hex()


def mask_text(s):
    """11-800178CC0 -> 11-*****8CC0; owner IDs such as 800603E50 -> *****3E50."""
    s = ACCOUNT.sub(lambda m: f"{m.group(1)}-*****{m.group(3)}", s)
    return ENTITY.sub(lambda m: f"*****{m.group(2)}", s)


def mask(*, data, **kwargs):
    if isinstance(data, str):
        return mask_text(data)
    if isinstance(data, dict):
        return {k: mask(data=v) for k, v in data.items()}
    if isinstance(data, (list, tuple)):
        return [mask(data=v) for v in data]
    return data


def enabled():
    return bool(os.environ.get("LANGFUSE_PUBLIC_KEY") and os.environ.get("LANGFUSE_SECRET_KEY")
                and (os.environ.get("LANGFUSE_HOST") or os.environ.get("LANGFUSE_BASE_URL")))


def client():
    global _client
    if _client is None:
        from langfuse import Langfuse
        _client = Langfuse(mask=mask, environment=os.environ.get("TRACEHOLD_ENV", "development"))
    return _client


@contextlib.contextmanager
def agent_run(agent, bank_id, investigation_id, complaint_id, trace_id=None):
    """with agent_run(...) as callbacks: graph.invoke(..., config={"callbacks": callbacks})"""
    if not enabled():
        yield []
        return
    from langfuse import propagate_attributes
    from langfuse.langchain import CallbackHandler
    lf = client()
    tid = trace_id or trace_id_for(investigation_id)
    try:
        with lf.start_as_current_observation(trace_context={"trace_id": tid}, name=agent, as_type="agent",
                                             metadata={"investigation_id": investigation_id}):
            with propagate_attributes(session_id=investigation_id, user_id=f"bank-{bank_id}",
                                      trace_name=f"investigation {complaint_id}",
                                      tags=["tracehold", f"bank-{bank_id}"]):
                yield [CallbackHandler()]
    finally:
        try:
            lf.flush()
        except Exception:
            pass


@contextlib.contextmanager
def generation(name, model, model_parameters, input):
    """One direct model call (the Anthropic path) as a Langfuse generation, nested in the current agent
    observation. Yields None when tracing is off or Langfuse fails; tracing never stops a run."""
    if not enabled():
        yield None
        return
    try:
        cm = client().start_as_current_observation(name=name, as_type="generation", model=model,
                                                   model_parameters=model_parameters, input=input)
        gen = cm.__enter__()
    except Exception:
        yield None
        return
    try:
        yield gen
    except BaseException as e:
        try:
            gen.update(level="ERROR", status_message=f"{type(e).__name__}: {str(e)[:300]}")
            cm.__exit__(type(e), e, e.__traceback__)
        except Exception:
            pass
        raise
    else:
        try:
            cm.__exit__(None, None, None)
        except Exception:
            pass


def score(trace_id, name, value, comment=""):
    """A score on a trace (Phase 3 step 13: each gate decision as feedback). No-op when tracing is off."""
    if not enabled():
        return
    try:
        lf = client()
        lf.create_score(name=name, value=float(value), trace_id=trace_id, data_type="NUMERIC", comment=mask_text(comment))
        lf.flush()
    except Exception:
        pass


def end_generation(gen, output, usage, usd):
    """Record a generation's answer, tokens and cost (no-op without a generation)."""
    if gen is None:
        return
    try:
        d = usage.get("input_token_details") or {}
        gen.update(output=output, usage_details={"input": int(usage.get("input_tokens", 0)) - int(d.get("cache_read", 0))
                                                 - int(d.get("cache_creation", 0)),
                                                 "input_cached_tokens": int(d.get("cache_read", 0)),
                                                 "output": int(usage.get("output_tokens", 0))},
                   cost_details={"total": float(usd)})
    except Exception:
        pass
