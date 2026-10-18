#!/usr/bin/env bash
# TraceHold: run the pinned FalkorDB container with Podman on Linux or EC2 (Phase 1 step 2).
# Settings come from deploy/falkordb.env.
#   bash deploy/falkordb.sh status | start | stop | logs | cli
set -euo pipefail
cd "$(dirname "$0")"
set -a; . ./falkordb.env; set +a
action="${1:-status}"
name="$FALKORDB_CONTAINER"

exists() { podman container exists "$name"; }

wait_ping() {
  for _ in $(seq 60); do
    if [ "$(podman exec "$name" redis-cli PING 2>/dev/null)" = "PONG" ]; then
      echo "FalkorDB is up on 127.0.0.1:6379 (Browser: http://localhost:3000)"; return 0
    fi
    sleep 1
  done
  echo "FalkorDB did not answer PING within 60 s; run: bash deploy/falkordb.sh logs" >&2; return 1
}

create() {
  if exists; then echo "Container '$name' already exists."; return; fi
  podman volume exists "$FALKORDB_VOLUME" || podman volume create "$FALKORDB_VOLUME" >/dev/null
  echo "Creating container '$name' from $FALKORDB_IMAGE ..."
  podman run -d --name "$name" \
    -p 127.0.0.1:6379:6379 -p 127.0.0.1:3000:3000 \
    -v "$FALKORDB_VOLUME:$FALKORDB_DATA_DIR" \
    -e "REDIS_ARGS=--appendonly yes" \
    "$FALKORDB_IMAGE"
}

case "$action" in
  create) create; wait_ping ;;
  start) if exists; then podman start "$name" >/dev/null; else create; fi; wait_ping ;;
  stop) podman stop "$name" ;;
  logs) podman logs --tail 60 "$name" ;;
  cli) podman exec -it "$name" redis-cli ;;
  status)
    if ! exists; then echo "Container: not created (run: bash deploy/falkordb.sh start)"; exit 0; fi
    echo "Container   : $name, $(podman ps -a --filter "name=^$name$" --format '{{.Status}}')"
    have="$(podman image inspect --format '{{.Digest}}' "$FALKORDB_IMAGE" 2>/dev/null || true)"
    if [ "$have" = "$FALKORDB_DIGEST" ]; then m="matches the pinned digest"; else m="DIFFERS from the pinned digest $FALKORDB_DIGEST"; fi
    echo "Image       : $FALKORDB_IMAGE, $have ($m)"
    pong="$(podman exec "$name" redis-cli PING 2>/dev/null || true)"
    echo "PING        : $pong"
    if [ "$pong" = "PONG" ]; then
      echo "Memory used : $(podman exec "$name" redis-cli INFO memory | grep used_memory_human | cut -d: -f2 | tr -d '\r')"
      echo "Graphs      : $(podman exec "$name" redis-cli GRAPH.LIST | tr '\n' ' ')"
    fi ;;
  *) echo "usage: bash deploy/falkordb.sh status|create|start|stop|logs|cli" >&2; exit 2 ;;
esac
