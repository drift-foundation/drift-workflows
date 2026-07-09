# Drift Workflows — umbrella monorepo.
#
# Components (each independently buildable + testable; versioned/signed/released ONLY from the root drift/manifest.json):
#   singular/      reusable language-neutral idempotency protocol + library
#   microflows/    typed durable workflow manager/service + runtime
# Cross-component suites:
#   integration/<suite>/   orchestration + assertions spanning >1 component
#
# The root owns AGGREGATION, ORDERING, and FAIL-FAST; components and integration
# suites own their isolated gates. Root gates run, in dependency order:
#     singular -> microflows -> each integration/<suite>
#
# Discovery rule: an integration suite is any immediate subdirectory of
# integration/ that contains a `justfile`. The root runs that suite's matching
# gate (test/perf/stress). A suite with no scenarios for a gate must provide an
# EXPLICIT no-op recipe (see integration/coordinator-singular/justfile) — the
# root never silently skips a missing gate.

HEARTBEAT_SECS := "30"
FLOCKER := env("DRIFT_TOOLCHAIN_ROOT", env("HOME") + "/opt/drift/certified/current/toolchain") + "/bin/flocker"

# --- Aggregate gates (repository-wide readiness) ---

# All gates in ONE combined plan (tools/emit_test_plan.py imports and merges
# singular's + microflows's + microflows/runner's + every integration suite's
# own build/run-unit/run-live jobs), handed to ONE drift_test_run.py
# invocation: every DB-free compile across all four sources overlaps under
# the shared flocker pool (previously three fully sequential `just test-*`
# calls, each with its own separate compile phase), and the runner binary
# (mfrunner/microflows-runner — identical sources/entry/trust-store) is built
# exactly once via the executor's `out`-dedup instead of twice.
# `test-singular`/`test-microflows`/`test-integration` below remain fully
# independent, unchanged standalone recipes for anyone testing one component.
test:
	#!/usr/bin/env bash
	set -euo pipefail
	source tools/cert-env.sh
	: "${DRIFT_TOOLCHAIN_ROOT:?set DRIFT_TOOLCHAIN_ROOT to a toolchain >= 0.33.17}"
	# Gate RESTORES ENTRY STATE exactly: bring up our private DB, then on exit (success OR failure) put it
	# back as found — created->removed, was-stopped->stopped, was-running->left. Sub-gates see it RUNNING.
	_dbstate="$("$DB_INSTANCE_SH" up)"
	case "$_dbstate" in   # restore EXACTLY: created(was absent)->remove; started(was stopped)->stop; running->leave
	  CREATED) trap '"$DB_INSTANCE_SH" down >/dev/null 2>&1 || true' EXIT ;;
	  STARTED) trap '"$DB_INSTANCE_SH" stop >/dev/null 2>&1 || true' EXIT ;;
	esac
	[[ -x "{{FLOCKER}}" ]] || { echo "error: flocker not found at {{FLOCKER}} (required for DB serialization; ships with the toolchain)" >&2; exit 1; }
	exec "{{FLOCKER}}" --key "$DB_LOCK" -j 1 --heartbeat {{HEARTBEAT_SECS}} -- just _test-locked

# PRIVATE: everything that runs under the one shared DB lock held by `test`:
# the combined drift plan, then the microflows-viz backend suite (Python; needs
# the microflows schema loaded and the DB definitely present, hence in-lock).
_test-locked:
	#!/usr/bin/env bash
	set -euo pipefail
	just _test-combined
	just _test-viz

# PRIVATE: microflows-viz backend gate (work/viz-consolidation slice 1) — the
# viz_ro read-only-grant proof, serve/API tests, and the mfinspect parity
# harness. Runs ONLY under the shared lock (it resets the microflows schema via
# the component's PRIVATE _db-load-schema, bypassing its lock-acquiring public
# wrapper). MFVIZ_REQUIRE_DB=1 turns DB absence into a hard failure — this
# suite must never silently skip in a gate. Interpreter: the mariachi venv's
# python (has pymysql), same precedent as `_test-resilience-locked`; the
# packaging tests inside the suite skip without a local .venv, by design.
_test-viz:
	#!/usr/bin/env bash
	set -euo pipefail
	source tools/cert-env.sh
	echo "=== [root] test: microflows-viz backend (viz_ro grant + serve/API + mfinspect parity) ==="
	MARIACHI_PY="$(dirname "$MARIACHI_BIN")/python"
	[[ -x "$MARIACHI_PY" ]] || { echo "error: mariachi venv python (with pymysql) not found at $MARIACHI_PY -- required for microflows-viz's DB-backed tests (set MARIACHI_BIN to the venv's mariachi binary)" >&2; exit 1; }
	( cd microflows && just _db-load-schema )
	( cd microflows-viz && MFVIZ_REQUIRE_DB=1 PYTHONPATH=src "$MARIACHI_PY" -m unittest discover -s tests -v )

