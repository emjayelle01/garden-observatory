# Task 15.1 — Model-independent durable recognition-job foundation

**Status: implementation complete and validated locally on Windows; pull
request open for review. Not merged.**

**No Raspberry Pi or production access. No deployment, no migration of any
real database, no model, no dependency, no configuration, no API, no service.
Schema 4 exists only in this branch.**

---

## 1. Scope

The local, model-independent foundation species recognition will run inside:

- migration 004 (`recognition_jobs`, `recognition_results`) and schema version 4;
- catalogue-driven eligibility reusing retention's media safety boundary;
- an idempotent reconciler;
- atomic claims, leases, heartbeat, bounded retries and terminal handling;
- a typed adapter protocol, a safe media open, and a deterministic fake adapter;
- a single-job runner;
- tests, mutation-register entries, `docs/Recognition.md` and this record.

Out of scope, and absent: any real detector or classifier, model files,
weights, labels or taxonomies, pixel decoding, a worker loop or systemd unit, a
CLI, configuration, an API route, detection/candidate/review/encounter/
sighting/first-seen/outbox/notification tables, human confirmation, and any
change to capture, event capture, retention, backup or notifications.

## 2. Baseline

| Item | Value |
|------|-------|
| Repository | `C:\AI\garden-observatory`, `emjayelle01/garden-observatory` |
| Branch at start | `main` |
| `main` / `origin/main` / live `refs/heads/main` | `fef11379fa956d68c1ac3184d63f4b6d9b0e54de` (all equal, 0 ahead / 0 behind) |
| Working tree | clean; only ignored caches and the ignored local `data/mgo.db` (untouched) |
| Stashes, extra worktrees, in-progress operations | none |
| Open pull requests, Task 15 branches | none |
| Full suite at baseline | 3483 passed, 20 skipped (26 m 46 s) |
| Local SQLite | 3.53.1 (Python 3.13.14) |

## 3. Inherited from Task 15.0 (design, not evidence)

The post-publication insertion point; catalogue-driven eligibility with exact
`origin = "motion"`, lifecycle exclusion, an operator-supplied enrolment
watermark and retention-equivalent path safety; no directory scanning; job and
result separation; `UNIQUE (capture_id, pipeline_version)`; the job-state,
error-category and outcome vocabularies; leases and bounded retries; short
transactions; the retention and missing-media contract; no camera, capture,
retention or backup coupling; the 12-row legacy catalogue audit (four
mock/pytest, four relocated-development, four in-directory legacy rows); and
the schema-4 rollback implication. The Task 15.0 decision itself is not a
repository document; it is recorded here as received in the task brief.

## 4. Implementation decisions made in Task 15.1

All are within the Task 15.0 contract; none adjusts it. Rationale for each is in
`docs/Recognition.md` §15.

1. `unsafe_path` is terminal `failed` (the contract allowed failed or skipped).
2. Retention's `not_regular_file` maps to recognition's `unsafe_path`.
3. `unexpected` is retryable and bounded by `max_attempts`.
4. A lease that expires on the final attempt is ended `failed` / `unexpected`
   by the next claim for that pipeline version.
5. Attempts are counted at claim time.
6. Any lifecycle row at run time skips the job as `media_missing`, even if the
   file still exists.
7. Heartbeat requires an unexpired owned lease; completion requires ownership
   only.
8. The lease owner is `<worker_id>:<uuid4 hex>`, unique per claim, and every
   claimed write also matches the attempt number.
9. Every recognition connection carries a SQLite authorizer denying row writes
   (and therefore schema changes) outside the two recognition tables.
10. Every recognition timestamp is constrained by `CHECK … GLOB` to one UTC
    microsecond layout, because leases and retry times are compared as text.
11. Legacy adoption (`_VERSION_TABLES` / `_VERSION_TABLE_SHAPES`) recognises
    version 4 and verifies both tables' full safety shape, following the
    precedent migration 003 set.
12. Heartbeat is a callback on the adapter request; no worker-status table.
13. Enqueue runs in batches of at most 100 inserts per transaction.

## 5. Final schema

