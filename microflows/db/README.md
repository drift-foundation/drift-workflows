# Microflows — MariaDB backend (DB asset)

This directory is the **MariaDB backend material for the `microflows` package at this version** — the
durable workflow tables (workflow / args / plan / event / checkpoint / operation) and the `sp_mf_*`
stored procedures the coordinator drives at runtime. It ships as a declared package **asset packed
inside the certified `.zdmp`** (driftc 0.33.56+) and is materialized by consumers through the
verify-gated `drift unpack`.

## It is a Mariachi template

The layout is a standard Mariachi schema template:

```
db/
  schema/      CREATE TABLE … (workflow control + history)
  procs/       the sp_mf_* stored procedures (the coordinator's backend contract)
  constants/   deterministic lookup rows (none today)
  grants/      role grants ({{SCHEMA}} placeholder substituted at apply time)
```

Apply it with Mariachi. The SQL is **schema-agnostic** (no hard-coded schema qualifiers), so it can be
applied into any schema name.

## Schema name is deployment scope

The default / conventional schema name is **`microflows`** — it must match the schema your coordinator's
DB config connects to. But the name is a **deployment choice, not part of the certified identity**: you
may provision the same versioned template into `microflows`, `microflows_5`, `microflows_canary`, … to
run multiple package versions side by side or to canary one.

## How to provision (certified path)

```bash
# 1. Verify + materialize the DB asset from the certified package (fail-closed, atomic):
drift unpack "$DRIFT_PKG_ROOT/microflows/0.2.0" --dest "$t" \
  --trust-store <your-trust.json> --expect-version 0.2.0 --expect-sci sha256:<resolved>

# 2. Apply the template into your chosen schema name (drift unpack materializes the asset at its
#    declared path under --dest, i.e. "$t/microflows/db" — no `assets/` prefix):
mariachi --schema-template "$t/microflows/db" \
  --host "$DB_HOST" --port "$DB_PORT" --user "$DB_USER" --password-env "$DB_PW_ENV" \
  apply --schema microflows --env production
```

Consumers **may** load the `.sql` through another reviewed process — these are plain SQL files — but
Mariachi is the supported, idempotent path.

## Not included

Test-only and malformed fixtures, and dev scenario/coordinator-fixtures seed data, are **not** part of
this template — this directory is production-only. Those fixtures live outside the package
(`microflows/db-tests/`) and are never shipped in the asset.
