#!/usr/bin/env bash
# TraceHold, Phase 2 step 11 probe (Linux / WSL, OpenShell with the Podman driver).
#
#   bash deploy/sandbox_probe.sh
#
# Answers, before the agents move into sandboxes:
#   1. Can a sandbox reach a service on the gateway host (host.openshell.internal)?
#   2. Are other requests (another host port such as FalkorDB's, a public website) blocked and logged?
#   3. Does Landlock block writes outside the allowed folders?
#   4. Does the sandbox see any key-like environment variables?
# It also prints the CLI help the setup script relies on. Output goes to the screen and to
# sandbox/probe_output.txt. Nothing is left running: the test sandbox and the test service are removed.
set -uo pipefail
cd "$(dirname "$0")/.."
OUT=sandbox/probe_output.txt
exec > >(tee "$OUT") 2>&1

IMAGE=docker.io/library/python:3.13-slim
NAME=tracehold-probe
PORT=8765

echo "== versions"
openshell --version; podman --version; uname -r
echo "== help (flags for the setup script)"
for c in "sandbox create" "sandbox exec" "sandbox upload" "provider create" "profile import" "logs"; do
  echo "--- openshell $c --help"; openshell $c --help 2>&1 | head -80
done

echo "== pull $IMAGE"
podman pull -q "$IMAGE" || { echo "pull failed"; exit 1; }

echo "== test service on 0.0.0.0:$PORT (this machine)"
python3 -m http.server "$PORT" --bind 0.0.0.0 >/tmp/tracehold_probe_http.log 2>&1 &
HTTP_PID=$!
cleanup() { kill "$HTTP_PID" 2>/dev/null; openshell sandbox delete "$NAME" >/dev/null 2>&1; }
trap cleanup EXIT
sleep 1

echo "== create sandbox $NAME"
openshell sandbox delete "$NAME" >/dev/null 2>&1 || true
openshell sandbox create --name "$NAME" --from "$IMAGE" --policy sandbox/probe_policy.yaml --no-auto-providers \
  -- sleep 900 >/tmp/tracehold_probe_create.log 2>&1 &
ready=0
for _ in $(seq 60); do
  if openshell sandbox exec -n "$NAME" -- true >/dev/null 2>&1; then ready=1; break; fi
  sleep 2
done
openshell sandbox list
if [ "$ready" != 1 ]; then echo "sandbox did not become ready; create log:"; cat /tmp/tracehold_probe_create.log; exit 1; fi

echo "== tests inside the sandbox"
PROBE_PY='
import os, sys, urllib.request, urllib.error
def get(url):
    try:
        r = urllib.request.urlopen(url, timeout=15)
        return f"{r.status} (allowed)"
    except urllib.error.HTTPError as e:
        return f"HTTP {e.code}: {e.read()[:160]!r}"
    except Exception as e:
        return f"{type(e).__name__}: {str(e)[:160]}"
for url in ("http://host.openshell.internal:'"$PORT"'/", "https://www.python.org/", "http://host.openshell.internal:6379/",
            "https://example.com/"):
    print(f"{url:42} -> {get(url)}")
for path in ("/var/tmp/tracehold_probe", "/tmp/tracehold_probe"):
    try:
        open(path, "w").write("x"); print(f"write {path:26} -> allowed")
    except Exception as e:
        print(f"write {path:26} -> {type(e).__name__}")
print("user:", os.getuid(), "| python:", os.path.realpath(sys.executable), "| cwd:", os.getcwd())
print("proxy variables:", sorted(k for k in os.environ if "proxy" in k.lower()))
print("TLS variables:", {k: v for k, v in os.environ.items() if any(w in k.upper() for w in ("SSL", "CERT", "CA_BUNDLE"))})
print("environment names:", sorted(os.environ))
print("key-like variables:", sorted(k for k in os.environ if any(w in k.upper() for w in ("KEY", "SECRET", "TOKEN", "PASSWORD"))))
'
openshell sandbox exec -n "$NAME" -- python3 -c "$PROBE_PY"

echo "== sandbox log: policy decisions"
sleep 2
openshell logs "$NAME" --since 10m --source sandbox 2>&1 | grep -E "DENIED|ALLOWED|NET:|HTTP:|ERROR|WARN" | tail -30
echo "== done: the test sandbox and service are removed on exit"
