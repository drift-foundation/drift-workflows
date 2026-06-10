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
test: test-singular test-microflows test-integration

# All performance suites, in the same order.
perf:
	#!/usr/bin/env bash
	set -euo pipefail
	echo "=== [root] perf: singular ==="    ; ( cd singular   && just perf )
	echo "=== [root] perf: microflows ==="  ; ( cd microflows && just perf )
	just _integration-gate perf

# All stress / soak / concurrency / recovery suites, in the same order.
stress:
	#!/usr/bin/env bash
	set -euo pipefail
	echo "=== [root] stress: singular ==="   ; ( cd singular   && just stress )
	echo "=== [root] stress: microflows ===" ; ( cd microflows && just stress )
	just _integration-gate stress

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
