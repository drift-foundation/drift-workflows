# tools/cert-env.sh — resolve the environment for drift-workflows' DB-backed gates.
#
# SOURCE this (do not execute) at the top of any recipe that provisions/loads/tests the DB:
#     source "<rel>/tools/cert-env.sh"
# It sets, in the current shell: MARIACHI_BIN DOCKER_BIN DB_INSTANCE_SH DB_HOST DB_PORT DB_USER
# MDB_ROOT_PWD DB_LOCK DB_HELD_GROUP.
#
# Capability model (build-orchestrator docs/certification-onboarding.md): the ONLY platform-injected
# capability env var is DRIFT_CERT_CAPABILITIES, pointing at a capabilities.json. drift-workflows declares
# requires:["tool:mariachi","tool:docker"] — two TOOLS the platform provides: the schema tool and the
# container runtime. MariaDB itself is NOT a platform service: it is a repo-PRIVATE Docker fixture
# (tools/db_instance.sh) the gate brings up (and tears down) itself, on a repo-owned port with a
# repo-owned root password. So there is NO service:mariadb, NO injected DB endpoint, and NO injected
# secret — DB_HOST/DB_PORT/DB_USER/MDB_ROOT_PWD are our own constants, set here in BOTH modes.
#
#   - DRIFT_CERT_CAPABILITIES SET   -> CERT mode: resolve tool:mariachi.bin + tool:docker.bin from the
#     document. A missing capability/bin FAILS the gate early (no silent fallback). No jq — python3.
#   - DRIFT_CERT_CAPABILITIES UNSET -> LOCAL dev mode: MARIACHI_BIN defaults to ../mariachi/.venv/bin/
#     mariachi, DOCKER_BIN defaults to `docker` on PATH (overrides honored).

_cert_root="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")/.." && pwd)"

# Repo-PRIVATE MariaDB fixture — tools/db_instance.sh owns the container + port. These are OURS: not a
# platform capability, not an injected secret. db-up/db-down + the gate's schema-load use DB_INSTANCE_SH.
export DB_INSTANCE_SH="${_cert_root}/tools/db_instance.sh"
export DB_HOST="127.0.0.1"
export DB_PORT="34214"        # MUST match tools/db_instance.sh HOST_PORT
export DB_USER="root"
export MDB_ROOT_PWD="rootpw"  # our private container's baked root password — repo-owned, not a secret

# Repo-owned DB serialization for our own concurrent gate runs against the private instance. The
# executor's per-job key ($DB_HELD_GROUP) is distinct so it can't deadlock the outer hold (flocker is
# not re-entrant).
export DB_LOCK="${DB_LOCK:-drift-workflows-mdb}"
export DB_HELD_GROUP="${DB_HELD_GROUP:-${DB_LOCK}-held}"

if [[ -n "${DRIFT_CERT_CAPABILITIES:-}" ]]; then
	# CERT mode: resolve the two platform tool capabilities — tool:mariachi (schema tool) and tool:docker
	# (container runtime for our private DB). python3 prints `export` lines (or exits non-zero with a clear
	# capability error). A missing capability/bin fails the gate here, not deep in the first schema setup.
	_caps_exports="$(python3 - "$DRIFT_CERT_CAPABILITIES" <<'PY'
import json, shlex, sys
path = sys.argv[1]
try:
    doc = json.load(open(path))
except Exception as e:
    sys.exit(f"cert-env: cannot read DRIFT_CERT_CAPABILITIES ({path}): {e}")
caps = doc.get("capabilities") or {}
def need_bin(cap_id):
    cap = caps.get(cap_id)
    if not cap:
        sys.exit(f"cert-env: required capability '{cap_id}' not provided in {path} "
                 f"(requires:['tool:mariachi','tool:docker'] — provision it on the cert host)")
    b = cap.get("bin")
    if b in (None, ""):
        sys.exit(f"cert-env: '{cap_id}.bin' missing/empty in {path}")
    return b
print("export MARIACHI_BIN=" + shlex.quote(need_bin("tool:mariachi")))
print("export DOCKER_BIN="   + shlex.quote(need_bin("tool:docker")))
PY
	)" || { echo "cert-env: capability resolution failed (DRIFT_CERT_CAPABILITIES=$DRIFT_CERT_CAPABILITIES)" >&2; return 1 2>/dev/null || exit 1; }
	eval "$_caps_exports"; unset _caps_exports
else
	# LOCAL dev mode: defaults, overrides honored.
	export MARIACHI_BIN="${MARIACHI_BIN:-${_cert_root}/../mariachi/.venv/bin/mariachi}"
	export DOCKER_BIN="${DOCKER_BIN:-docker}"
fi
unset _cert_root
