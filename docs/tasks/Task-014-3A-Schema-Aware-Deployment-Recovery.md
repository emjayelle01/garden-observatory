# Task 14.3A — Schema-Aware Deployment Recovery

**Status: implementation complete, validated off-Pi, awaiting review.**

**Not merged. Not installed. No Raspberry Pi access. No production change. No
deployment. Task 14.3 deployment remains BLOCKED until this correction is
reviewed, merged and later installed.**

---

## 1. Scope

One narrow correction to `scripts/deploy/mgo-validate`: the deployment gateway
must never automatically restore a build that cannot open the database as it
then stands.

Nothing else. No database recovery capability, no new gateway action, no sudoers
change, no application code, no migration, no dependency, no configuration.

---

## 2. The Critical defect

Found during the Task 14.3 deployment design review, in the gateway shipped at
`d7a30bf9` — byte-identical to the copy installed on the Pi
(`3e26a7ce…0bf3e`), so this is the live behaviour, not a stale local copy.

`deploy-main`'s post-restart failure path restores the repository
unconditionally:

```
13. systemctl restart mgo.service          <- the boundary
14. await_recovery                          <- the new build starts and MIGRATES
15. restore_preview_state
16. final_verification
```

MGO applies migrations **first** in its lifespan, and a migration failure
propagates so the application refuses to start rather than serving against a
schema it cannot trust. So by the time step 14 has succeeded, the database is
already at the new schema.

If step 15 or 16 then fails, `fail_after_restart` restored the previous commit,
resynchronised its environment and restarted it. That build refuses a schema
newer than it supports — `IncompatibleSchemaError`, database left unchanged —
so it does not start, `await_recovery` fails, and the run ends `EX_ROLLBACK`.

**Production is left with the old code, a newer database and a service that will
not come up.** Recovering needs a manual database restore, which has never been
rehearsed here, and the gateway has no rollback action at all.

For the concrete case: the Pi runs `48bdaf3`, which supports schema 2. The
target `d7a30bf9` supports schema 3 and ships migration 003.

### Reproduced first

A test was written before the correction and run against the unmodified
gateway. Baseline schema 2, actual schema 3, a post-restart verification
failure — and the gateway took the unsafe path:

```
AssertionError: ROLLBACK_CALLED
  RESTART_CALLED
  PREVIEW_RESTORE_CALLED
```

It exercises the real failure boundary, not a helper's return value: the
rollback, restart and preview-restoration seams are doubled so that reaching
them is itself the evidence.

---

## 3. Architectural decision

The gateway gains **read-only schema awareness**, and nothing else.

It was tempting to give it a database-restoring action, so a failed deployment
could recover on its own. That was rejected. It would put root-privileged code
that overwrites the production database into the most security-sensitive file in
the repository, to solve a problem that only arises during a schema-changing
deployment — a moment when an operator is present by policy anyway. The far
smaller change is to stop the gateway *creating* the incompatible pair, and
leave recovery where it already lives: a manual, deliberate procedure.

So the rule is a refusal, not a repair.

### The compatibility rule

Repository rollback proceeds **only** when the recorded database schema equals
the schema the previous build supports. Three cases refuse, for three different
reasons:

| Case | Why it refuses |
| --- | --- |
| `actual > baseline` | The defect this exists for: the database has moved beyond the previous build |
| `actual < baseline` | Nothing in this architecture explains a database going backwards mid-deployment. Not understood is not safe |
| unknown / malformed | A probe that could not answer is not a probe that answered yes |

**The guarantee is narrow, and the code says so.** Equal schema versions mean
the previous build will *open* this database. They do not mean arbitrary data
transformations are reversible. A migration that rewrote rows within one
version, or a build that changed how it reads existing rows, is outside what a
version number can promise — and outside what this gateway claims. This is not
a migration-compatibility engine and must not grow into one.

---

## 4. Implementation

Four new shell functions and one new exit code.

| Piece | Purpose |
| --- | --- |
| `MGO_SCHEMA_PROBE` | The read-only probe program. `mode=ro`, `PRAGMA query_only = ON`, one `SELECT MAX(version) FROM schema_migrations`. Prints one integer; every failure exits non-zero with stderr discarded |
| `build_supported_schema` | The schema a checked-out build supports, asked of that build's own interpreter |
| `database_schema_version` | What the live database records. Non-zero means "unknown", never a guess |
| `repository_rollback_is_safe` | The compatibility rule, in isolation |
| `refuse_rollback_if_schema_advanced` | Returns cleanly when restoration is safe; otherwise exits and never returns |
| `EX_MANUAL_RECOVERY=79` | A distinct classification: rollback refused, not rollback failed |

**`immutable=1` is deliberately not used.** It asserts the file cannot change,
which is untrue of a live database being read while the service may still be
serving from it, and would licence SQLite to ignore concurrent WAL state.

