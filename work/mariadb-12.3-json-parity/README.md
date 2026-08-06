# DB-side strict-JSON parity — blocked on the MariaDB 12.3 migration

## Objective

Bring the DATABASE boundary to parity with the Drift-side strict std.json
acceptance contract adopted in the drift-0.35.0 alignment
(work/drift-0.35.0-migration/): JSON documents entering our schemas must be
rejected when they carry duplicate keys (top-level or nested), so the DB stops
being a permissive store for documents the application layer would refuse.

## Why blocked

The repo's private fixture is `mariadb:11.4` (tools/db_instance.sh). On 11.4,
`JSON_VALID` ACCEPTS duplicate-key objects, and there is no unique-key
predicate to build a CHECK constraint or SP guard from — so DB-side rejection
cannot be implemented or tested on the current fixture. The user has decided
to migrate to MariaDB 12.3, which provides the
`IS JSON OBJECT WITH UNIQUE KEYS` predicate; this effort is BLOCKED until that
migration lands. Decision (2026-08-06): keep strict validation Drift-side now,
do NOT relax it, and close the DB gap in the 12.3 migration rather than
attempting a 11.4 workaround.

## Work when unblocked

1. Bump the fixture image (tools/db_instance.sh) and any version-sensitive
   mariachi templates to MariaDB 12.3.
2. Add `IS JSON OBJECT WITH UNIQUE KEYS`-based guards for every JSON-bearing
   column/parameter in singular/db, microflows/db, and the db-tests fixture
   schemas: CHECK constraints where the column contract is "JSON object", and
   ingress SP guards (SIGNAL on violation) where documents arrive via
   procedure parameters (e.g. args/continuation/payload/checkpoint paths).
3. Pin duplicate-key rejection at the DB boundary — top-level AND nested —
   mirroring the Drift-side pins added in the 0.35.0 round:
   - runner unit pin: microflows/runner/tests/unit/strict_json_test.drift
   - gateway/backend pin: singular malformed fixture keys 0x0F/0x10
   - operator-manifest pins: runner manifest fixtures manifest_dupkey_*
   DB pins should live in the schema regression suites (singular db-tests +
   microflows db-tests) and assert SIGNAL/refusal for both depths.
4. Re-verify the Drift-side pins still hold (the two layers must agree:
   whatever the DB now rejects, the gateway also rejects — no acceptance gap
   in either direction for duplicate keys).

## Verification criteria

- Fixture on 12.3; all existing gates green.
- New DB-boundary duplicate-key regressions (top-level + nested) red-green
  demonstrated against a guard-free schema, then green with guards.
- No Drift-side relaxation anywhere (`parse_with_config(..., permissive())`
  remains absent from production sources).
