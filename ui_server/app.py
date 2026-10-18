"""The analyst UI server: serves the React app built into web/dist, forwards the app's calls to the tool server's
handoff API, and adds the few reads a browser cannot do itself (evaluation results, Langfuse, sandbox logs).

  python -m ui_server.app        http://127.0.0.1:8501 (UI_ADDRESS and UI_PORT change it)

Only the UI's own handoff routes are forwarded (ALLOW): the routes the agents' workers use (claim, submit, step, the
gate's record, execute and finish, chat answers, spend records) are never reachable from a browser. The UI never
talks to FalkorDB and never reads ground-truth labels; model keys stay on the server.
"""
from __future__ import annotations

import contextlib
import glob
import json
import os
import re
import time

import httpx
from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, Response
from starlette.routing import Route

from agents.common.config import load
from data import settings

DIST = os.environ.get("TRACEHOLD_WEB_DIST") or os.path.join(settings.ROOT, "web", "dist")
EVAL_DIR = os.path.join(settings.WORK_DIR, "reports", "evaluation")
MAX_BODY = 1_000_000
BANK, INV = r"\d{1,6}", r"INV-[0-9A-Za-z-]{4,40}"
ALLOW = {"GET": [r"complaints", r"banks", r"spend", r"spend/calls", r"investigations", rf"investigations/{BANK}/{INV}",
                 rf"gate/{BANK}/{INV}", r"reviews", r"memory", rf"trail/{BANK}/{INV}", rf"chat/{BANK}/{INV}",
                 r"graph/activity", r"graph/stats"],
         "POST": [r"investigations", r"gate/decide", r"actions/undo", r"reviews/resolve", r"chat/ask", r"close"]}
EVAL_RE = re.compile(r"^evaluation_\d{8}_\d{6}(_[ab]_v\d+)?\.json$")
TRACE_RE = re.compile(r"^[0-9a-f]{16,64}$")


def allowed(method, path):
    """Is this handoff route open to the UI? Path is the part after /handoff/."""
    return any(re.fullmatch(p, path) for p in ALLOW.get(method, []))


def _err(status, code, message):
    return JSONResponse({"error": {"code": code, "message": message}}, status_code=status)


def evaluations(folder=EVAL_DIR, _cache={}):
    """Every evaluation results file, newest first, with what it covers (read once per file version)."""
    out = []
    for path in sorted(glob.glob(os.path.join(folder, "evaluation_*.json")), reverse=True):
        name = os.path.basename(path)
        if not EVAL_RE.match(name):
            continue
        key = (path, os.path.getmtime(path))
        if key not in _cache:
            with open(path, encoding="utf-8") as f:
                r = json.load(f)
            m, h = r.get("meta", {}), r.get("headline") or {}
            _cache[key] = {"file": name, "written": m.get("written"), "sample": m.get("sample_name", "a"),
                           "rubric": m.get("rubric", "v1"), "tracehold_runs": m.get("tracehold_runs"),
                           "complaints": h.get("complaints")}
        out.append(_cache[key])
    return out


def evaluation(name=None, folder=EVAL_DIR):
    """One results file by name (the newest when no name); None when there is none."""
    files = evaluations(folder)
    if not files:
        return None
    if name is None:
        name = files[0]["file"]
    if not EVAL_RE.match(name or "") or name not in {f["file"] for f in files}:
        raise ValueError(f"no evaluation file {name!r}")
    with open(os.path.join(folder, name), encoding="utf-8") as f:
        return json.load(f)


