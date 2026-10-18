#!/usr/bin/env bash
# TraceHold: copy the project from this PC to the EC2 server. Run on the PC in Git Bash, from the TraceHold folder.
#
#   bash deploy/ec2_push.sh <server address> <key.pem>          the code and the server's .env (after every change)
#   bash deploy/ec2_push.sh <server address> <key.pem> --data   also the graph snapshot and the spend ledger (once, ~450 MB)
#
# Code goes to ~/TraceHold on the server, data to ~/tracehold-work. IBM's raw CSV files and the plan PDFs stay on
# the PC (the snapshot holds everything the graph is restored from). The server's .env is made from this PC's .env
# without the AWS access key lines (the model needs only BEDROCK_API_KEY) and with the server's work folder; it is
# sent over SSH with owner-only permissions and never printed.
set -euo pipefail
[ $# -ge 2 ] || { echo "usage: bash deploy/ec2_push.sh <server address> <key.pem> [--data]"; exit 1; }
HOST="$1"; KEY="$2"; DATA="${3:-}"
cd "$(dirname "$0")/.."
SSH=(ssh -i "$KEY" -o StrictHostKeyChecking=accept-new -o ServerAliveInterval=30 "ubuntu@$HOST")

items=(.env.example)
for f in *; do
  case "$f" in HI-Small_*) ;; *) items+=("$f") ;; esac
done
echo "== code -> ubuntu@$HOST:~/TraceHold"
# sandbox/*_output.txt are written on the server by the probe and the checks; never overwrite them from here.
tar -czf - --exclude='__pycache__' --exclude='.pytest_cache' --exclude='.ipynb_checkpoints' --exclude='node_modules' --exclude='*.pdf' \
    --exclude='sandbox/probe_output.txt' --exclude='sandbox/check_output.txt' "${items[@]}" \
  | "${SSH[@]}" 'mkdir -p ~/TraceHold && tar -xzf - -C ~/TraceHold'

echo "== server .env (without AWS access keys; work folder ~/tracehold-work)"
tr -d '\r' < .env \
  | grep -vE '^[[:space:]]*#?[[:space:]]*(AWS_ACCESS_KEY_ID|AWS_SECRET_ACCESS_KEY|AWS_SESSION_TOKEN)=' \
  | sed 's|^TRACEHOLD_WORK_DIR=.*|TRACEHOLD_WORK_DIR=/home/ubuntu/tracehold-work|' \
  | "${SSH[@]}" 'umask 077 && cat > ~/TraceHold/.env'

if [ "$DATA" = "--data" ]; then
  work="$(tr -d '\r' < .env | sed -n 's/^TRACEHOLD_WORK_DIR=//p' | tail -1)"
  work="$(cygpath -u "$work" 2>/dev/null || printf '%s' "$work")"
  [ -d "$work/snapshot" ] || { echo "no snapshot in $work"; exit 1; }
  echo "== snapshot and spend ledger from $work -> ~/tracehold-work (about 450 MB, a few minutes)"
  extra=(); [ -f "$work/spend.sqlite" ] && extra+=(spend.sqlite); [ -d "$work/reports" ] && extra+=(reports)
  tar -cf - -C "$work" snapshot "${extra[@]}" | "${SSH[@]}" 'mkdir -p ~/tracehold-work && tar -xf - -C ~/tracehold-work'
fi
echo "Done."
