#!/usr/bin/env bash
# TraceHold, Phase 2 steps 11-12: the Tracer, Adjudicator and Verifier, each in its own OpenShell sandbox (Podman
# driver, rootless), on this Linux or WSL machine. The rest of the stack runs beside them (deploy/wsl_stack.sh).
#
#   bash deploy/sandboxes.sh build       build the agent image (localhost/tracehold-agent:dev)
#   bash deploy/sandboxes.sh providers   import the two provider profiles and store the keys from .env in the gateway
#   bash deploy/sandboxes.sh start       one sandbox per agent, each running its worker
#   bash deploy/sandboxes.sh check       the step 11 and 12 checks, from inside the Tracer sandbox
#   bash deploy/sandboxes.sh status      the sandboxes and the last lines of each worker's log
#   bash deploy/sandboxes.sh logs <agent>   the sandbox's policy log (allowed and denied requests)
#   bash deploy/sandboxes.sh stop        delete the three sandboxes
#
# Keys: .env is read here, outside the sandboxes, and the keys go to the OpenShell gateway as provider credentials.
# A sandbox sees only placeholders; OpenShell's proxy puts the real values into requests to the model route and to
# Langfuse, and nowhere else (sandbox/profiles/). Policies: sandbox/policies/<agent>.yaml (plan Appendix I).
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
IMAGE="${TRACEHOLD_AGENT_IMAGE:-localhost/tracehold-agent:dev}"
AGENTS=(tracer adjudicator verifier)
WORK="${TRACEHOLD_LINUX_WORK_DIR:-$HOME/tracehold-work}"
LOGS="$WORK/logs"
GATEWAY_TOML="$HOME/.config/openshell/gateway.toml"
cd "$ROOT"

say() { printf '%s\n' "$*"; }
die() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }

remove_sandbox() {
  # Delete a sandbox and wait until it is gone: deletion finishes in the background, and creating the same name
  # before then fails with "already exists" (seen after an instance restart left sandboxes in the Error phase).
  # After a reboot the gateway brings back each sandbox's record, and a deletion can hang on its dead containers
  # (seen 18 Oct, "Exited 292 years ago"); after 20 s those containers are removed with podman, which lets it finish.
  local name="$1" i id
  openshell sandbox delete "$name" >/dev/null 2>&1 || return 0
  for i in $(seq 90); do
    openshell sandbox list 2>/dev/null | awk 'NR > 1 {print $1}' | grep -qx -- "$name" || { say "$name deleted"; return 0; }
    if [ "$i" = 20 ]; then
      for id in $(podman ps -a --format '{{.Names}}' | sed -n "s/^openshell-default--$name-//p"); do
        say "$name: removing its stuck containers ($id)"
        podman rm -f "openshell-default--$name-$id" "openshell-supervisor-$id" >/dev/null 2>&1 || true
      done
    fi
    sleep 1
  done
  die "$name was not deleted within 90 s (openshell sandbox list)"
}

read_env() {
  # .env into this shell only (never printed); Windows line endings removed.
  [ -f .env ] || die "no .env in $ROOT"
  set -a
  # shellcheck disable=SC1090
  . <(tr -d '\r' < .env | grep -E '^[A-Za-z_][A-Za-z0-9_]*=')
  set +a
}

gateway_ip_current() {
  # On EC2, host.openshell.internal must resolve to this server's private address (deploy/ec2_setup.sh writes it).
  # A server launched from an AMI gets a new private address; with the old one left in the file the sandboxes
  # time out reaching the tool server (18 Oct). Not on EC2 (no instance metadata, e.g. WSL): leave the file alone.
  grep -q '^host_gateway_ip' "$GATEWAY_TOML" 2>/dev/null || return 0
  local token ip have
  token="$(curl -fsS -m 2 -X PUT http://169.254.169.254/latest/api/token \
             -H 'X-aws-ec2-metadata-token-ttl-seconds: 60' 2>/dev/null || true)"
  ip="$(curl -fsS -m 2 -H "X-aws-ec2-metadata-token: $token" \
          http://169.254.169.254/latest/meta-data/local-ipv4 2>/dev/null || true)"
  [ -n "$ip" ] || return 0
  have="$(sed -n 's/^host_gateway_ip = "\(.*\)"/\1/p' "$GATEWAY_TOML")"
  [ "$have" = "$ip" ] && return 0
  say "== gateway: host_gateway_ip $have -> $ip (this server's private address changed)"
  cp "$GATEWAY_TOML" "$GATEWAY_TOML.bak.$(date +%s)"
  sed -i "s/^host_gateway_ip = .*/host_gateway_ip = \"$ip\"/" "$GATEWAY_TOML"
  openshell-gateway config preflight --path "$GATEWAY_TOML"
  systemctl --user restart openshell-gateway
}

