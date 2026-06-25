#!/usr/bin/env bash
# tools/db_instance.sh — drift-workflows' OWN private MariaDB test instance.
#
# drift-workflows does NOT consume a platform-provided `service:mariadb` and does NOT piggy-back on any
# other repo's instance (e.g. drift-mariadb-client's mdb114-a). MariaDB is a repo-private TEST FIXTURE:
# the gate brings it up, populates it (via Mariachi), tests against it, and tears it down. The container
# runtime is the declared `tool:docker` capability (cert surface: `tool:mariachi` + `tool:docker`); the
# Mariachi binary comes from `tool:mariachi`.
#
# A single dedicated container on a PRIVATE port distinct from the mdb114-* family, so it can never
# collide with another repo's DB on the same box.
#
# Container runtime is the declared `tool:docker` capability: the docker CLIENT is resolved from
# DOCKER_BIN (set by tools/cert-env.sh from tool:docker.bin in cert mode; defaults to `docker` on PATH).
# The preflight verifies the client; daemon liveness is checked here (`_have_docker` + the up/ready wait).
# The image is PINNED BY DIGEST so the gate is deterministic and does not depend on a moving tag.
#
# Usage: db_instance.sh {up|down|stop|rm|status|port|wait|sql <SQL>}
set -euo pipefail

DOCKER="${DOCKER_BIN:-docker}"
CONTAINER="drift-workflows-mdb"
# mariadb:11.4 pinned by digest (determinism; no run-time tag drift / network pull surprise).
IMAGE="mariadb@sha256:2f45480c9cac0545cd723ad0006d6ac28e173eeb6120b83ab31efc1a043dd325"
HOST_PORT="34214"          # private; mdb114-a/b/c use 34114/34119/34124 — we deliberately do not overlap
ROOT_PASSWORD="rootpw"
READY_TIMEOUT_SECS="${DB_READY_TIMEOUT_SECS:-90}"

_have_docker() { command -v "$DOCKER" >/dev/null 2>&1 || { echo "error: docker client '$DOCKER' not found — drift-workflows provisions its own private MariaDB and needs the tool:docker capability (or docker on PATH)" >&2; exit 1; }; "$DOCKER" info >/dev/null 2>&1 || { echo "error: docker daemon not reachable via '$DOCKER' — the tool:docker preflight checks the client; the daemon must be live for the gate" >&2; exit 1; }; }
_exists()  { "$DOCKER" inspect "$CONTAINER" >/dev/null 2>&1; }
_running() { [[ "$("$DOCKER" inspect -f '{{.State.Running}}' "$CONTAINER" 2>/dev/null || true)" == "true" ]]; }

# Readiness must verify the SAME path the gates use: a real SQL connection over the PUBLISHED HOST PORT
# (127.0.0.1:HOST_PORT). The in-container `mariadb-admin ping` is NOT sufficient — a fresh MariaDB image
# answers the local ping during initialization, then RESTARTS the server (its init sequence); during that
# window the host port forward is up but drops the TCP handshake. That exact race rejected a cert run
# (debug stress: "Lost connection to MySQL server during query" at Mariachi's first connect). So:
#   phase 1 — cheap in-container liveness (process is up);
#   phase 2 — loop on `SELECT 1` over 127.0.0.1:HOST_PORT until it actually succeeds.
# Phase 2 uses ONLY tool:docker (a throwaway client container on the host network) — no host mariadb
# client or pymysql dependency. This connects exactly like Mariachi/the tests do, so "ready" means ready.
_wait_ready() {
	local deadline=$(( SECONDS + READY_TIMEOUT_SECS ))
	# phase 1: in-container liveness (server process answering locally)
	while (( SECONDS < deadline )); do
		"$DOCKER" exec "$CONTAINER" mariadb-admin ping -uroot -p"$ROOT_PASSWORD" --silent >/dev/null 2>&1 && break
		sleep 1
	done
	# phase 2: REAL readiness over the published host port (the path every gate/Mariachi uses)
	while (( SECONDS < deadline )); do
		"$DOCKER" run --rm --network host --entrypoint mariadb "$IMAGE" \
			-h 127.0.0.1 -P "$HOST_PORT" -uroot -p"$ROOT_PASSWORD" --skip-ssl \
			--connect-timeout=3 -e "SELECT 1" >/dev/null 2>&1 && return 0
		sleep 1
	done
	echo "error: $CONTAINER not ready on host port 127.0.0.1:${HOST_PORT} (SELECT 1) within ${READY_TIMEOUT_SECS}s" >&2
	"$DOCKER" logs --tail 30 "$CONTAINER" >&2 2>/dev/null || true
	return 1
}

# Reports the PRE-state so a gate can restore it EXACTLY on teardown:
#   RUNNING — already up      -> leave it (no teardown)
#   STARTED — present-stopped -> we started it; restore = `stop` (back to stopped, keep the container)
#   CREATED — absent          -> we created it; restore = `down` (stop + remove, back to absent)
cmd_up() {
	_have_docker
	# Even an already-RUNNING container may still be mid-init (nested gate / a just-started instance) —
	# verify host-port readiness before reporting it usable.
	if _running; then _wait_ready; echo "RUNNING"; return 0; fi
	local state
	if _exists; then
		"$DOCKER" start "$CONTAINER" >/dev/null
		state="STARTED"
	else
		"$DOCKER" run -d --name "$CONTAINER" \
			-p "${HOST_PORT}:3306" \
			-e MARIADB_ROOT_PASSWORD="$ROOT_PASSWORD" \
			--health-cmd="mariadb-admin ping -uroot -p${ROOT_PASSWORD} --silent" \
			--health-interval=2s --health-timeout=3s --health-retries=30 \
			"$IMAGE" --bind-address=0.0.0.0 >/dev/null
		state="CREATED"
	fi
	_wait_ready
	echo "$state"
}

cmd_down() { _have_docker; if _exists; then "$DOCKER" stop "$CONTAINER" >/dev/null && "$DOCKER" rm "$CONTAINER" >/dev/null; echo "$CONTAINER stopped + removed" >&2; else echo "$CONTAINER not present" >&2; fi; }
cmd_stop() { _have_docker; if _running; then "$DOCKER" stop "$CONTAINER" >/dev/null; echo "$CONTAINER stopped (kept)" >&2; else echo "$CONTAINER not running" >&2; fi; }
cmd_rm()   { cmd_down; }
cmd_status() {
	_have_docker
	if _running; then echo "running  127.0.0.1:${HOST_PORT} ($CONTAINER)";
	elif _exists;  then echo "stopped  ($CONTAINER)";
	else                echo "absent   ($CONTAINER)"; fi
}
cmd_port() { echo "$HOST_PORT"; }
cmd_wait() { _have_docker; _running || { echo "error: $CONTAINER not running" >&2; exit 1; }; _wait_ready; }
cmd_sql()  { _have_docker; _running || { echo "error: $CONTAINER not running (run 'db_instance.sh up' first)" >&2; exit 1; }; "$DOCKER" exec -i "$CONTAINER" mariadb -uroot -p"$ROOT_PASSWORD" -e "$1"; }

case "${1:-}" in
	up)     cmd_up ;;
	down)   cmd_down ;;
	stop)   cmd_stop ;;
	rm)     cmd_rm ;;
	status) cmd_status ;;
	port)   cmd_port ;;
	wait)   cmd_wait ;;
	sql)    shift; cmd_sql "${1:?usage: db_instance.sh sql <SQL>}" ;;
	*) echo "usage: db_instance.sh {up|down|stop|rm|status|port|wait|sql <SQL>}" >&2; exit 2 ;;
esac
