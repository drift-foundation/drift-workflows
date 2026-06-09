# Drift Workflows — umbrella monorepo.
#
# Components:
#   microflows/   typed durable workflow manager/service + runtime
#   singular/     reusable language-neutral idempotency protocol + library
#
# This thin root justfile delegates the standard Foundation gates (test,
# stress, perf) to both components. Each component has its own justfile and is
# independently buildable, testable, versioned, signed, and publishable.

# Run the test gate for both components.
test:
	#!/usr/bin/env bash
	set -euo pipefail
	echo "=== drift-workflows: test singular ==="
	( cd singular && just test )
	echo "=== drift-workflows: test microflows ==="
	( cd microflows && just test )

# Run the stress gate for both components.
stress:
	#!/usr/bin/env bash
	set -euo pipefail
	echo "=== drift-workflows: stress singular ==="
	( cd singular && just stress )
	echo "=== drift-workflows: stress microflows ==="
	( cd microflows && just stress )

# Run the perf gate for both components.
perf:
	#!/usr/bin/env bash
	set -euo pipefail
	echo "=== drift-workflows: perf singular ==="
	( cd singular && just perf )
	echo "=== drift-workflows: perf microflows ==="
	( cd microflows && just perf )