# PRIVATE: emit + run the one combined plan. Runs ONLY under the shared lock
# held by `test` (flocker is not re-entrant — the plan's own run-live jobs use
# $DB_HELD_GROUP, a distinct key, for their internal serial chain).
_test-combined:
	#!/usr/bin/env bash
	set -euo pipefail
	source tools/cert-env.sh
	: "${DRIFT_TOOLCHAIN_ROOT:?set DRIFT_TOOLCHAIN_ROOT to a toolchain >= 0.33.17}"
	export DRIFT_PKG_ROOT="${DRIFT_PKG_ROOT:-$HOME/opt/drift/certified/current/pkgs}"
	RUNNER="${DRIFT_TOOLCHAIN_ROOT}/lib/tools/drift_test_run.py"
	[[ -f "$RUNNER" ]] || { echo "error: shared executor not found at $RUNNER (need toolchain >= 0.33.17)" >&2; exit 1; }
	WORK="$(mktemp -d -t root-combined-test-XXXXXX)"
	trap 'rm -rf "$WORK"' EXIT
	python3 tools/emit_test_plan.py test --out "$WORK/plan.json"
	python3 "$RUNNER" --plan "$WORK/plan.json" --work-dir "$WORK" --heartbeat {{HEARTBEAT_SECS}}

# All performance suites, in the same order.
perf:
	#!/usr/bin/env bash
	set -euo pipefail
	source tools/cert-env.sh
	_dbstate="$("$DB_INSTANCE_SH" up)"   # restore entry state (see `test`): tear down on exit iff we started it
	case "$_dbstate" in   # restore EXACTLY: created(was absent)->remove; started(was stopped)->stop; running->leave
	  CREATED) trap '"$DB_INSTANCE_SH" down >/dev/null 2>&1 || true' EXIT ;;
	  STARTED) trap '"$DB_INSTANCE_SH" stop >/dev/null 2>&1 || true' EXIT ;;
	esac
	echo "=== [root] perf: singular ==="    ; ( cd singular   && just perf )
	echo "=== [root] perf: microflows ==="  ; ( cd microflows && just perf )
	just _integration-gate perf

# All stress / soak / concurrency / recovery suites, in the same order.
stress:
	#!/usr/bin/env bash
	set -euo pipefail
	source tools/cert-env.sh
	_dbstate="$("$DB_INSTANCE_SH" up)"   # restore entry state (see `test`): tear down on exit iff we started it
	case "$_dbstate" in   # restore EXACTLY: created(was absent)->remove; started(was stopped)->stop; running->leave
	  CREATED) trap '"$DB_INSTANCE_SH" down >/dev/null 2>&1 || true' EXIT ;;
	  STARTED) trap '"$DB_INSTANCE_SH" stop >/dev/null 2>&1 || true' EXIT ;;
	esac
	echo "=== [root] stress: singular ==="   ; ( cd singular   && just stress )
	echo "=== [root] stress: microflows ===" ; ( cd microflows && just stress )
	just _integration-gate stress

