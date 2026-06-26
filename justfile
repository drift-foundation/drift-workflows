# Drift Workflows — umbrella monorepo.
#
# Components (each independently buildable, testable, versioned, signed):
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

# --- Aggregate gates (repository-wide readiness) ---

# All gates: component unit + stored-procedure + component E2E, then every
# cross-component integration suite. Ordered, fail-fast (set -e).
test:
	#!/usr/bin/env bash
	set -euo pipefail
	source tools/cert-env.sh
	# Gate RESTORES ENTRY STATE exactly: bring up our private DB, then on exit (success OR failure) put it
	# back as found — created->removed, was-stopped->stopped, was-running->left. Sub-gates see it RUNNING.
	_dbstate="$("$DB_INSTANCE_SH" up)"
	case "$_dbstate" in   # restore EXACTLY: created(was absent)->remove; started(was stopped)->stop; running->leave
	  CREATED) trap '"$DB_INSTANCE_SH" down >/dev/null 2>&1 || true' EXIT ;;
	  STARTED) trap '"$DB_INSTANCE_SH" stop >/dev/null 2>&1 || true' EXIT ;;
	esac
	just test-singular
	just test-microflows
	just test-integration

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
# drift/manifest.json (singular + microflows), signed with the Foundation key (The Drift
# Foundation owns this repo). The orchestrator's stage_packages runs the bare
# `drift deploy --dest <libs_root>` and binds real cert-suite evidence; the local recipes
# here are the dev fallback (`--cert-suite-id drift-workflows/dev --cert-suite-no-evidence`).
# No bespoke evidence ceremony — same as the other Foundation cert-pool repos.

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
	"$DRIFT" prepare --package-root "${DRIFT_PKG_ROOT:-$HOME/opt/drift/certified/current/libs}"

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

# Build, sign, and publish singular + microflows. Dev fallback cert-suite; the orchestrator
# overrides --cert-suite-id and binds real evidence (deps resolved from DRIFT_PKG_ROOT).
deploy *ARGS:
	#!/usr/bin/env bash
	set -euo pipefail
	DRIFT="${DRIFT_TOOLCHAIN_ROOT:-$HOME/opt/drift/certified/current/toolchain}/bin/drift"
	LIBS="${DRIFT_PKG_ROOT:-$HOME/opt/drift/certified/current/libs}"
	DEST="${DRIFT_DEPLOY_DEST:-build/deploy}"
	mkdir -p "$DEST"
	EXTRA=""
	if [[ "{{ARGS}}" != *--cert-suite* ]]; then
	  EXTRA="--cert-suite-id drift-workflows/dev --cert-suite-no-evidence"
	fi
	"$DRIFT" deploy --dest "$DEST" --package-root "$LIBS" ${EXTRA} {{ARGS}}
