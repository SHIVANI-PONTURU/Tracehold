#!/usr/bin/env bash
# TraceHold on an EC2 server (Ubuntu 26.04 LTS, us-west-2; Bedrock is called in us-east-2): one-time setup, safe to
# run again.
# Run on the server as the ubuntu user, from ~/TraceHold, after deploy/ec2_push.sh has copied the code, the
# snapshot and the server's .env from the PC:
#
#   bash deploy/ec2_setup.sh
#
# 1. System packages: rootless Podman 5 and its user socket, linger (so user services run without a login).
# 2. The kernel features the sandboxes need: Landlock and cgroups v2 (OpenShell refuses to start without them).
# 3. NVIDIA OpenShell 0.1.4: the gateway on the Podman driver, telemetry off, and host.openshell.internal pointed at
#    this server's private address, so sandboxes reach the tool server there (never loopback, never FalkorDB).
# 4. The Python 3.13 environment and packages (deploy/wsl_stack.sh setup), and the agent image
#    (deploy/sandboxes.sh build).
# Afterwards: bash deploy/sandbox_probe.sh (once), then bash deploy/ec2.sh start.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
OPENSHELL_WANT="0.1.4"
CFG_DIR="$HOME/.config/openshell"
cd "$ROOT"

say() { printf '%s\n' "$*"; }
die() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }

[ "$(id -u)" != 0 ] || die "run as the ubuntu user, not root"
[ -f .env ] || die "no .env here; from the PC run: bash deploy/ec2_push.sh <host> <key>"
export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"

say "== 1. system packages"
sudo apt-get update -q
sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -q podman uidmap passt slirp4netns curl ca-certificates python3
podman_major="$(podman version --format '{{.Client.Version}}' | cut -d. -f1)"
[ "$podman_major" -ge 5 ] || die "OpenShell needs Podman 5.x; this server has $(podman --version)"
if ! grep -q "^$USER:" /etc/subuid; then
  sudo usermod --add-subuids 100000-165535 --add-subgids 100000-165535 "$USER"
fi
sudo loginctl enable-linger "$USER"
systemctl --user enable --now podman.socket
SOCKET="$XDG_RUNTIME_DIR/podman/podman.sock"
[ -S "$SOCKET" ] || die "the Podman user socket is missing ($SOCKET)"
say "Podman: $(podman --version), socket $SOCKET"

say "== 2. kernel features"
lsm="$(sudo cat /sys/kernel/security/lsm 2>/dev/null || true)"
case ",$lsm," in *,landlock,*) say "Landlock: enabled ($lsm)" ;; *) die "Landlock is not in the active LSM list ($lsm)" ;; esac
[ "$(stat -fc %T /sys/fs/cgroup)" = cgroup2fs ] || die "cgroups v2 is not mounted at /sys/fs/cgroup"
say "cgroups v2: yes; kernel $(uname -r)"
say "unprivileged user namespaces restricted by AppArmor: $(sysctl -n kernel.apparmor_restrict_unprivileged_userns 2>/dev/null || echo n/a)"

say "== 3. OpenShell $OPENSHELL_WANT"
have="$(openshell --version 2>/dev/null | awk '{print $2}' || true)"
if [ "$have" != "$OPENSHELL_WANT" ]; then
  curl -LsSf https://raw.githubusercontent.com/NVIDIA/OpenShell/main/install.sh | OPENSHELL_VERSION="v$OPENSHELL_WANT" sh \
    || curl -LsSf https://raw.githubusercontent.com/NVIDIA/OpenShell/main/install.sh | sh
  hash -r
  have="$(openshell --version 2>/dev/null | awk '{print $2}' || true)"
fi
[ -n "$have" ] || die "openshell is not on PATH after the install"
[ "$have" = "$OPENSHELL_WANT" ] || say "WARNING: OpenShell $have installed, the policies were written for $OPENSHELL_WANT"

# This server's private address: what host.openshell.internal resolves to inside a sandbox. The tool server
# listens there (deploy/wsl_stack.sh); FalkorDB and the mock bank stay on 127.0.0.1, out of the sandboxes' reach.
token="$(curl -fsS -X PUT http://169.254.169.254/latest/api/token -H 'X-aws-ec2-metadata-token-ttl-seconds: 60' || true)"
PRIVATE_IP="$(curl -fsS -H "X-aws-ec2-metadata-token: $token" http://169.254.169.254/latest/meta-data/local-ipv4 2>/dev/null \
              || hostname -I | awk '{print $1}')"
[ -n "$PRIVATE_IP" ] || die "could not find this server's private address"
mkdir -p "$CFG_DIR"
[ -f "$CFG_DIR/gateway.toml" ] && cp "$CFG_DIR/gateway.toml" "$CFG_DIR/gateway.toml.bak.$(date +%s)"
cat > "$CFG_DIR/gateway.toml" <<EOF
# TraceHold: OpenShell gateway on the rootless Podman driver (written by deploy/ec2_setup.sh)
[openshell]
version = 2

[openshell.gateway]
compute_driver = "podman"

[openshell.drivers.podman]
socket_path = "$SOCKET"
image_pull_policy = "if_not_present"
host_gateway_ip = "$PRIVATE_IP"
EOF
touch "$CFG_DIR/gateway.env"
grep -q '^OPENSHELL_TELEMETRY_ENABLED=' "$CFG_DIR/gateway.env" || echo 'OPENSHELL_TELEMETRY_ENABLED=false' >> "$CFG_DIR/gateway.env"
openshell-gateway config preflight --path "$CFG_DIR/gateway.toml"
systemctl --user enable openshell-gateway >/dev/null 2>&1 || true
systemctl --user restart openshell-gateway
for _ in $(seq 30); do openshell status >/dev/null 2>&1 && break; sleep 2; done
openshell status || die "the OpenShell gateway did not connect (journalctl --user -u openshell-gateway -n 50)"
openshell gateway info 2>/dev/null | grep -i -E 'driver|version' || true
say "host.openshell.internal -> $PRIVATE_IP"

say "== 4. Python environment and the agent image"
bash deploy/wsl_stack.sh setup
bash deploy/sandboxes.sh build

say "== 5. the web app (skipped when web/dist is already built)"
if [ ! -f web/dist/index.html ]; then
  command -v npm >/dev/null || sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -q nodejs npm
  (cd web && npm ci && npm run build)
fi

say "Setup done. Next: bash deploy/ec2.sh build-graph (once, without a snapshot), then bash deploy/ec2.sh start"
