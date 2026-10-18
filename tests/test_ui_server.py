"""The React UI's server (ui_server/app.py) and the reads it needs: only the UI's own handoff routes are forwarded,
results files are found by name only, the app's routes fall back to index.html, every response carries the browser
hardening headers; plus the spend ledger's per-call rows and the FalkorDB query log behind the live view."""
import json
import os

import httpx
import pytest
from starlette.testclient import TestClient

from graph import client as G
from handoff import api as H
from handoff import spend
from mcp_server import tools as T
from ui_server import app as U

AGENT_ONLY = [("POST", "claim"), ("POST", "submit"), ("POST", "step"), ("POST", "gate/record"), ("POST", "gate/execute"),
              ("POST", "gate/finish"), ("POST", "fail"), ("POST", "spend"), ("POST", "chat/claim"),
              ("POST", "chat/answer"), ("GET", "read/11/INV-0011-0cce9cd83d25/trace")]


def make_app(tmp_path, handler):
    seen = []

    def record(request):
        seen.append(request)
        return handler(request)
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text("<!doctype html><title>TraceHold</title>", encoding="utf-8")
    (dist / "assets" / "app.js").write_text("console.log(1)", encoding="utf-8")
    (tmp_path / "secret.txt").write_text("not for the browser", encoding="utf-8")
    app = U.build("http://tool.test", dist=str(dist), eval_dir=str(tmp_path / "eval"),
                  transport=httpx.MockTransport(record))
    return app, seen


def test_agent_routes_are_never_forwarded(tmp_path):
    app, seen = make_app(tmp_path, lambda r: httpx.Response(200, json={}))
    with TestClient(app) as c:
        for method, path in AGENT_ONLY:
            r = c.request(method, f"/api/h/{path}", json={})
            assert r.status_code == 403 and r.json()["error"]["code"] == "forbidden", (method, path)
        assert c.get("/api/h/investigations/11/../claim").status_code in (403, 404)
    assert seen == []                                           # nothing reached the tool server


def test_ui_routes_are_forwarded_with_their_query_and_body(tmp_path):
    def handler(r):
        if r.url.path == "/handoff/gate/decide":
            return httpx.Response(409, json={"error": {"code": "wrong_state", "message": "not at the gate"}})
        return httpx.Response(200, json=[{"id": "INV-0011-0cce9cd83d25"}])
    app, seen = make_app(tmp_path, handler)
    with TestClient(app) as c:
        r = c.get("/api/h/investigations", params={"bank_id": "11"})
        assert r.status_code == 200 and r.json()[0]["id"] == "INV-0011-0cce9cd83d25"
        body = {"bank_id": "11", "investigation_id": "INV-0011-0cce9cd83d25", "decisions": [], "gate_mode": "placeholder"}
        r = c.post("/api/h/gate/decide", json=body)
        assert r.status_code == 409 and r.json()["error"]["code"] == "wrong_state"   # the tool server's answer, as is
    assert seen[0].method == "GET" and seen[0].url.path == "/handoff/investigations" and seen[0].url.params["bank_id"] == "11"
    assert seen[1].method == "POST" and json.loads(seen[1].content) == body


def test_an_unreachable_tool_server_is_a_502(tmp_path):
    def handler(r):
        raise httpx.ConnectError("refused")
    app, _ = make_app(tmp_path, handler)
    with TestClient(app) as c:
        r = c.get("/api/h/banks", params={"ids": "11"})
    assert r.status_code == 502 and r.json()["error"]["code"] == "tool_server_unreachable"


def test_results_files_are_found_by_name_only(tmp_path):
    folder = tmp_path / "eval"
    folder.mkdir()
    for name, sample, rubric in (("evaluation_20261017_201321.json", "a", "v1"),
                                 ("evaluation_20261017_211446_b_v2.json", "b", "v2")):
        (folder / name).write_text(json.dumps({"meta": {"written": name[11:26], "sample_name": sample, "rubric": rubric,
                                                        "tracehold_runs": 41}, "headline": {"complaints": 31}}))
    (folder / "evaluation_notes.json").write_text("{}")
    files = U.evaluations(str(folder))
    assert [f["file"] for f in files] == ["evaluation_20261017_211446_b_v2.json", "evaluation_20261017_201321.json"]
    assert files[0]["sample"] == "b" and files[0]["rubric"] == "v2" and files[0]["complaints"] == 31
    assert U.evaluation(None, str(folder))["meta"]["sample_name"] == "b"           # the newest by default
    assert U.evaluation("evaluation_20261017_201321.json", str(folder))["meta"]["sample_name"] == "a"
    for bad in ("../secret.txt", "evaluation_notes.json", "evaluation_20261017_999999.json"):
        with pytest.raises(ValueError):
            U.evaluation(bad, str(folder))
    assert U.evaluation(None, str(tmp_path / "missing")) is None


def test_the_apps_routes_fall_back_to_index_and_files_stay_inside_dist(tmp_path):
    app, _ = make_app(tmp_path, lambda r: httpx.Response(200, json={}))
    with TestClient(app) as c:
        page = c.get("/investigations/INV-0011-0cce9cd83d25")
        assert page.status_code == 200 and "TraceHold" in page.text and page.headers["cache-control"] == "no-cache"
        js = c.get("/assets/app.js")
        assert js.status_code == 200 and "immutable" in js.headers["cache-control"]
        assert "not for the browser" not in c.get("/../secret.txt").text
        assert c.get("/api/nothing").status_code == 404
        r = c.get("/")
        for k, v in (("x-frame-options", "DENY"), ("x-content-type-options", "nosniff"), ("referrer-policy", "same-origin")):
            assert r.headers[k] == v