# AmbiguousWrite resilience gate (see work/workflows-failpoint-resilience/): injects a lost COMMIT
# ack via drift-mariadb-client's mariadb-failpoint-proxy against real Microflows/Singular schemas and
# verifies the coordinator/participant recover safely (retriable outcome, no crash, no duplicate
# mutation, RpcCommitError.kind respected). Deliberately NOT part of `test`/`_test-combined`: the
# proxy binary comes from a sibling repo that is not a declared cert capability
# (requires:["tool:mariachi","tool:docker"] only), and this gate is materially slower than the
# cert-critical path. Runnable in any environment with a `drift-mariadb-client` checkout built
# alongside this one (or MARIADB_FAILPOINT_PROXY_BIN pointing at a pre-built binary) — not yet wired
# into build-orchestrator's own capability set.
test-resilience:
	#!/usr/bin/env bash
	set -euo pipefail
	source tools/cert-env.sh
	: "${DRIFT_TOOLCHAIN_ROOT:?set DRIFT_TOOLCHAIN_ROOT to a toolchain >= 0.33.17}"
	export DRIFT_PKG_ROOT="${DRIFT_PKG_ROOT:-$HOME/opt/drift/certified/current/pkgs}"
	PROXY_BIN="${MARIADB_FAILPOINT_PROXY_BIN:-../drift-mariadb-client/build/local-app/mariadb-failpoint-proxy}"
	[[ -x "$PROXY_BIN" ]] || { echo "error: mariadb-failpoint-proxy not found at $PROXY_BIN (set MARIADB_FAILPOINT_PROXY_BIN, or build it: cd ../drift-mariadb-client && just build-app mariadb-failpoint-proxy)" >&2; exit 1; }
	MARIACHI_PY="$(dirname "$MARIACHI_BIN")/python"
	[[ -x "$MARIACHI_PY" ]] || { echo "error: mariachi venv python (with pymysql) not found at $MARIACHI_PY -- required for the failpoint tests' own DB assertions (set MARIACHI_BIN to the venv's mariachi binary)" >&2; exit 1; }
	_dbstate="$("$DB_INSTANCE_SH" up)"   # restore entry state (see `test`): tear down on exit iff we started it
	case "$_dbstate" in
	  CREATED) trap '"$DB_INSTANCE_SH" down >/dev/null 2>&1 || true' EXIT ;;
	  STARTED) trap '"$DB_INSTANCE_SH" stop >/dev/null 2>&1 || true' EXIT ;;
	esac
	[[ -x "{{FLOCKER}}" ]] || { echo "error: flocker not found at {{FLOCKER}} (required for DB serialization; ships with the toolchain)" >&2; exit 1; }
	# NOT `exec` here (unlike `test`'s own _test-combined call) -- exec REPLACES this shell's process
	# image, so the EXIT trap registered above (restoring DB entry state) would never fire if this
	# recipe is the one that created/started the DB. Running flocker as a normal foreground command
	# keeps this shell alive to run its own trap on exit; `set -e` already propagates flocker's exit
	# code identically (a nonzero exit here ends the script with that same code, after the trap runs).
	"{{FLOCKER}}" --key "$DB_LOCK" -j 1 -- just _test-resilience-locked "$PROXY_BIN" "$MARIACHI_PY"

# PRIVATE: runs ONLY under the shared lock held by `test-resilience` (flocker is not re-entrant, so
# every schema reset below calls the component's PRIVATE `_db-load-schema`/etc recipe directly,
# bypassing that component's own lock-acquiring public wrapper).
_test-resilience-locked PROXY_BIN MARIACHI_PY:
	#!/usr/bin/env bash
	set -euo pipefail
	source tools/cert-env.sh
	export DRIFT_PKG_ROOT="${DRIFT_PKG_ROOT:-$HOME/opt/drift/certified/current/pkgs}"

	echo "=== [resilience] reset schemas ==="
	( cd singular && just _db-load-schema )
	( cd microflows && just _db-load-schema && just _db-load-test-fixtures )

	echo "=== [resilience] build binaries ==="
	( cd microflows/runner && just prepare && just build )
	( cd microflows/participant-stub && just prepare && just build )

	echo "=== [resilience] start mariadb-failpoint-proxy ==="
	PROXY_DATA_PORT=43306
	PROXY_CONTROL_PORT=43307
	"{{PROXY_BIN}}" --data-host 127.0.0.1 --data-port "$PROXY_DATA_PORT" \
	                --backend-host "$DB_HOST" --backend-port "$DB_PORT" \
	                --control-host 127.0.0.1 --control-port "$PROXY_CONTROL_PORT" &
	PROXY_PID=$!
	trap 'kill "$PROXY_PID" 2>/dev/null || true; wait "$PROXY_PID" 2>/dev/null || true' EXIT
	_ready=0
	for _ in $(seq 1 50); do
	  if (exec 3<>"/dev/tcp/127.0.0.1/$PROXY_CONTROL_PORT") 2>/dev/null; then exec 3<&- 3>&-; _ready=1; break; fi
	  sleep 0.2
	done
	[[ "$_ready" == 1 ]] || { echo "error: mariadb-failpoint-proxy did not become ready on control port $PROXY_CONTROL_PORT" >&2; exit 1; }

	echo "=== [resilience] microflows failpoint tests (call_submit / operation_request / operation_settle / checkpoint_reverse_child_reopen / checkpoint_reverse_child_settle) ==="
	MF_RUNNER_BIN="$(pwd)/microflows/runner/build/dist/bin/mfrunner" \
	  "{{MARIACHI_PY}}" microflows/db-tests/failpoint_resilience_test.py

	echo "=== [resilience] singular failpoint tests, via participant-stub (start / complete / resume) ==="
	"{{MARIACHI_PY}}" microflows/participant-stub/tests/http/failpoint_resilience_test.py

