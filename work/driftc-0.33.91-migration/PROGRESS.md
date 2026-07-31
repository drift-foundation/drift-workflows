# PROGRESS — driftc 0.33.91 migration

Status: STARTED 2026-07-30. Docs read (release notes, mariadb/net-tls notes,
effective-drift §call-site auto-borrow, history 0.33.91). Staged compiler
verified: `driftc 0.33.91 | abi 22 | git ef7ebd14`.

Ledger:
- [x] Read canonical + dep migration docs; charter written.
- [~] Source migration to convergence (diagnostics-driven, scratchpad mig.py
      over driftc --json spans; zero skipped spans anywhere):
      mfrunner build (microflows pkg + runner) clean: 1,887 edits / 7 iters;
      participant-stub build (singular pkg + stub) clean: 253 edits / 5 iters;
      all 5 singular + 5 microflows test sources clean; runner ir_graph_test /
      ir_exec_test / parser_test (compiler-stress canary, 308 edits) clean.
      DONE: 3,718 borrow tokens removed (git grep '&' HEAD vs worktree),
      every edit from a driftc --json diagnostic span, zero skipped.
      0 E_MUT_RVALUE_ARG_BINDING_REQUIRED, 0 E_OVERLOAD_PARAM_MODE_ONLY_DIFF.
- [x] Version bumps staged: singular 0.9.2 / microflows 0.8.2 / uflowsd 0.7.3
      (manifest version-only diff + RUNNER_VERSION echo synced).
- [~] Shipped docs + compiler floor: justfile DRIFT_TOOLCHAIN_ROOT guards
      (5 files) + examples/README.md + examples/RUN_LOCAL.md now say
      0.33.91+/ABI 22 (executor ">= 0.33.17" messages left — they describe
      drift_test_run.py availability, not the source floor). No shipped md has
      drift call-borrow code samples (checked: no ```drift fences, no `(&`).
- [x] Gates (staged toolchain, DRIFT_TEST_JOBS=8), all green:
      just test: drift-test-run 61 ok / 0 failed + microflows-viz 62/62 OK;
      just test-resilience: 20 passed / 0 failed (failpoints armed AND fired);
      just stress: singular 2 ok + microflows 3 ok, exit 0;
      just perf: lease_cycle 639us (baseline 633, limit 1899) PASS;
      service_reserve_drive 3.057 ms/wf (baseline 3.12, limit 9.4) PASS.
      mfrunner --version -> "mfrunner 0.7.3".
- [x] Reseal: author-claims re-minted (0.9.2/0.8.2/0.7.3), root lock
      re-resolved (external deps unchanged from certified pool; co-artifact
      microflows@0.8.2), component dev locks sha-resynced, trust-check green.
- [ ] Commit (BLOCKED on explicit user permission) → deploy/stage → SCI check.
- [ ] Announce note in /tmp/drift-announce/.

Next action: user reviews proposed commit message + file list; on approval,
commit, then `just deploy` to stage, verify SCI, publish announce.