gateway_ready() {
  # The Podman driver must use the locally built image instead of pulling it from a registry.
  if ! grep -q '^\[openshell.drivers.podman\]' "$GATEWAY_TOML" 2>/dev/null; then
    say "== gateway: Podman driver uses local images first ($GATEWAY_TOML)"
    printf '\n[openshell.drivers.podman]\nimage_pull_policy = "if_not_present"\n' >> "$GATEWAY_TOML"
    openshell-gateway config preflight --path "$GATEWAY_TOML"
    systemctl --user restart openshell-gateway
  fi
  gateway_ip_current
  for _ in $(seq 30); do openshell status >/dev/null 2>&1 && return 0; sleep 1; done
  die "the OpenShell gateway is not connected (systemctl --user status openshell-gateway)"
}

build() {
  local b="$HOME/.cache/tracehold/agent-image"
  rm -rf "$b"; mkdir -p "$b/code/handoff" "$b/code/mcp_server" "$b/code/data"
  cp sandbox/Containerfile.agents sandbox/requirements.agents.txt "$b/"
  sed '1s/^\xEF\xBB\xBF//' requirements.lock.txt | tr -d '\r' | grep -viE '^(pywin32|pywinpty)==' > "$b/requirements.lock.linux.txt"
  cp -r agents "$b/code/"
  cp handoff/__init__.py handoff/contracts.py "$b/code/handoff/"
  cp mcp_server/__init__.py mcp_server/signals.py "$b/code/mcp_server/"
  cp data/__init__.py data/settings.py "$b/code/data/"
  find "$b/code" -name __pycache__ -prune -exec rm -rf {} +
  say "== building $IMAGE (code: agents/, handoff/contracts.py, mcp_server/signals.py, data/settings.py)"
  podman build -t "$IMAGE" -f "$b/Containerfile.agents" "$b"
  podman run --rm "$IMAGE" python -c "import agents.worker, agents.tracer.graph, agents.adjudicator.graph, agents.verifier.graph, langfuse.langchain; print('agent image imports OK')"
}

