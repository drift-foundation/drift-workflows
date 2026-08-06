# Progress — DB-side strict-JSON parity

Status: BLOCKED — waiting on the MariaDB 12.3 fixture migration (user
decision 2026-08-06; 11.4's JSON_VALID admits duplicate keys and has no
unique-key predicate to guard with).

## Ledger

- [x] Charter recorded; Drift-side strict validation kept (no relaxation).
- [x] Drift-side duplicate-key pins landed in the 0.35.0 round (runner unit
      test, singular malformed fixture 0x0F/0x10, manifest_dupkey_* fixtures)
      — see work/drift-0.35.0-migration/.
- [ ] BLOCKED: MariaDB 12.3 fixture migration (image bump in
      tools/db_instance.sh + template compatibility pass).
- [ ] IS JSON OBJECT WITH UNIQUE KEYS CHECK constraints + ingress SP guards.
- [ ] DB-boundary duplicate-key pins (top-level + nested) in the schema
      regression suites.

## Next action

None here until the 12.3 migration starts; then follow README "Work when
unblocked" in order.
