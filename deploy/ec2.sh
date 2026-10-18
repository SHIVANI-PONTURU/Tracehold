#!/usr/bin/env bash
# TraceHold on the EC2 server: start, stop and check the whole stack. Run on the server from ~/TraceHold.
#
#   bash deploy/ec2.sh build-graph   once on a new server without a snapshot: build the graph from IBM's three files
#                                (in this folder) and save a snapshot (about 25 minutes)
#   bash deploy/ec2.sh start     FalkorDB (restores the graph the first time), mock bank, tool server, UI, then the
#                                three agents, each in its own OpenShell sandbox
#   bash deploy/ec2.sh enable-boot   start everything at every boot (systemd user units deploy/tracehold.service for
#                                the stack and deploy/tracehold-agents.service for the sandboxes; their halves of
#                                start are the commands stack and agents)
#   bash deploy/ec2.sh status    what is running
#   bash deploy/ec2.sh check     the sandbox checks: blocked and logged requests, no key inside (sandbox/check_output.txt)
#   bash deploy/ec2.sh stop      the sandboxes first, then the rest; FalkorDB keeps its data on disk
#   bash deploy/ec2.sh update    after pushing new code: stop, rebuild the agent image (the sandboxes run a copy of
#                                the agents' code), refresh the sandbox policies, start again. Never while a run waits
#                                at the gate unless you accept that the gate is replayed from the stored handoffs.
#
# The UI listens on 127.0.0.1:8501 only. The public entrance is Caddy on ports 80 and 443 (deploy/Caddyfile), here
# https://tracehold.duckdns.org (fallback name https://32-188-216-174.sslip.io). For development, a tunnel from the
# PC still works (http://localhost:8501):
#   ssh -i <key.pem> -N -L 8501:127.0.0.1:8501 ubuntu@<server address>
# One-time setup before the first start: bash deploy/ec2_setup.sh
set -euo pipefail
cd "$(dirname "$0")/.."
export TRACEHOLD_UI_ADDRESS=127.0.0.1
export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"

say() { printf '%s\n' "$*"; }

providers_ready() {
  for p in tracehold-bedrock tracehold-langfuse; do
    openshell provider list 2>/dev/null | grep -qw -- "$p" || return 1
  done
}

gateway_up() {
  # At boot the gateway may still be starting; asking it for providers too early fails with "connection refused".
  for _ in $(seq 60); do openshell status >/dev/null 2>&1 && return 0; sleep 2; done
  say "ERROR: the OpenShell gateway is not answering after 2 minutes (systemctl --user status openshell-gateway)"
  return 1
}

case "${1:-status}" in
  build-graph)
    export TRACEHOLD_WORK_DIR="${TRACEHOLD_LINUX_WORK_DIR:-$HOME/tracehold-work}"
    PY="${TRACEHOLD_VENV:-$HOME/.venvs/tracehold}/bin/python"
    [ -x "$PY" ] || { say "run bash deploy/ec2_setup.sh first"; exit 1; }
    bash deploy/falkordb.sh start
    "$PY" -m data.pipeline all        # check and clean IBM's data, add the synthetic overlay, build the test set
    "$PY" -m data.pipeline graph      # load the graph into FalkorDB and save a snapshot
    say "Graph built. Next: bash deploy/ec2.sh start"
    ;;
  enable-boot)
    units=(tracehold.service tracehold-agents.service)
    systemctl --user disable "${units[@]}" >/dev/null 2>&1 || true   # drop links made by older copies of the units
    mkdir -p "$HOME/.config/systemd/user"
    for u in "${units[@]}"; do cp "deploy/$u" "$HOME/.config/systemd/user/$u"; done
    loginctl show-user "$(id -un)" -p Linger | grep -q yes || sudo loginctl enable-linger "$(id -un)"
    systemctl --user daemon-reload
    systemctl --user enable "${units[@]}"
    say "The stack and the agents now start at every boot (systemctl --user status tracehold tracehold-agents)."
    ;;
  stack)
    bash deploy/wsl_stack.sh start
    ;;
  agents)
    gateway_up
    providers_ready || bash deploy/sandboxes.sh providers
    bash deploy/sandboxes.sh start
    ;;
  start)
    bash deploy/wsl_stack.sh start
    gateway_up
    providers_ready || bash deploy/sandboxes.sh providers
    bash deploy/sandboxes.sh start
    say "All started. UI: the public URL (Caddy), or http://localhost:8501 through a tunnel"
    ;;
  status)
    bash deploy/wsl_stack.sh status
    bash deploy/sandboxes.sh status
    free -h | sed -n '1,2p'
    ;;
  check)
    bash deploy/sandboxes.sh check
    ;;
  stop)
    bash deploy/sandboxes.sh stop
    bash deploy/wsl_stack.sh stop
    say "Stopped. You can now stop the instance in the EC2 console (Instance state > Stop instance)."
    ;;
  update)
    bash deploy/sandboxes.sh stop || true
    bash deploy/wsl_stack.sh stop || true
    bash deploy/wsl_stack.sh setup                # packages from the lock file (no change when nothing is new)
    bash deploy/sandboxes.sh build
    bash deploy/wsl_stack.sh start
    gateway_up
    providers_ready || bash deploy/sandboxes.sh providers
    bash deploy/sandboxes.sh start                # each sandbox is created again with the current policy file
    say "Updated and started. UI: the public URL (Caddy), or http://localhost:8501 through a tunnel"
    ;;
  *)
    say "usage: bash deploy/ec2.sh build-graph|start|stack|agents|status|check|stop|update|enable-boot"; exit 1 ;;
esac