def build(tool_server_url=None, dist=DIST, eval_dir=EVAL_DIR, transport=None):
    cfg = load()
    tool = (tool_server_url or cfg["tool_server_url"]).rstrip("/")
    http = httpx.AsyncClient(base_url=tool, timeout=httpx.Timeout(30.0), transport=transport)
    cache = {}

    async def handoff(request: Request):
        path = request.path_params["path"]
        if not allowed(request.method, path):
            return _err(403, "forbidden", f"{request.method} /handoff/{path} is not open to the UI")
        body = await request.body() if request.method == "POST" else None
        if body is not None and len(body) > MAX_BODY:
            return _err(413, "too_large", "request too large")
        try:
            r = await http.request(request.method, f"/handoff/{path}", params=request.query_params, content=body,
                                   headers={"content-type": "application/json"} if body is not None else None)
        except httpx.HTTPError as e:
            return _err(502, "tool_server_unreachable", f"the tool server did not answer ({type(e).__name__})")
        return Response(r.content, status_code=r.status_code, media_type=r.headers.get("content-type", "application/json"))

    async def ui_config(request):
        lf = os.environ.get("LANGFUSE_HOST") or os.environ.get("LANGFUSE_BASE_URL") or ""
        return JSONResponse({"banks": cfg["bank_ids"], "default_bank": cfg["bank_id"],
                             "gate_mode": cfg["verifier"]["gate_mode"], "budget": cfg["budget"],
                             "default_model": cfg["model"]["id"], "langfuse_host": lf.rstrip("/"),
                             "langfuse_ready": bool(lf and os.environ.get("LANGFUSE_PUBLIC_KEY"))})

    async def ui_health(request):
        try:
            ok = (await http.get("/health", timeout=3)).status_code == 200
        except httpx.HTTPError:
            ok = False
        return JSONResponse({"ok": True, "tool_server": ok, "web_built": os.path.exists(os.path.join(dist, "index.html"))})

    async def ui_evaluations(request):
        return JSONResponse(await run_in_threadpool(evaluations, eval_dir))

    async def ui_evaluation(request):
        try:
            r = await run_in_threadpool(evaluation, request.query_params.get("file"), eval_dir)
        except ValueError as e:
            return _err(404, "not_found", str(e))
        return JSONResponse(r) if r is not None else _err(404, "not_found", "no evaluation run yet")

    async def ui_langfuse(request):
        tid = request.path_params["trace_id"]
        if not TRACE_RE.match(tid):
            return _err(400, "bad_input", "not a trace ID")
        from ui_server import traces
        return JSONResponse(await run_in_threadpool(traces.langfuse_summary, tid))

    async def ui_blocked(request):
        hit = cache.get("blocked")
        if not hit or time.time() - hit[0] > 30:
            from ui_server import traces
            hit = cache["blocked"] = (time.time(), await run_in_threadpool(traces.blocked_requests))
        return JSONResponse({"available": hit[1] is not None, "requests": hit[1] or [], "read_at": int(hit[0])})

    async def page(request):
        """A file from the built app, or index.html for the app's own routes (/investigations/..., ...)."""
        rel = request.path_params.get("path", "")
        if rel.startswith("api/"):
            return _err(404, "not_found", "no such API route")
        full = os.path.normpath(os.path.join(dist, rel))
        if rel and full.startswith(os.path.normpath(dist) + os.sep) and os.path.isfile(full):
            cached = "/assets/" in full.replace(os.sep, "/")
            return FileResponse(full, headers={"cache-control": "public, max-age=31536000, immutable" if cached
                                               else "no-cache"})
        index = os.path.join(dist, "index.html")
        if not os.path.exists(index):
            return Response("The web app is not built yet: in web/, run npm run build.", status_code=503)
        return FileResponse(index, headers={"cache-control": "no-cache"})

    @contextlib.asynccontextmanager
    async def lifespan(app):
        yield
        await http.aclose()

    routes = [Route("/api/h/{path:path}", handoff, methods=["GET", "POST"]),
              Route("/api/ui/config", ui_config), Route("/api/ui/health", ui_health),
              Route("/api/ui/evaluations", ui_evaluations), Route("/api/ui/evaluation", ui_evaluation),
              Route("/api/ui/langfuse/{trace_id}", ui_langfuse), Route("/api/ui/blocked", ui_blocked),
              Route("/{path:path}", page)]
    return Starlette(routes=routes, lifespan=lifespan, middleware=[Middleware(SecurityHeaders)])


class SecurityHeaders:
    """Basic browser hardening on every response (no framing, no MIME sniffing, same-origin referrers)."""
    EXTRA = ((b"x-content-type-options", b"nosniff"), (b"x-frame-options", b"DENY"), (b"referrer-policy", b"same-origin"))

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)

        async def send_with_headers(msg):
            if msg["type"] == "http.response.start":
                hs = list(msg.get("headers", []))
                have = {k.lower() for k, _ in hs}
                msg["headers"] = hs + [(k, v) for k, v in self.EXTRA if k not in have]
            await send(msg)
        await self.app(scope, receive, send_with_headers)


def main():
    import uvicorn
    uvicorn.run(build(), host=os.environ.get("UI_ADDRESS", "127.0.0.1"), port=int(os.environ.get("UI_PORT", "8501")),
                log_level="warning")


if __name__ == "__main__":
    main()