See `migrations/004_recognition_jobs.sql` and `docs/Recognition.md` §4.
`CURRENT_SCHEMA_VERSION` is 4. Migration 004 is additive: plain `CREATE TABLE`
(a pre-existing table of the same name aborts it), two new tables, two new
indexes (`idx_recognition_jobs_due`, `idx_recognition_jobs_lease`), and four
SQLite auto-indexes from the primary and unique keys. No existing table or index
is altered — proven by byte-identical `sqlite_master` DDL before and after an
upgrade from a genuine schema-3 database.

### Files

New: `migrations/004_recognition_jobs.sql`; `src/mgo/recognition/`
(`__init__`, `models`, `eligibility`, `repository`, `reconciler`, `adapter`,
`fake_adapter`, `runner`); `tests/test_recognition_schema.py`,
`tests/test_recognition_eligibility.py`, `tests/test_recognition_jobs.py`;
`docs/Recognition.md`; this record.

Modified: `src/mgo/core/database.py` (version 4 and adoption shapes);
`tests/test_database_migrations.py`, `tests/test_database.py`,
`tests/test_captures.py`, `tests/test_retention_database.py`,
`tests/test_retention_cli.py` (schema-4 expectations only);
`tests/mutation_register.py` (Task 15.1 section); `docs/Database.md` and
`docs/API.md` (schema version and migration 004).

Unchanged: `pyproject.toml`, `uv.lock`, configuration, every capture,
event-capture, retention, backup, notification, API and deployment source.

## 6. Evidence

All on Windows 11, Python 3.13.14, SQLite 3.53.1, against synthetic temporary
databases and files only.

| Gate | Result |
|------|--------|
| Recognition + migration + affected suites (`test_recognition_schema`, `test_recognition_eligibility`, `test_recognition_jobs`, `test_database_migrations`, `test_database`, `test_captures`, `test_retention_database`) | 384 passed, 3 skipped (POSIX-only) |
| Retention CLI suite (after its fixture fix) | 163 passed |
| Complete suite (final run) | **3729 passed, 23 skipped, 0 failed** (34 m 38 s); baseline was 3483 passed, 20 skipped — the three new skips are the POSIX-only tests in §7 |
| `ruff check src tests` | all checks passed |
| `mypy` (strict, `src`) | no issues in 69 source files |
| Mutation register, new entries (`--only recognition`, `--only migration-004`) | 33/33 detected |
| Mutation register, pre-existing entries on the modified `database.py` | 5/5 detected |
| Register invariant (`test_the_mutation_register_is_part_of_the_repository`) | passed (363 entries, unique, each `old` applies once) |

### 6.1 Mutation register, guard by guard

