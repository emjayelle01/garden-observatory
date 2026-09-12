# Task 15.1A — Independent pre-merge review and native-Linux validation of PR #19

**Status: validation complete. PR #19 remains open and unmerged.** Corrections
were made on the existing branch `task-015-1-recognition-job-foundation`.

**No production change.** No deployment, no gateway use, no systemd unit, no
production database, configuration, media or API access, no service or timer
control, no package installation, no environment modification. The Raspberry Pi
was used only as a native-Linux test host, inside one temporary stage under
`/tmp` that was removed afterwards.

---

## 1. Scope

Task 15.1 built the model-independent recognition-job foundation (migration 004,
`src/mgo/recognition/`). Task 15.1A independently reviewed that pull request,
executed the three POSIX-only tests that skip on Windows on real Linux, settled
the two decisions Task 15.1 had left recorded as open, and corrected the defects
the review found.

## 2. Preflight

| Item | Value |
|------|-------|
| `main`, `origin/main`, live GitHub `refs/heads/main` | `fef11379fa956d68c1ac3184d63f4b6d9b0e54de` (all three equal) |
| Branch head, local and remote | `60a406fc54e3c86cd40057db63ce293365871c24` (equal) |
| PR #19 | open, base `main`, MERGEABLE/CLEAN, not merged, one commit, 23 files |
| Working tree at start | clean; no stashes, one worktree, no in-progress Git operation |

## 3. The two approved decisions

### 3.1 A cancelled `pending_delete` (approved)

A job processed while a lifecycle row exists becomes terminal
`skipped` / `media_missing`. If retention later cancels that deletion, nothing
reopens or resets the job, terminal history stays immutable, and reprocessing is
an explicit operator action or a new pipeline version.

Verified in code: the only transitions that revive a job are `_CLAIM_SQL`
(guarded by the state the claim query observed, which is only `pending` or
`running`) and `_RETRY_SQL` (guarded by `state = 'running'` plus lease owner and
attempt number), so no terminal state can move; and the reconciler's
`NOT EXISTS` excludes a capture that has *any* job for the pipeline version,
whatever its state, so no replacement job is created.

Previously untested — the review found `DELETE FROM capture_media_lifecycle`
appeared nowhere in the suite. Task 15.1A added
`test_a_cancelled_deletion_intent_does_not_reopen_a_skipped_job` and the
mutation entry `recognition-terminal-jobs-stop-blocking-re-enrolment`, and
recorded the approval in `docs/Recognition.md` §10 and §15.

### 3.2 The enrolment watermark (approved)

The reconciler must be *given* an aware UTC watermark. There is no default, no
configuration key, and no implicit backfill.

Verified: no watermark default exists anywhere in `src/`; the parameter is
keyword-only on both the reconciler and the rules; a naive value is refused.
The review found that nothing pinned the *absence* of a default — adding one
would have left the whole suite green while silently enrolling all history.
Task 15.1A added `test_the_enrolment_watermark_must_be_supplied`,
`test_a_reconciler_without_a_watermark_cannot_be_built`,
`test_a_naive_watermark_is_refused_by_the_rules_directly`, a naive-watermark
guard in `evaluate_eligibility` itself (previously a bare `TypeError` from the
comparison), and two mutation entries.

## 4. Findings and dispositions