providers() {
  read_env
  for v in BEDROCK_API_KEY LANGFUSE_PUBLIC_KEY LANGFUSE_SECRET_KEY LANGFUSE_HOST; do
    if [ -z "${!v:-}" ]; then
      [ "$v" = BEDROCK_API_KEY ] && die "BEDROCK_API_KEY is not in .env. The sandboxes use the Bedrock API key (AWS console, US East (Ohio): Bedrock > API keys); add it as BEDROCK_API_KEY=... and run this again"
      die "$v is empty in .env"
    fi
  done
  local lf tmp
  lf="${LANGFUSE_HOST#*://}"; lf="${lf%%/*}"
  tmp="$(mktemp -d)"
  cp sandbox/profiles/*.yaml "$tmp/"
  sed -i "s/host: cloud.langfuse.com/host: $lf/" "$tmp/tracehold-langfuse.yaml"
  # Start clean every time, so this can be run again: providers first (they use the profiles), then the profiles
  # (lint refuses an ID that already exists, and an update needs the gateway's resource version).
  openshell provider delete tracehold-bedrock >/dev/null 2>&1 || true
  openshell provider delete tracehold-langfuse >/dev/null 2>&1 || true
  for p in tracehold-bedrock tracehold-langfuse; do
    say "== profile $p"
    openshell profile delete "$p" >/dev/null 2>&1 || true
    openshell profile lint -f "$tmp/$p.yaml"
    openshell profile import -f "$tmp/$p.yaml"
  done
  rm -rf "$tmp"
  say "== providers (the keys go to the gateway; nothing is printed)"
  openshell provider create --name tracehold-bedrock --type tracehold-bedrock --credential BEDROCK_API_KEY
  openshell provider create --name tracehold-langfuse --type tracehold-langfuse \
    --credential LANGFUSE_PUBLIC_KEY --credential LANGFUSE_SECRET_KEY
}

start() {
  read_env
  gateway_ready
  podman image exists "$IMAGE" || die "no agent image; run: bash deploy/sandboxes.sh build"
  for p in tracehold-bedrock tracehold-langfuse; do
    openshell provider list 2>/dev/null | grep -qw -- "$p" || die "provider $p is missing; run: bash deploy/sandboxes.sh providers"
  done
  curl -fsS -o /dev/null http://127.0.0.1:8000/health || die "the tool server is not running; run: bash deploy/wsl_stack.sh start"
  mkdir -p "$LOGS"
  # Non-secret settings only. `sandbox create` takes no environment flag, so /usr/bin/env sets them and then
  # becomes the worker (exec), and the agent's Python stays the only process the policy rules allow.
  local settings=("TRACEHOLD_TOOL_SERVER_URL=http://host.openshell.internal:8000"
                  "TRACEHOLD_BANK_ID=${TRACEHOLD_BANK_ID:-11}"
                  "TRACEHOLD_BANK_IDS=${TRACEHOLD_BANK_IDS:-11,20,12}"
                  "LANGFUSE_HOST=$LANGFUSE_HOST"
                  "TRACEHOLD_WORK_DIR=/sandbox" "TMPDIR=/sandbox" "HOME=/sandbox"
                  "VERIFIER_CHECKPOINT_DB=/sandbox/verifier_checkpoints.sqlite")
  [ -n "${TRACEHOLD_MODEL_ID:-}" ] && settings+=("TRACEHOLD_MODEL_ID=$TRACEHOLD_MODEL_ID")
  for a in "${AGENTS[@]}"; do
    local name="th-$a"
    remove_sandbox "$name"
    nohup openshell sandbox create --name "$name" --from "$IMAGE" --policy "sandbox/policies/$a.yaml" \
      --no-auto-providers --provider tracehold-bedrock --provider tracehold-langfuse --no-tty \
      -- /usr/bin/env "${settings[@]}" python -m agents.worker "$a" >"$LOGS/$a.sandbox.log" 2>&1 &
    echo $! >"$LOGS/$a.sandbox.pid"
    say "$name starting; worker log $LOGS/$a.sandbox.log"
  done
  sleep 20
  status
}

status() {
  openshell sandbox list || true
  for a in "${AGENTS[@]}"; do
    say "--- $a (last lines of $LOGS/$a.sandbox.log)"
    tail -n 5 "$LOGS/$a.sandbox.log" 2>/dev/null || say "(no log yet)"
  done
}

logs() {
  local a="${1:-tracer}"
  openshell logs "th-$a" --since 30m --source sandbox
}

check() {
  # Plan step 11: from the Tracer sandbox, FalkorDB's port and a public website are blocked and logged, while its
  # MCP path works. Step 12: the environment inside a sandbox shows no Bedrock key.
  read_env
  local out=sandbox/check_output.txt real_hash
  real_hash="$(printf '%s' "${BEDROCK_API_KEY:-}" | sha256sum | cut -c1-64)"
  {
    say "== TraceHold sandbox checks, $(date -u +%Y-%m-%dT%H:%M:%SZ), OpenShell $(openshell --version | awk '{print $2}')"
    openshell sandbox exec -n th-tracer -- python -c '
import hashlib, json, os, socket, sys, urllib.request, urllib.error
real = sys.argv[1]
def call(method, url, body=None):
    req = urllib.request.Request(url, method=method, data=json.dumps(body).encode() if body is not None else None,
                                 headers={"content-type": "application/json"})
    try:
        r = urllib.request.urlopen(req, timeout=20)
        return f"{r.status} allowed"
    except urllib.error.HTTPError as e:
        txt = e.read()[:200].decode("utf-8", "replace")
        return (f"HTTP {e.code} blocked by policy" if "policy_denied" in txt
                else f"allowed: reached the tool server, which answered HTTP {e.code} ({txt[:70]})")
    except Exception as e:
        return f"blocked ({type(e).__name__}: {str(e)[:120]})"
H = "http://host.openshell.internal"
rows = [
    ("its handoff API (spend read)",         "GET",  f"{H}:8000/handoff/spend"),
    ("its MCP path /mcp/tracer/",           "POST", f"{H}:8000/mcp/tracer/"),
    ("another agent MCP path /mcp/verifier/", "POST", f"{H}:8000/mcp/verifier/"),
    ("the gate decision API",               "POST", f"{H}:8000/handoff/gate/decide"),
    ("FalkorDB port 6379",                  "GET",  f"{H}:6379/"),
    ("the mock bank port 8100",             "GET",  f"{H}:8100/health"),
    ("a public website",                    "GET",  "https://example.com/"),
]
for label, m, url in rows:
    print(f"{label:40} {m:4} {url:48} -> {call(m, url, {} if m == "POST" else None)}")
try:
    socket.create_connection(("host.openshell.internal", 6379), timeout=5).close(); print("raw TCP to FalkorDB 6379: connected (BAD)")
except Exception as e:
    print(f"raw TCP to FalkorDB 6379: blocked ({type(e).__name__})")
v = os.environ.get("BEDROCK_API_KEY", "")
print("BEDROCK_API_KEY inside the sandbox:", "absent" if not v else
      ("the REAL key (BAD)" if hashlib.sha256(v.encode()).hexdigest() == real else f"a placeholder ({len(v)} characters)"))
print("environment names with KEY or SECRET:", sorted(k for k in os.environ if "KEY" in k or "SECRET" in k))
' "$real_hash"
    say "== policy log of the Tracer sandbox (denied requests)"
    sleep 5                                     # the supervisor writes its log a moment after the requests
    openshell logs th-tracer --since 10m --source sandbox 2>&1 | grep -E 'OCSF.*DENIED' | tail -12 || true
  } 2>&1 | tee "$out"
  say "Saved to $out"
}

stop() {
  for a in "${AGENTS[@]}"; do
    remove_sandbox "th-$a"
    [ -f "$LOGS/$a.sandbox.pid" ] && kill "$(cat "$LOGS/$a.sandbox.pid")" 2>/dev/null || true
    rm -f "$LOGS/$a.sandbox.pid"
  done
}

case "${1:-status}" in
  build) build ;;
  providers) providers ;;
  start) start ;;
  check) check ;;
  status) status ;;
  logs) logs "${2:-tracer}" ;;
  stop) stop ;;
  *) die "usage: bash deploy/sandboxes.sh build|providers|start|check|status|logs <agent>|stop" ;;
esac
