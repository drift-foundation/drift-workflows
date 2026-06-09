# Singular — Outstanding Work

## Test key seeding

- E2e tests currently use fixed key seeds (e.g., `"test-claim-complete-redone"`),
  which means re-running tests without cleaning the DB hits stale `AlreadyDone`
  state. Add a per-run seed (timestamp, random, or run counter) so each test run
  derives unique idempotency keys. This removes the dependency on DB cleanup
  between runs and makes `just test` fully idempotent.

## Stored procedure cleanup (blocked on Java retirement)

- `db/procs/sp_singular_inspect.sql`: When the Java Singular client is no longer
  in use, change the "not found" path from returning an all-NULL row to returning
  an empty result set (`SELECT 1 FROM DUAL WHERE FALSE;`). This removes the need
  for callers to check `is_null(status_code)` to distinguish "not found" from a
  real result. The current behavior exists because the Java client was written to
  always expect exactly one row.