def test_the_spend_ledger_lists_calls_newest_first(tmp_path):
    db = str(tmp_path / "spend.sqlite")
    for inv, agent, usd in (("INV-0011-a", "tracer", 0.01), ("INV-0011-a", "adjudicator", 0.07), ("INV-0020-b", "tracer", 0.02)):
        spend.record({"agent": agent, "investigation_id": inv, "model": "bedrock/claude-opus-4-6", "input_tokens": 100,
                      "cached_input_tokens": 0, "output_tokens": 20, "reasoning_tokens": 0, "usd": usd}, path=db)
    rows = spend.calls(path=db)
    assert [r["usd"] for r in rows] == [0.02, 0.07, 0.01] and rows[0]["id"] > rows[1]["id"]
    assert [r["agent"] for r in spend.calls("INV-0011-a", path=db)] == ["adjudicator", "tracer"]
    assert len(spend.calls(limit=1, path=db)) == 1


def test_the_query_log_keeps_who_ran_each_query():
    token = G.CALLER.set({"source": "tool", "name": "trace_funds", "bank": "11", "investigation_id": "INV-0011-q"})
    try:
        e = G.record("network", "read", "MATCH (a:Account {id: $id})\n  RETURN a", {"id": "11-800178CC0", "ids": list(range(9))},
                     12.5, rows=1, engine_ms=0.8)
    finally:
        G.CALLER.reset(token)
    G.record("memory_11", "read-write", "MERGE (s:Step {id: $id})", {"id": "x"}, 3.0, rows=0)
    assert e["query"] == "MATCH (a:Account {id: $id}) RETURN a" and e["params"]["ids"] == "[9 items]"
    assert e["source"] == "tool" and e["name"] == "trace_funds" and e["investigation_id"] == "INV-0011-q"
    tool = G.activity(source="tool", investigation_id="INV-0011-q")["entries"]
    assert [x["seq"] for x in tool] == [e["seq"]]
    later = G.activity(since_seq=e["seq"])
    assert all(x["seq"] > e["seq"] for x in later["entries"]) and later["entries"][-1]["graph"] == "memory_11"
    assert later["entries"][-1]["source"] == "other" and later["totals"]["network"]["queries"] >= 1


def test_tools_and_routes_tag_their_queries():
    def trace_funds(ctx, bank_id, investigation_id):
        return G.record("network", "read", "MATCH (n) RETURN n", {}, 1.0)
    e = T.run_tool(trace_funds, None, "20", "INV-0354-6ec246a8dea4")
    assert (e["source"], e["name"], e["bank"], e["investigation_id"]) == ("tool", "trace_funds", "20", "INV-0354-6ec246a8dea4")
    assert G.CALLER.get() is None                              # reset after the tool

    def claim(pp, qp, b):
        return G.record("memory_11", "read", "MATCH (i:Investigation) RETURN i", {}, 1.0)

    def detail(pp, qp, b):
        return G.record("memory_11", "read", "MATCH (i:Investigation {id: $id}) RETURN i", {}, 1.0)
    assert H._tagged(claim, {}, {}, {"bank_id": "11"})["source"] == "agent"
    # the header's per-agent status reads which agent polled: from the claim body, or the Verifier for its own routes
    assert H._tagged(claim, {}, {}, {"bank_id": "11", "agent": "adjudicator"})["agent"] == "adjudicator"

    def chat_claim(pp, qp, b):
        return G.record("memory_11", "read", "MATCH (m:Message) RETURN m", {}, 1.0)
    assert H._tagged(chat_claim, {}, {}, {"bank_id": "11"})["agent"] == "verifier"
    d = H._tagged(detail, {"bank": "11", "inv": "INV-0011-0cce9cd83d25"}, {}, {})
    assert d["agent"] == ""
    assert (d["source"], d["name"], d["bank"], d["investigation_id"]) == ("ui", "detail", "11", "INV-0011-0cce9cd83d25")
    assert H._tagged(detail, {}, {}, ["not", "a", "dict"])["source"] == "ui"     # a malformed body still tags


def test_every_ui_handoff_route_exists_on_the_tool_server():
    """The allowlist only names routes the handoff API really has (so a typo cannot silently break a page)."""
    paths = {(m, r.path) for r in H.make_routes(None) for m in r.methods}
    for method, patterns in U.ALLOW.items():
        for p in patterns:
            concrete = p.replace(U.BANK, "11").replace(U.INV, "INV-0011-0cce9cd83d25")
            assert any(m == method and _matches(path, concrete) for m, path in paths), (method, p)


def _matches(route_path, concrete):
    import re
    return re.fullmatch(re.sub(r"\{[a-z]+\}", "[^/]+", route_path), "/handoff/" + concrete) is not None


def test_the_web_build_is_not_required_for_the_api(tmp_path):
    app = U.build("http://tool.test", dist=str(tmp_path / "nothing"), eval_dir=str(tmp_path),
                  transport=httpx.MockTransport(lambda r: httpx.Response(200, json={})))
    with TestClient(app) as c:
        assert c.get("/").status_code == 503 and "npm run build" in c.get("/").text
        assert c.get("/api/ui/health").json()["web_built"] is False
    assert not os.path.exists(tmp_path / "nothing")
