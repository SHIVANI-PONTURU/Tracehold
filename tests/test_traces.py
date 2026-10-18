"""The UI's Agent trace tab: the run as Langfuse recorded it, and the requests the sandboxes refused."""
from ui_server import traces as T


def test_langfuse_cost_is_summed_per_agent_span(monkeypatch):
    monkeypatch.setenv("LANGFUSE_HOST", "https://lf.example")
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk")
    obs = [{"id": "a", "type": "AGENT", "name": "tracer"}, {"id": "c", "type": "SPAN", "name": "judge", "parentObservationId": "a"},
           {"id": "g", "type": "GENERATION", "name": "tracer.judge", "parentObservationId": "c", "totalCost": 0.05}]
    lf = T.langfuse_summary("t" * 32, http_get=lambda url, params: {"data": obs, "meta": {}})
    assert lf == {"observations": 3, "generations": 1, "usd": 0.05, "agents": {"tracer": {"generations": 1,
                                                                                           "USD (Langfuse)": 0.05}}}


def test_langfuse_without_keys_says_so(monkeypatch):
    for k in ("LANGFUSE_HOST", "LANGFUSE_BASE_URL", "LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY"):
        monkeypatch.delenv(k, raising=False)
    assert "error" in T.langfuse_summary("t" * 32, http_get=lambda url, params: {})


def test_blocked_requests_keep_only_denied_lines():
    blocked = T.blocked_requests(runner=lambda a: "x OCSF NET DENIED transparent_tcp_policy_denied 6379\nok line\n"
                                 if a == "tracer" else "")
    assert blocked == [{"agent": "tracer", "line": "x OCSF NET DENIED transparent_tcp_policy_denied 6379"}]