Both probes run as the **unprivileged runtime account** through `runuser` with
`env -i` and a fixed minimal environment — the same pattern
`require_runtime_can_execute` already uses, and for the same reason: an
inherited `PYTHONPATH` or `VIRTUAL_ENV` could answer for a different application
than the one about to run. The gateway starts under sudo; that is not a reason
to do database work as root. The database path is resolved by the application's
own configuration loader and can never be supplied by a caller.

### Where the facts are established

```
require_uv_available
baseline_schema  <- HERE: before the fetch, while the previous build is on disk
git fetch / merge --ff-only
uv sync --frozen
require_runtime_can_execute
target_schema    <- HERE: before the restart, while ordinary rollback is safe
systemctl restart          <- the database moves at this point
```

The baseline value can only be read before the fast-forward: afterwards nothing
on disk can answer what the previous build supported. A build that cannot answer
is a **precondition** failure, before any mutation. A *target* that cannot answer
fails through `fail_before_restart`, where the database has not yet been exposed
to it and the ordinary restoration is still correct.

### The refusal

`fail_after_restart` gains one line. The refusal itself lives in its own
function, which keeps that path independently testable and keeps
`fail_after_restart` short enough that the existing structural tests around it
still read the way they were written.

On refusal: nothing is restored, resynchronised, restarted or written. The
deployed build is the one that matches the database, so it stays. A healthy
service is left running; an unhealthy one is left alone rather than flapped.
Two bounded sentences state whether the build in place supports the database,
and the exit message says rollback was **REFUSED** — never that anything was
restored. No path, value or exception appears in any of them.

---

## 5. Tests

**+40 tests** in `tests/test_deployment_gateway.py`, 780 → 820. The module is
**purely additive**: the diff removes not one line.

* the Critical defect, reproduced and now prevented;
* the control — an unchanged schema still performs the ordinary rollback, so
  the correction cannot pass by refusing everything;
* fail-on-call seams proving the advanced-schema path never reaches
  `restore_checkout`, `sync_environment`, `restart_service`, `await_recovery`,
  `restore_preview_state` or `rollback_repository`;
* fail-on-call seams for `sqlite3`, `cp`, `mv` and `rm`, so no database write
  can be reintroduced unnoticed;
* fail-closed for empty, non-numeric, decimal, negative and whitespace answers,
  on both the baseline and actual halves;
* a lower schema refused, with the reasoning stated;
* the compatibility rule exercised directly across its whole domain;
* the probe asserted read-only three ways, with `immutable=1` and every
  write-shaped verb excluded;
* the probes asserted unprivileged, `env -i`, and non-numeric-refusing;
* the ordering properties — baseline before any mutation, target before the
  restart, each failing through the correct path;
* every post-restart call site passing the baseline, counted;
* `fail_before_restart` unchanged;
* no new public action, no database path argument;
* the refusal message asserted to disclose nothing and to claim nothing.

One authoring note worth recording. The gateway sets `errexit` when sourced, so
a test that captured `$?` from an honestly-failing function aborted the harness
before it could print. The compatibility test uses `&&`/`||` instead. A test
that cannot report a legitimate negative result is not testing the negative.

---

## 6. Mutation coverage

The register targets this shell asset heavily, so no artificial entry was added.
**The total stays 254.**

One entry went stale and was **re-anchored, not dropped**:
`final-verification-removed` pinned the `final_verification` call site including
its argument list, and that list gained `"$baseline_schema"`. A mutation whose
anchor no longer applies has silently stopped testing anything, which is worse
than one that fails loudly.

---

## 7. Validation

See the completion report for the run output: `uv sync --frozen`, `ruff`,
`mypy`, `bash -n` on the gateway, the full gateway module, the complete suite
and the complete mutation register.

---

## 8. Explicitly not done

No database recovery capability. No `restore`, `rollback-database` or any other
new action — the accepted set is still exactly `show-approval`, `deploy-main`,
`restart-api`. No database file copied, moved, overwritten or deleted. No WAL or
SHM file removed. No backup selected, taken or changed. No sudoers change. No
systemd change. No caller-supplied database path. No way to bypass the schema
check. No migration added and 003 unchanged. No dependency. No application,
retention or database code. No configuration change. Retention and
`event_capture` remain disabled and no retention bound was chosen. No scheduler
and no destructive HTTP endpoint.

**No Raspberry Pi access. No deployment. No production change. No approval-SHA
change. No backup or restore run. No media read or deleted.**

---

## 9. Relationship to Task 14.3

Task 14.3 — Retention Deployment and Read-Only Production Validation — remains
**blocked**. This correction removes the mechanism by which a failed deployment
would have created an unrecoverable code/database pair. It does not remove the
need for the rest of the recovery preparation the 14.3 design identified: a
fresh backup, a `restore-test` against that exact set, an operator present for
the window, and a pre-approved manual recovery runbook.

It also changes `main`. The SHA proposed for approval in the Task 14.3 design,
`d7a30bf985a4340cdb99a8229fc4c97027f0966c`, is therefore **obsolete once this
is merged** and must not be approved; the deployment target becomes the new
`main` tip, which is also the first build whose deployment is protected by this
correction.