# --- Repo-private MariaDB fixture (Docker) ---
# drift-workflows owns its DB: a private mariadb:11.4 container (tools/db_instance.sh) on port 34214 —
# NOT a platform `service:mariadb`, NOT shared with any other repo. The gates AUTO-PROVISION + tear it
# down; the container runtime is the declared `tool:docker` capability, so the cert surface is exactly
# `tool:mariachi` + `tool:docker`. These recipes are for explicit lifecycle control / fast iteration.

# Bring up the private MariaDB fixture (idempotent; ~3s cold, ~8ms if already running).
db-up:
	tools/db_instance.sh up

# Stop + remove the private MariaDB fixture.
db-down:
	tools/db_instance.sh down

# Report the private MariaDB fixture status.
db-status:
	tools/db_instance.sh status

# --- Component-focused gates (independently runnable) ---

test-singular:
	#!/usr/bin/env bash
	set -euo pipefail
	echo "=== [root] test: singular ==="; ( cd singular && just test )

test-microflows:
	#!/usr/bin/env bash
	set -euo pipefail
	echo "=== [root] test: microflows ==="; ( cd microflows && just test )

# Every discovered integration suite's test gate.
test-integration:
	#!/usr/bin/env bash
	set -euo pipefail
	just _integration-gate test

# --- Internal: run one gate across all discovered integration suites ---
# A suite = integration/<dir>/justfile. Ordered (sorted), fail-fast.
_integration-gate GATE:
	#!/usr/bin/env bash
	set -euo pipefail
	shopt -s nullglob
	found=0
	for d in integration/*/; do
		[[ -f "${d}justfile" ]] || continue
		found=1
		echo "=== [root] {{GATE}}: ${d} ==="
		( cd "$d" && just {{GATE}} )
	done
	if [[ "$found" -eq 0 ]]; then echo "[root] no integration suites found under integration/"; fi

# --- Certification author/deploy surface (the drift-web / drift-mariadb-client convention) ---
# The repo ROOT owns the multi-artifact author-claim + lock + deploy over the top-level
# drift/manifest.json (singular + microflows + uflowsd), signed with the Foundation key (The Drift
# Foundation owns this repo). The root manifest is ALSO the SOLE source of truth for the shipped
# artifact VERSIONS — the per-component manifests are local-dev only and pin every artifact to the
# sentinel `0.0.0` (drift requires a non-empty version; the sentinel makes clear no release-
# authoritative version lives in a component tree). Bump versions in the root, then `just reseal`.
# The orchestrator's stage_packages stages the PACKAGES ONLY:
# `drift deploy --artifact singular --artifact microflows --dest <libs_root>` and binds real cert-suite
# evidence. `uflowsd` is a kind:app artifact — NOT staged via the cert pool — its
# stage_packages is package-only BY POLICY (the app author/cert legs DO work: shipped driftc 0.33.61,
# `drift verify-app` green). uflowsd is built/signed LOCALLY by `just deploy` (which adds --app-dest + key). The local recipes
# here are the dev fallback (`--cert-suite-id drift-workflows/dev --cert-suite-no-evidence`) — no bespoke
# evidence ceremony, same as the other Foundation cert-pool repos.

# Re-mint drift/{singular,microflows}.author-claim under the Foundation author key.
#   DRIFT_LANG_ROOT (default ~/src/drift-lang); DRIFT_SIGN_KEY_FILE (default seed).
author-claim:
	#!/usr/bin/env bash
	set -euo pipefail
	DRIFT_LANG_ROOT="${DRIFT_LANG_ROOT:-${HOME}/src/drift-lang}"
	KEY_FILE="${DRIFT_SIGN_KEY_FILE:-${HOME}/.config/drift/keys/default.seed}"
	[[ -d "${DRIFT_LANG_ROOT}/tools/drift_author" ]] || { echo "error: tools.drift_author not found at ${DRIFT_LANG_ROOT}" >&2; exit 1; }
	[[ -f "${KEY_FILE}" ]] || { echo "error: signing key not found: ${KEY_FILE}" >&2; exit 1; }
	for ART in singular microflows uflowsd; do
	  echo "[author-claim] minting drift/${ART}.author-claim"
	  PYTHONPATH="${DRIFT_LANG_ROOT}" python3 -m tools.drift_author publish \
	    --manifest "$(pwd)/drift/manifest.json" \
	    --artifact "${ART}" \
	    --key-file "${KEY_FILE}" \
	    --overwrite
	done

# Resolve deps + write drift/lock.json against the package root.
prepare:
	#!/usr/bin/env bash
	set -euo pipefail
	DRIFT="${DRIFT_TOOLCHAIN_ROOT:-$HOME/opt/drift/certified/current/toolchain}/bin/drift"
	"$DRIFT" prepare --package-root "${DRIFT_PKG_ROOT:-$HOME/opt/drift/certified/current/pkgs}"

# Read-only trust preflight (author-claims, SCI equality, trust grants).
trust-check:
	#!/usr/bin/env bash
	set -euo pipefail
	DRIFT="${DRIFT_TOOLCHAIN_ROOT:-$HOME/opt/drift/certified/current/toolchain}/bin/drift"
	"$DRIFT" trust check

# Re-mint author-claims + re-resolve lock + trust-check; run before committing a version bump.
# (Does NOT test — run `just test` first.)
reseal:
	@just author-claim
	@just prepare
	@just trust-check
	@echo "[reseal] done — review & commit: drift/manifest.json, drift/lock.json, drift/*.author-claim"

# Build, sign, and publish singular + microflows (packages) + uflowsd (app). Dev fallback cert-suite;
# the orchestrator overrides --cert-suite-id and binds real evidence (deps resolved from DRIFT_PKG_ROOT).
deploy *ARGS:
	#!/usr/bin/env bash
	set -euo pipefail
	DRIFT="${DRIFT_TOOLCHAIN_ROOT:-$HOME/opt/drift/certified/current/toolchain}/bin/drift"
	LIBS="${DRIFT_PKG_ROOT:-$HOME/opt/drift/certified/current/pkgs}"
	DEST="${DRIFT_DEPLOY_DEST:-build/deploy}"
	APP_DEST="${DRIFT_APP_DEST:-build/deploy-app}"   # uflowsd is kind:app -> drift deploy requires --app-dest
	KEY_FILE="${DRIFT_SIGN_KEY_FILE:-$HOME/.config/drift/keys/default.seed}"
	# Preflight: deploying the app artifact needs a signing key — fail clearly rather than deep in `drift deploy`.
	[[ "{{ARGS}}" == *--sign-key-file* || -f "$KEY_FILE" ]] || { echo "error: signing key not found: $KEY_FILE (set DRIFT_SIGN_KEY_FILE or pass --sign-key-file)" >&2; exit 1; }
	mkdir -p "$DEST" "$APP_DEST"
	EXTRA=""
	# Dev fallback ONLY when neither --cert-suite* (CLI) nor DRIFT_DEPLOY_CERT_SUITE_* (env) supplies one —
	# otherwise an env-driven real cert-suite would be clobbered by the dev sentinel.
	if [[ "{{ARGS}}" != *--cert-suite* ]] && [[ -z "$(env | grep '^DRIFT_DEPLOY_CERT_SUITE_' || true)" ]]; then
	  EXTRA="--cert-suite-id drift-workflows/dev --cert-suite-no-evidence"
	fi
	[[ "{{ARGS}}" == *--app-dest* ]]      || EXTRA="$EXTRA --app-dest $APP_DEST"
	[[ "{{ARGS}}" == *--sign-key-file* ]] || EXTRA="$EXTRA --sign-key-file $KEY_FILE"
	"$DRIFT" deploy --dest "$DEST" --package-root "$LIBS" ${EXTRA} {{ARGS}}
