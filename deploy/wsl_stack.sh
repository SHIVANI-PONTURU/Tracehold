#!/usr/bin/env bash
# TraceHold on Linux or WSL (Phase 2 step 11 onwards): FalkorDB, the mock bank, the tool server and the UI run on
# this machine; the three agents run in OpenShell sandboxes on the same machine (deploy/sandboxes.sh).
#
#   bash deploy/wsl_stack.sh setup     once: Python 3.13 environment, packages, copy of the work folder
#   bash deploy/wsl_stack.sh start     FalkorDB (restores the snapshot the first time), mock bank, tool server, UI
#   bash deploy/wsl_stack.sh status    what is running
#   bash deploy/wsl_stack.sh stop      stops everything this script started, and FalkorDB
#
# The tool server listens on all addresses of this machine, because sandboxes reach it as host.openshell.internal
# (OpenShell never lets a sandbox reach 127.0.0.1). FalkorDB and the mock bank stay on 127.0.0.1, so no sandbox can
# reach them; the sandbox policies block those ports as well. The UI opens on http://localhost:8501 in Windows.
# On an EC2 server (deploy/ec2.sh) the same script runs with the UI on 127.0.0.1 only (TRACEHOLD_UI_ADDRESS), reached
# through Caddy on ports 80 and 443 (deploy/Caddyfile) or an SSH tunnel; the security group keeps every other port closed.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
VENV="${TRACEHOLD_VENV:-$HOME/.venvs/tracehold}"
PY="$VENV/bin/python"
export TRACEHOLD_WORK_DIR="${TRACEHOLD_LINUX_WORK_DIR:-$HOME/tracehold-work}"
LOGS="$TRACEHOLD_WORK_DIR/logs"
UI_ADDRESS="${TRACEHOLD_UI_ADDRESS:-0.0.0.0}"
SERVICES=(bank_api tool_server ui)
cd "$ROOT"

say() { printf '%s\n' "$*"; }
die() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }
is_wsl() { grep -qi microsoft /proc/sys/kernel/osrelease 2>/dev/null; }