| Entry | Guard | Killing test(s) | Unsafe behaviour without it |
|-------|-------|-----------------|-----------------------------|
| `recognition-enrols-any-origin` | exact `origin == MANAGED_ORIGIN` | `only_exactly_motion_origin_is_eligible` | manual/foreign captures inferred on |
| `recognition-interprets-undecodable-metadata` | metadata must decode to an object | `malformed_or_non_object_metadata_is_ineligible`, `one_malformed_row_does_not_stop` | one bad row aborts all enrolment |
| `recognition-watermark-ignored` | enrolment watermark | `before_the_watermark_is_ineligible`, `protected_evidence` | silent backfill of history and evidence |
| `recognition-watermark-boundary-exclusive` | inclusive boundary | `exactly_at_the_watermark_is_eligible` | documented and enforced boundary disagree |
| `recognition-enrols-media-retention-owns` | lifecycle exclusion at reconcile | `capture_with_a_lifecycle_row_is_ineligible` | reclaimed media queued |
| `recognition-enqueue-skips-the-lifecycle-recheck` | lifecycle re-check inside the insert | `lifecycle_row_committed_during_reconciliation` | intent committed mid-pass still gets a job |
| `recognition-relative-catalogue-path-accepted` | absolute path (retention) | `relative_path_is_ineligible_even_when_the_cwd` | target depends on the cwd |
| `recognition-traversal-accepted` | `..` rejection (retention) | `traversal_is_ineligible_even_when_it_resolves_inside` | traversal trusted because it resolves |
| `recognition-containment-abandoned` | containment (retention) | `path_outside_the_capture_root_is_ineligible`, `legacy_shaped_path` | any host file sent to a model |
| `recognition-symlinked-target-accepted` | symlink rejection (retention) | `symlinked_target_is_ineligible` | link redirects inference |
| `recognition-non-regular-target-accepted` | regular file (retention) | `non_regular_target_is_ineligible` | device/socket opened as a capture |
| `recognition-filename-disagreement-accepted` | filename/path agreement (retention) | `filename_that_disagrees_with_the_path_is_ineligible` | contradictory row trusted |
| `recognition-size-mismatch-accepted` | size agreement (retention) | `size_mismatch_is_ineligible` | a different file recognised under the id |
| `recognition-missing-media-misclassified` | missing-media classification (retention) | `media_is_missing_is_ineligible` | reclaimed media reported as unsafe |
| `recognition-enqueue-conflict-clause-removed` | `ON CONFLICT … DO NOTHING` | `interleaved_second_reconciler`, `concurrent_reconcilers` | overlapping reconcilers crash |
| `migration-004-job-uniqueness-removed` | `UNIQUE (capture_id, pipeline_version)` | `second_job_for_the_same_capture_and_pipeline_is_refused` | duplicate jobs |
| `migration-004-one-result-per-job-removed` | `job_id UNIQUE` | `second_result_for_one_job_is_refused` | conflicting results per job |
| `migration-004-pipeline-version-dropped-from-uniqueness` | version in the key | `different_pipeline_version_is_a_distinct_job` | reprocessing impossible |
| `recognition-reconcile-ignores-pipeline-version` | version filter on reconcile | `new_pipeline_version_is_queued_separately` | new version never queued |
| `recognition-claim-ignores-pipeline-version` | version filter on claim | `claims_are_separated_by_pipeline_version`, `second_pipeline_version_is_processed_independently` | job answered by the wrong pipeline |
| `recognition-claim-selects-outside-its-reservation` | select + update in one reserved transaction | `competing_writer_is_held_off_between_select_and_update` | two workers select one job |
| `recognition-valid-lease-stolen` | lease expiry comparison | `valid_running_lease_cannot_be_stolen`, `claimed_exactly_once` | live work taken over |
| `recognition-expired-lease-never-recovered` | expired-lease recovery | `expired_lease_is_recovered`, `interrupted_worker_leaves` | crashed work stranded |
| `recognition-retry-time-ignored` | `next_attempt_at` respected | `retry_is_not_claimed_before_its_time` | tight retry loop |
| `recognition-retry-exhaustion-removed` | exhaustion → terminal | `retryable_failures_become_terminal`, `persistent_crash_ends_failed` | last attempt requeued; only the schema `CHECK` refuses it |
| `recognition-exhausted-lease-never-ended` | expired final attempt ends | `expired_lease_on_the_final_attempt_ends_the_job` | worker-killing job runs forever |
| `recognition-writes-ignore-claim-ownership` | owner + attempt match | `worker_that_lost_its_lease_cannot_complete`, `claim_is_taken_during_inference` | stale worker overwrites a new claim |
| `recognition-stale-completion-still-inserts-a-result` | no insert unless the claim held | `worker_that_lost_its_lease_cannot_complete`, `stale_worker_cannot_overwrite` | stale result recorded |
| `recognition-missing-media-retried` | `media_missing` → `skipped` | `media_removed_after_reconciliation_is_skipped`, `error_disposition_is_exactly` | reclaimed media retried to failure |
| `recognition-runner-ignores-a-late-lifecycle-row` | run-time lifecycle check | `lifecycle_row_added_after_reconciliation_skips_the_job` | inference races retention |
| `recognition-adapter-runs-inside-a-write-transaction` | no transaction across the adapter | `no_write_transaction_is_open_while_the_adapter_runs` | inference holds the write lock |
| `recognition-result-and-success-committed-separately` | atomic result + `succeeded` | `result_and_the_succeeded_state_commit_together` | succeeded job with no result |
| `recognition-connections-may-write-capture-tables` | SQLite authorizer | `recognition_connections_cannot_write_capture_tables` | recognition can alter the catalogue |