| # | Finding | Severity | Disposition |
|---|---------|----------|-------------|
| 1 | `_CLAIM_SQL` did not clear `error_category`, so a reclaimed job read as `running` while carrying the previous attempt's error | low, **real code path** | **Fixed** + test + mutation entry |
| 2 | Timestamp `CHECK`s constrained shape only: a BLOB of the same characters, or a NUL-padded value whose `length()` still read 32, was storable and would never come due | medium (direct SQL only) | **Fixed**: `typeof` + byte-length bounds on all six timestamp columns, two refusal tests, two mutation entries |
| 3 | Adoption did not verify `recognition_results` integer bounds, though §15 claimed it did | medium-low | **Fixed**: five fragments added, adoption variant test, mutation entry |
| 4 | Docs claimed the database enforces "a result only for a succeeded job"; no cross-table `CHECK` can | medium-low (doc) | **Fixed**: §4, §9 and the migration header now state what the schema enforces versus what the single writer guarantees, including the `superseded` implication for a later task |
| 5 | Nothing pinned the absence of a watermark default | medium-high | **Fixed** (§3.2) |
| 6 | "Cancelled `pending_delete`" behaviour untested | medium | **Fixed** (§3.1) |
| 7 | The `ValueError` arm of `media_refusal` was untested; narrowing it to `OSError` left the suite green | medium-low | **Fixed**: parametrised over both arms, mutation entry |
| 8 | 34 schema-variant rows asserted bare `IntegrityError`, so a variant refused for a different class of reason would pass silently | low-medium | **Fixed**: `match="CHECK"` on both parametrised tests |
| 9 | `test_concurrent_reconcilers_create_each_job_exactly_once` could pass with a hung thread | low | **Fixed**: asserts every thread finished and every reconciler returned |
| 10 | The schema-3 health-check test called bare `load_config()`, inheriting `MGO_CONFIG_PATH` from the developer's environment | low-medium | **Fixed**: explicit tracked configuration path |
| 11 | Foreign keys hold only while `PRAGMA foreign_keys` is on (the bare `sqlite3` CLI defaults it off) | low-medium (operational) | **Documented** in §4 |
| 12 | Legacy adoption can be satisfied by required constraint text hidden inside a **double-quoted** SQL string literal, so a constraint-free table is adopted and then trusted | medium | **Not fixed here**: `_normalised_definition` is Task 14.1 code shared with the version-3 lifecycle shape, so changing it would alter existing adoption behaviour, which this task forbids. Raised as a separate task. Reachable only by presenting a hand-crafted *unversioned* database |
| 13 | Retention's own metadata decoder catches only `JSONDecodeError` (recognition's equivalent was already widened in Task 15.1) | low (pre-existing) | **Not fixed**: outside this task's scope; already raised separately |

Also reviewed and found sound, with no change required: a shape-valid
non-instant (`month 99`) remains storable by direct SQL — no writer can produce
one and such a row simply never comes due; falsely strict adoption cases (a
reordered vocabulary, a `CREATE UNIQUE INDEX` instead of an inline `UNIQUE`) all
fail closed; temporal coherence between timestamps is unconstrained but
unreachable; `COLLATE NOCASE` on `state` would widen the vocabulary if an
operator hand-built such a table.

## 5. Windows evidence (final tree)

| Gate | Result |
|------|--------|
| Focused: recognition ×3, migrations, database, captures, retention database, retention CLI | 556 passed, 3 skipped (POSIX-only) |
| `ruff check src tests` | all checks passed |
| `mypy` strict | no issues in 69 source files |
| Mutation register, recognition section | 35/35, then 5/5 for `migration-004`, 1/1 for the new adoption entry |
| Mutation register, affected pre-existing | `version-three-adoption` 4/4, lifecycle identity 1/1, retention CLI schema gate 1/1, `migration-003` 1/1 |
| Register invariants | 371 entries, unique, every `old` applies exactly once |
| Complete suite | **3739 passed, 23 skipped, 0 failed** (33 m 26 s) |

Two **equivalent mutants** were found and replaced rather than left to pass as
coverage: Task 15.1 had already recorded `BEGIN IMMEDIATE → DEFERRED`, and this
task found that removing only the timestamp *length* bound survived, because
`typeof` still refused the BLOB case and no test put a NUL-padded value in
`next_attempt_at`. The test now covers both columns and the register pins each
half of the hardening separately.

Hand-inspection: every high-risk mutant was applied by hand and its failure
output read, to confirm it dies for the intended safety reason — exact-origin,
lifecycle exclusion, containment, symlink, size, conflict clause, lease
ownership, stale completion, adapter transaction boundary, atomic result
completion, capture-write denial, and all six new Task 15.1A entries.

## 6. Native-Linux evidence (`mgo-core`)

PI_RERUN_PLACEHOLDER

## 7. Limitations

- The Windows full-suite run against the *as-submitted* head was stopped
  deliberately at roughly 45% once it became the only obstacle to the migration
  correction; that head had already passed the complete suite (3729 passed, 23
  skipped) and its focused modules were re-proven on native Linux. The
  authoritative result is the final run in §5.
- The Pi is a test host only. Nothing in this task validates recognition against
  real capture media, a real model, or production data — by design.
- Finding 12 remains open in a separate task; it does not affect a database this
  application created.

## 8. Merge recommendation

MERGE_RECOMMENDATION_PLACEHOLDER