windows_work_dir() {
  # The Windows work folder (TRACEHOLD_WORK_DIR in .env, else %USERPROFILE%\tracehold-work), as a WSL path.
  local p
  if [ -n "${TRACEHOLD_WIN_WORK_DIR:-}" ]; then printf '%s\n' "$TRACEHOLD_WIN_WORK_DIR"; return; fi
  p="$(tr -d '\r' < .env 2>/dev/null | sed -n 's/^TRACEHOLD_WORK_DIR=//p' | tail -1)"
  if [ -z "$p" ] || [ "$p" = "work" ]; then
    p="$(cmd.exe /c 'echo %USERPROFILE%' 2>/dev/null | tr -d '\r')\\tracehold-work"
  fi
  case "$p" in
    [A-Za-z]:*) wslpath -u "$p" 2>/dev/null || true ;;
    /*) printf '%s\n' "$p" ;;
    *) printf '%s\n' "$ROOT/$p" ;;
  esac
}

wait_url() {
  for _ in $(seq "$2"); do
    if curl -fsS -o /dev/null "$1" 2>/dev/null; then return 0; fi
    sleep 1
  done
  return 1
}

setup() {
  command -v podman >/dev/null || die "Podman is not installed (sudo apt install -y podman)"
  if ! command -v uv >/dev/null && [ ! -x "$HOME/.local/bin/uv" ]; then
    say "== installing uv (Python manager)"
    curl -LsSf https://astral.sh/uv/install.sh | sh
  fi
  export PATH="$HOME/.local/bin:$PATH"
  if [ ! -x "$PY" ]; then
    say "== Python 3.13 environment in $VENV"
    uv venv --python 3.13 "$VENV"
  fi
  say "== packages (requirements.lock.txt, without the Windows-only ones)"
  local lock
  lock="$(mktemp)"
  sed '1s/^\xEF\xBB\xBF//' requirements.lock.txt | tr -d '\r' | grep -viE '^(pywin32|pywinpty)==' > "$lock"
  uv pip install --python "$PY" -r "$lock"
  rm -f "$lock"

  mkdir -p "$TRACEHOLD_WORK_DIR"
  if [ ! -d "$TRACEHOLD_WORK_DIR/snapshot" ]; then
    local win=""
    is_wsl && win="$(windows_work_dir)"
    if [ -n "$win" ] && [ -d "$win/snapshot" ]; then
      say "== copying the work folder from $win (about 1 GB, a few minutes)"
      (cd "$win" && tar --exclude=./logs -cf - .) | (cd "$TRACEHOLD_WORK_DIR" && tar -xf -)
    else
      say "== no graph snapshot in $TRACEHOLD_WORK_DIR yet: build the graph once with bash deploy/ec2.sh build-graph"
      say "   (or copy a snapshot from another machine: bash deploy/ec2_push.sh <host> <key> --data)"
    fi
  fi

  # Rootless containers need / mounted as shared; WSL forgets this at every start, so set it at boot.
  if is_wsl && ! grep -q 'make-rshared' /etc/wsl.conf 2>/dev/null; then
    say "== making / a shared mount at every WSL start (/etc/wsl.conf; asks for your Ubuntu password)"
    if grep -q '^\[boot\]' /etc/wsl.conf 2>/dev/null; then
      if grep -q '^command' /etc/wsl.conf; then
        say "   /etc/wsl.conf already has a boot command; add 'mount --make-rshared /' to it yourself"
      else
        sudo sed -i '/^\[boot\]/a command = "mount --make-rshared /"' /etc/wsl.conf
      fi
    else
      printf '\n[boot]\ncommand = "mount --make-rshared /"\n' | sudo tee -a /etc/wsl.conf >/dev/null
    fi
  fi
  findmnt -no PROPAGATION / | grep -q shared || sudo mount --make-rshared /
  say "Setup done. Work folder: $TRACEHOLD_WORK_DIR. Next: bash deploy/wsl_stack.sh start"
}

running() { [ -f "$LOGS/$1.pid" ] && kill -0 "$(cat "$LOGS/$1.pid")" 2>/dev/null; }

launch() {
  local name="$1"; shift
  if running "$name"; then say "$name already running (pid $(cat "$LOGS/$name.pid"))"; return; fi
  nohup "$@" >"$LOGS/$name.log" 2>&1 &
  echo $! >"$LOGS/$name.pid"
  say "$name started (pid $!); log $LOGS/$name.log"
}

start() {
  [ -x "$PY" ] || die "run: bash deploy/wsl_stack.sh setup"
  mkdir -p "$LOGS"
  findmnt -no PROPAGATION / | grep -q shared || sudo mount --make-rshared /
  bash deploy/falkordb.sh start
  if ! podman exec falkordb redis-cli GRAPH.LIST 2>/dev/null | grep -qx network; then
    [ -d "$TRACEHOLD_WORK_DIR/snapshot" ] || die "no network graph and no snapshot in $TRACEHOLD_WORK_DIR; build it first: bash deploy/ec2.sh build-graph"
    say "== restoring the network graph from the snapshot (first start here; a few minutes)"
    "$PY" -m data.snapshot restore
  fi
  launch bank_api "$PY" -m uvicorn bank_api.app:app --host 127.0.0.1 --port 8100
  wait_url http://127.0.0.1:8100/health 30 || say "WARNING: the mock bank did not answer; see $LOGS/bank_api.log"
  launch tool_server env MCP_HOST=0.0.0.0 "$PY" -m mcp_server.server
  wait_url http://127.0.0.1:8000/health 90 || say "WARNING: the tool server did not answer; see $LOGS/tool_server.log"
  # The React UI (web/dist, built with npm run build) behind the small UI server.
  [ -f web/dist/index.html ] || say "WARNING: web/dist is missing: build it (cd web && npm ci && npm run build)"
  launch ui env UI_ADDRESS="$UI_ADDRESS" UI_PORT=8501 "$PY" -m ui_server.app
  wait_url http://127.0.0.1:8501/api/ui/health 30 || say "WARNING: the UI server did not answer; see $LOGS/ui.log"
  say "UI: http://localhost:8501. Agents: bash deploy/sandboxes.sh start"
}

status() {
  bash deploy/falkordb.sh status || true
  for s in "${SERVICES[@]}"; do
    if running "$s"; then say "$(printf '%-12s running (pid %s)' "$s" "$(cat "$LOGS/$s.pid")")"
    else say "$(printf '%-12s stopped' "$s")"; fi
  done
  curl -fsS http://127.0.0.1:8000/health 2>/dev/null && echo || say "tool server health: no answer"
}

stop() {
  for s in "${SERVICES[@]}"; do
    if running "$s"; then kill "$(cat "$LOGS/$s.pid")" && say "$s stopped"; fi
    rm -f "$LOGS/$s.pid"
  done
  bash deploy/falkordb.sh stop >/dev/null 2>&1 && say "FalkorDB stopped" || true
}

case "${1:-status}" in
  setup) setup ;;
  start) start ;;
  status) status ;;
  stop) stop ;;
  *) die "usage: bash deploy/wsl_stack.sh setup|start|status|stop" ;;
esac
