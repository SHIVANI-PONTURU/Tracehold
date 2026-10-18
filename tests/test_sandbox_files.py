"""Phase 2 steps 11-12 without OpenShell: the sandbox policies, provider profiles and agent image contents say
what plan Appendix I asks for. The live checks (blocked and logged requests, no key inside a sandbox) are
deploy/sandboxes.sh check, on a machine with OpenShell."""
import os
import subprocess
import sys

import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
AGENTS = ("tracer", "adjudicator", "verifier")
PYTHON = "/usr/local/bin/python3.13"


def policy(agent):
    with open(os.path.join(ROOT, "sandbox", "policies", f"{agent}.yaml"), encoding="utf-8") as f:
        return yaml.safe_load(f)


def rules(p):
    eps = [e for r in p["network_policies"].values() for e in r["endpoints"]]
    return {(x["allow"]["method"], x["allow"]["path"]) for e in eps for x in e.get("rules", [])}


def test_each_policy_is_deny_by_default_with_landlock_and_one_binary():
    for a in AGENTS:
        p = policy(a)
        assert p["version"] == 1
        assert p["landlock"]["compatibility"] == "hard_requirement"
        fs = p["filesystem_policy"]
        assert fs["include_workdir"] is True and fs["read_write"] == ["/dev/null"]
        assert "/" not in fs["read_only"] and "/opt/tracehold" in fs["read_only"]
        assert p["process"]["run_as_user"] == "sandbox"
        for name, r in p["network_policies"].items():
            assert [b["path"] for b in r["binaries"]] == [PYTHON], (a, name)
            for e in r["endpoints"]:
                assert e["enforcement"] == "enforce" and e["host"] == "host.openshell.internal" and e["port"] == 8000
                assert "access" not in e and e["rules"], (a, name)


def test_each_agent_reaches_only_its_own_mcp_path_and_never_the_gate_decision():
    for a in AGENTS:
        r = rules(policy(a))
        paths = {p for _, p in r}
        assert f"/mcp/{a}/**" in paths
        assert not any(p.startswith("/mcp/") and not p.startswith(f"/mcp/{a}") for p in paths)
        assert ("POST", "/handoff/gate/decide") not in r                  # only the UI, outside, decides
        assert not any(p.startswith("/handoff/investigations/") for p in paths)
    assert ("POST", "/handoff/gate/execute") in rules(policy("verifier"))
    assert not any("gate" in p for _, p in rules(policy("tracer")) | rules(policy("adjudicator")))


def test_provider_profiles_hold_the_keys_for_their_endpoints_only():
    for name, env, host in (("tracehold-bedrock", ["BEDROCK_API_KEY"], "bedrock-runtime.us-east-2.amazonaws.com"),
                            ("tracehold-langfuse", ["LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY"], "cloud.langfuse.com")):
        with open(os.path.join(ROOT, "sandbox", "profiles", f"{name}.yaml"), encoding="utf-8") as f:
            prof = yaml.safe_load(f)
        assert prof["id"] == name
        assert [v for c in prof["credentials"] for v in c["env_vars"]] == env
        assert [e["host"] for e in prof["endpoints"]] == [host]
        assert all(e["enforcement"] == "enforce" and e.get("tls") != "skip" for e in prof["endpoints"])
        assert all(e.get("rules") for e in prof["endpoints"])     # OpenShell's lint refuses an endpoint without rules
        assert prof["binaries"] == [PYTHON]


def test_the_agents_import_without_the_database_client_or_data_packages():
    """The agent image has no FalkorDB client, pandas or server packages; the workers must not need them."""
    code = ("import sys\n"
            "for m in ('falkordb', 'redis', 'pandas', 'numpy', 'pyarrow', 'pandera', 'streamlit', 'fastapi', 'uvicorn'):\n"
            "    sys.modules[m] = None\n"
            "import agents.worker, agents.tracer.graph, agents.adjudicator.graph, agents.verifier.graph\n"
            "import agents.common.clients, agents.common.model, agents.common.anthropic_api, agents.common.tracing\n")
    r = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stderr[-2000:]


def test_signal_names_have_one_source():
    from mcp_server import signals, tools
    assert tools.SIGNALS is signals.SIGNALS