Each key mutant was additionally applied by hand and its failure output
inspected, to confirm it dies for the intended reason rather than incidentally.

### 6.2 Findings during validation

- **An equivalent mutant.** `BEGIN IMMEDIATE → BEGIN DEFERRED` survived when
  first registered: every recognition transaction begins with a write (the
  claim opens with the exhausted-lease sweep), which takes the reservation
  anyway. The entry was replaced with one that releases the reservation between
  select and update, which is detected; the reasoning is recorded in the
  register.
- **Independent review (read-only subagent) before commit** found, and this
  task corrected: six existing tests in `test_captures.py`, `test_database.py`
  and `test_retention_database.py` still asserting schema 3; metadata whose
  JSON raises `ValueError` (integer-digit limit) or `RecursionError` escaping
  eligibility and aborting reconciliation; result integers above SQLite's range
  and NUL characters passing Python validation; `open_media` blocking on a FIFO
  (now `O_NONBLOCK`); two schema-test variants refused by a different constraint
  than the one they named; adoption verifying fewer recognition constraints than
  documented; stale schema-3 API examples; and an unmigrated-database test that
  did not build a schema-3 database.
- **The first complete-suite run** (3727 passed, 2 failed, 23 skipped) found
  one more schema-3 assumption the review had not: `test_retention_cli.py`
  fabricated a "newer" database as literal version 4, which is now current.
  The fixture now uses `CURRENT_SCHEMA_VERSION + 1`, so it cannot go stale
  again; the register's `the-schema-gate-accepts-any-version` mutation, which
  selects on that test, is still detected.
- **Kept as specified, recorded as an open decision:** a job run while
  retention holds a `pending_delete` intent is skipped; if retention later
  cancels that intent the capture is not requeued automatically
  (`docs/Recognition.md` §10).
- **Existing gap outside scope, not changed:** retention's own metadata decoder
  (`mgo.retention.repository._parse_origin`) catches only `JSONDecodeError`, so
  the same pathological metadata ends a retention run through the service's
  generic handler as `unexpected` rather than `catalogue_invalid`. Nothing is
  deleted either way; the task did not authorise changing retention.

### 6.3 Boundaries proven

- No capture, lifecycle, observation or migration-history row is inserted,
  updated or deleted by reconciliation or by runs, including failures and
  crashes (full-table snapshots before and after); media bytes, mtimes and the
  directory listing are unchanged.
- The authorizer refuses `UPDATE`/`DELETE`/`INSERT` on those tables and
  `DROP`/`CREATE` through a recognition connection.
- No `OperationLock` is acquired and no lock file is created.
- No directory enumeration primitive is reached during reconciliation.
- No recognition module directly imports camera, capture, event-capture,
  motion, operations, notifications, API, imaging, network, subprocess or
  locking modules; nothing outside the package references the fake adapter.
- No path or exception text appears in recognition rows or run reports.

## 7. Limitations

- Three POSIX-only tests (a real symlink refused by eligibility; a real symlink
  refused by `open_media` through `O_NOFOLLOW`; a real FIFO refused without
  blocking) are skipped on this Windows host, which cannot create symlinks or
  FIFOs and has no `O_NOFOLLOW`. The same refusals are proven on Windows through
  retention's filesystem seam and injected `ELOOP`/`fstat` results. No Linux
  host was available or authorised for this task.
- The enrolment watermark is a constructor argument; there is no configured
  value and no operator procedure yet.
- The reconciler re-examines every unqueued catalogue row on each pass
  (including rows that stay ineligible). At the catalogue's current size this is
  negligible; it is noted for the worker task.
- Nothing runs the reconciler or the runner. Choosing that, and its schedule,
  is a later task.

## 8. Deployment and rollback

Task 15.1 authorises no deployment. Schema 4 is refused by every schema-3 build
(proven), so a future deployment needs a fresh schema-3 recovery set taken
immediately beforehand and the existing schema-aware recovery procedure. See
`docs/Recognition.md` §14.
