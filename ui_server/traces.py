"""What the UI's Agent trace tab reads beside the case itself: the run as Langfuse recorded it (its public API) and the
requests the OpenShell sandboxes refused (from `openshell logs`, on the server)."""
from __future__ import annotations

import os
import re
import shutil
import subprocess

AGENTS = ("tracer", "adjudicator", "verifier")


def _cost(o):
    for k in ("totalCost", "calculatedTotalCost"):
        if isinstance(o.get(k), (int, float)):
            return float(o[k])
    cd = o.get("costDetails") or {}
    return float(cd.get("total", 0) or 0)


def langfuse_summary(trace_id, http_get=None):
    """The run as Langfuse recorded it: observations, generations and cost per agent span. Reads the v2
    observations API (the v1 trace API is closed for organisations created after 16 Sep 2026)."""
    host = (os.environ.get("LANGFUSE_HOST") or os.environ.get("LANGFUSE_BASE_URL") or "").rstrip("/")
    pk, sk = os.environ.get("LANGFUSE_PUBLIC_KEY"), os.environ.get("LANGFUSE_SECRET_KEY")
    if not (host and pk and sk):
        return {"error": "Langfuse keys are not set for the UI (LANGFUSE_HOST, LANGFUSE_PUBLIC_KEY, LANGFUSE_SECRET_KEY)"}
    try:
        if http_get is None:
            import httpx2
            def http_get(url, params):
                return httpx2.get(url, params=params, auth=(pk, sk), timeout=20).json()
        obs, page = [], None
        for _ in range(10):
            params = {"traceId": trace_id, "fields": "core,basic,usage,model", "limit": 100}
            if page:
                params["cursor"] = page
            r = http_get(f"{host}/api/public/v2/observations", params)
            obs += r.get("data", [])
            page = (r.get("meta") or {}).get("cursor") or (r.get("meta") or {}).get("nextCursor")
            if not page:
                break
    except Exception as e:
        return {"error": f"{type(e).__name__}: {str(e)[:200]}"}
    by_id = {o.get("id"): o for o in obs}

    def agent_of(o):
        seen = 0
        while o and seen < 30:
            if o.get("type") == "AGENT" or (o.get("name") in AGENTS + ("gate", "chat")):
                return o.get("name")
            o = by_id.get(o.get("parentObservationId"))
            seen += 1
        return "?"
    gens = [o for o in obs if o.get("type") == "GENERATION"]
    agents = {}
    for g in gens:
        a = agents.setdefault(agent_of(g), {"generations": 0, "USD (Langfuse)": 0.0})
        a["generations"] += 1
        a["USD (Langfuse)"] = round(a["USD (Langfuse)"] + _cost(g), 4)
    return {"observations": len(obs), "generations": len(gens), "usd": round(sum(_cost(g) for g in gens), 4),
            "agents": agents}


DENIED = re.compile(r"DENIED")


def blocked_requests(since="6h", runner=None):
    """Requests the sandboxes refused, from each sandbox's OpenShell policy log; None where OpenShell is not here."""
    if runner is None:
        if not shutil.which("openshell"):
            return None
        def runner(agent):
            r = subprocess.run(["openshell", "logs", f"th-{agent}", "--since", since, "--source", "sandbox"],
                               capture_output=True, text=True, timeout=20)
            return r.stdout
    out = []
    for a in AGENTS:
        try:
            text = runner(a)
        except Exception as e:
            out.append({"agent": a, "line": f"(could not read the log: {type(e).__name__})"})
            continue
        out += [{"agent": a, "line": line.strip()[:300]} for line in text.splitlines() if DENIED.search(line)][-20:]
    return out
