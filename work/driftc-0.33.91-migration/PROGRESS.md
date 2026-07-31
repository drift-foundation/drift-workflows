# PROGRESS — driftc 0.33.91 migration

Status: **REOPENED 2026-07-31** — cert run 20260731-001644-drift-lang-ef7ebd1
REJECTED drift-workflows@1be3d62: the bundled run's package pool carries ONLY
the 0.33.91-migrated dep versions (mariadb-rpc 0.8.1, wire-proto 0.6.1,
net-tls 0.6.3, web-client 0.5.4, web-jwt 0.5.3, web-rest 0.6.4) while our
committed locks pinned the previous certified versions exactly → every
dep-consuming build job failed "version not found under package roots".
Not a code/toolchain issue (dep-free runner IR jobs passed).

Fix round (mirrors 499bc37 "repin deps"): ALL FIVE locks repinned against
~/opt/drift/staged/libs (byte-identical to the run pool — verified by sha256
on mariadb-rpc 0.8.1 + net-tls 0.6.3): root drift/lock.json,
microflows/drift, singular/drift/drift, microflows/runner/drift,
microflows/participant-stub/drift. First re-gate attempt failed because the
two COMPONENT locks (microflows/drift, singular/drift/drift) were missed —
their emitters then resolved mariadb-rpc@0.8.0 from staged/libs, which holds
a STALE pre-0.33.88 0.8.0 build (sha 4c0c9ac0 ≠ certified 348d230e); 0.33.91
consuming it fails with a misleading "module 'mariadb.rpc' does not export
trait 'RpcCommitErrorKind'" (repro: same source compiles clean vs 0.8.1).
Lesson: repin means ALL lock.json files (find . -name lock.json), and
staged/libs keeps stale old-version copies — the exact-version pin is what
protects against them. Gate3 (strict, repinned): ALL GREEN — test 61 ok/0 +
viz 62/62; resilience 61+20/0; stress 2+3 ok; perf 643us / 3.112ms PASS.
Reseal green (SCIs unchanged, release_utc+sig only).

CERT-TEAM ANSWER (2026-07-31T012412Z announce): pool stays candidate-only;
our proposal declined; consuming repos must run cert-lane builds in
source-rebuild mode keyed off DRIFT_CERT_MODE=certify (orchestrator exports
it + DRIFT_RUN_SNAPSHOT). Implemented as repo-root tools/cert_deps.py (the
cert-env.sh shim precedent) — ONE dep-flag authority: strict lane reads the
committed lock (unchanged dev behavior); certify lane fresh-resolves within
manifest ranges + author-claim required_deps closure against DRIFT_PKG_ROOT,
gates every pick against the run snapshot (scid + artifact sha256; missing/
mismatch = hard fail), and prints lock drift as "[cert-deps] evidence:"
lines. Wired into: singular emitter, microflows emitter, integration
emitter (_external_deps), runner justfile build, participant-stub justfile
build (root emitter has no own dep jobs). Verified: strict CLI == old lock
flags; certify vs HEAD's stale lock floats 0.8.0→0.8.1 etc. with evidence
lines; strict & certify-sim root plans emit IDENTICAL 61-job dep sets;
certify-without-snapshot hard-fails.

ORCH REVIEW (2026-07-31T013936Z announce): rewiring validated green, BUT the
hand-rolled `_fresh_graph`/`_in_range`/`_snapshot_gate` resolver must not
ship — a parallel copy of drift-lang's single source-rebuild authority
(misses full identity-triple gating, constraint operators like ^0.3.0,
co-artifact overlays, structural trust gates). REWRITTEN: cert_deps.py's
certify branch now delegates to
tools.drift_deploy.source_rebuild.resolve_source_rebuild imported from a
drift-lang SOURCE checkout ($DRIFT_LANG_SRC → $DRIFT_LANG_ROOT →
~/src/drift-lang; binary toolchain doesn't ship the module), evidence via
its print_evidence (redirected to stderr; CLI stdout = flags only),
snapshot_exempt_ids=None (pure consumer). Strict lane stays stdlib-only.
INTERIM until drift-lang ships `drift lock emit --source-rebuild` (their
CLI ask) — then the certify branch collapses to one exec. TRACKING: delete
the import path when that ships.
Learned in testing: certify mode REQUIRES the candidate-only run pool as
DRIFT_PKG_ROOT — the authority's index builder hard-fails on any pool
package absent from the snapshot (staged/libs is invalid input by design).
Verified vs the rejected run's real pool+snapshot: repinned lock → clean
flags; stale HEAD lock → floats 0.8.0→0.8.1 etc. with full typed evidence.

Gate5 (drift-lang-src interim): ALL GREEN — strict test 61/61 + viz;
certify-sim vs real run pool/snapshot 61/61; runner+stub builds both modes.

DRIFT-LANG SHIPPED THE CLI (2026-07-31T042844Z announce): `drift lock emit
--artifact X --source-rebuild` in staged 0.33.92 (driftc 0.33.92 | abi 22 |
git ff1bc2b2). STOPGAP DELETED (tracking item CLOSED): cert_deps.py's
certify branch is now ONE EXEC of $DRIFT_TOOLCHAIN_ROOT/bin/drift lock emit
--source-rebuild (stdout=flags, stderr=evidence, fail-closed); no
DRIFT_LANG_SRC, no source import, no hand-rolled resolver. Certify lane
requires run toolchain >= 0.33.92 (older rejects the flag = correct fail).
CLI checks green: strict unchanged; certify vs run pool+snapshot resolves
0.8.1/0.6.1/0.6.3/0.5.4/0.5.3/0.6.4; 0.33.91 toolchain fails closed.

Running (gate6): full `just test` certify-sim + strict under 0.33.92
toolchain + runner/stub certify builds (compile canary for the upcoming
0.33.92 bundled run). Then: final combined commit proposal, user commits,
submit to cert (no local redeploy — cert builds its own artifacts).

Ledger (all complete):
- [x] Docs read; source migration to convergence: 3,718 redundant argument
      borrows removed, every edit from a driftc --json diagnostic span, zero
      skipped; 0 E_MUT_RVALUE_ARG_BINDING_REQUIRED, 0
      E_OVERLOAD_PARAM_MODE_ONLY_DIFF; parser_test canary compiles clean.
- [x] Floor raised to 0.33.91+/ABI 22 (justfile guards + examples docs).
- [x] Gates green: test 61 ok/0 failed + viz 62/62; resilience 20/20;
      stress green; perf PASS (lease_cycle 639us, service_reserve_drive
      3.057 ms/wf — no regression).
- [x] Versions bumped + RUNNER_VERSION synced; reseal green; committed by
      user as 1be3d62 BEFORE the build (provenance names it).
- [x] `just deploy`: three artifacts published to build/deploy{,-app};
      SCI agrees across provenance/author-claim/cert-claim per artifact;
      `drift verify-app` OK; binaries echo 0.7.3.
- [x] Announce published:
      /tmp/drift-announce/2026-07-31T001622Z-drift-workflows-release-notes.md

Next action: none — effort landed. Per work/README.md convention this folder
should be DELETED in a follow-up commit (it was included in 1be3d62).
