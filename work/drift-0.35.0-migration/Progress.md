# Progress — drift-0.35.0 alignment

Status: DONE (pending user commit) — review round addressed: floor enforced
at 0.35.0, minor bumps 0.10.0/0.9.0/0.8.0, duplicate-key pins landed, all
gates green on final tree, resealed, follow-up announce published. DB parity
tracked separately as BLOCKED: work/mariadb-12.3-json-parity/.

## Ledger

- [x] Repin all 5 lock.json files against staged pool (mariadb-rpc 0.8.2,
      wire-proto 0.6.2, net-tls 0.6.4, web-client 0.5.5, web-jwt 0.5.4,
      web-rest 0.6.5) with drift-0.35.0+abi22 `drift prepare`.
- [x] Pre-scan for 0.35.0 hazards: no unqualified `Ok(`; stored callbacks
      already `core.callback1`; compile is the real audit.
- [x] Round-1 `just test` probe: FAILED uniformly on `std.json` no longer
      exporting `parse_strict` (0.33.93 clean break, per staged toolchain
      doc/stdlib/std_json.md: `parse()` IS the strict entry now).
- [x] Migration: renamed all 29 `json.parse_strict(` → `json.parse(` sites
      (runner ir/parser/runner, host, participant-stub app, 2 runner unit-test
      files — compile-required rename only). NOTE: ~24 pre-existing
      `json.parse(` sites silently gain strict semantics (old permissive
      default removed); gates validate, inputs are self-produced envelopes.
      No lambda-contract or `Ok(...)` fallout observed in round 1.
- [x] Round-2 `just test` on staged 0.35.0: GREEN (exit 0; combined drift plan
      passed fail-fast, microflows-viz 62/62 OK in 2.73s).
- [x] `just perf`: GREEN — singular lease_cycle 640us/cycle (baseline 633,
      limit 1899) PASS; coordinator service_reserve_drive 2.969 ms/wf
      (baseline 3.12, limit 9.4) PASS.
- [x] `just stress`: GREEN — singular stress 2 ok/0 failed; coordinator
      concurrent-drive 20 rounds x 8 drivers, exactly-once dispatch held.
- [x] Version bumps: singular 0.9.2→0.9.3, microflows 0.8.2→0.8.3,
      uflowsd 0.7.3→0.7.4 (+ RUNNER_VERSION runner.drift:54).
- [x] `just reseal`: GREEN — claims minted (release_utc 2026-08-06T05:33:53Z),
      root lock re-resolved (uflowsd → microflows@0.8.3), trust-check ✓ x3.
- [x] Final `just test` on resealed tree: GREEN — combined plan 61 ok /
      0 failed / 0 skipped (342.5s) + microflows-viz 62/62 OK.
- [x] Announce note published:
      /tmp/drift-announce/2026-08-06T05-40-31Z-drift-workflows-release-notes.md
      (verdict: adopted with reviewed migration).

## Round 2 (review feedback — floor + minor bumps + pins)

- [x] Floor enforced at 0.35.0 in tools/cert_deps.py (fail-closed; verified
      both directions); all ">= 0.33.91" texts/docs updated.
- [x] Minor bumps: singular 0.10.0 / microflows 0.9.0 / uflowsd 0.8.0;
      RUNNER_VERSION synced; uflowsd microflows range 0.8→0.9; changelog
      entries in singular/history.md + microflows/CHANGELOG.md.
- [x] Duplicate-key pins (top-level + nested): runner
      tests/unit/strict_json_test.drift (standalone compile+run verified,
      wired into root emitter + runner justfile); singular malformed fixture
      0x0F/0x10 (SP + test); manifest_dupkey_{toplevel,nested}_rejected
      fixtures (13/13 manifest suite green).
- [x] DB parity recorded BLOCKED on MariaDB 12.3 migration:
      work/mariadb-12.3-json-parity/ (UNIQUE KEYS guards + DB-boundary pins).
- [x] Reseal with 0.10.0/0.9.0/0.8.0: claims minted (release_utc
      2026-08-06T11:02:03Z), lock resolves uflowsd → microflows@0.9.0,
      trust-check ✓ x3.
- [x] Full gates on final tree: `just test` 65 ok / 0 failed + viz 62/62;
      `just perf` 662us (limit 1899) + 2.846 ms/wf (limit 9.4) PASS;
      `just stress` green, exactly-once held (20x8).
- [x] Follow-up announce note:
      /tmp/drift-announce/2026-08-06T11-13-01Z-drift-workflows-release-notes.md
      (supersedes the 05-40-31Z note's versioning).

## Round 3 (review P2s — floor hardening + doc refresh)

- [x] P2 fix: enforce_toolchain_floor rejects NONZERO driftc exit BEFORE
      parsing stdout (review repro: fake driftc printing valid at-floor JSON
      with exit 1 was previously accepted).
- [x] Floor now FULLY enforced (the "every gate compile" claim was overbroad
      — dep-free standalone ir/unit compiles bypassed cert_deps): all four
      plan emitters call enforce_toolchain_floor() at emit time; runner
      justfile's direct-driftc test loop runs `cert_deps.py --check-floor`;
      participant-stub/runner builds already exec cert_deps for --dep flags.
      Verified: all emitters pass at floor, reject 0.34.1.
- [x] Focused coverage: tools/tests/test_cert_deps_floor.py (6 cases incl.
      the nonzero-exit repro, fake-toolchain stubs) — 6/6 green standalone,
      wired into the root plan as gated job cert-deps-floor-test.
- [x] README refreshed: minor versions 0.10.0/0.9.0/0.8.0 as the accepted
      decision, 66-job test criteria, floor-enforcement coverage described
      precisely; CHANGELOG/history wording corrected likewise.
- [x] `just test` rerun on final tree: GREEN — 66 ok / 0 failed / 0 skipped
      (344.2s) + viz 62/62. Perf/stress evidence from round 2 stands (no
      .drift/manifest/claim change this round); no reseal needed.
- [x] Superseding announce note:
      /tmp/drift-announce/2026-08-06T11-37-18Z-drift-workflows-release-notes.md

## Uncommitted worktree

- 5x lock.json repinned (root, microflows, singular/drift, runner,
  participant-stub); 29-site parse_strict→parse rename (7 files, incl. 2 runner
  unit tests — compile-required); drift/manifest.json version bumps;
  runner.drift RUNNER_VERSION; drift/*.author-claim re-minted;
  work/drift-0.35.0-migration/. AGENTS.md/AGENTS-MAILBOX-PROTO.md changes
  pre-date this effort (not ours).

## Next action

User commits the adoption files (git is user-run per repo policy); suggested
scope split: adoption (locks, manifest, claims, .drift renames + versions,
work/drift-0.35.0-migration/) separate from the AGENTS.md /
AGENTS-MAILBOX-PROTO.md workflow changes.
