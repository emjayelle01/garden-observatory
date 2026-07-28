# Task 10 — Operations foundation

## Status

**Implementation complete; pre-merge validation cleanup correction in
progress.**

A repository review of the five implementation commits found nine genuine
defects — one requirement omitted outright, several checks that were weaker
than they appeared, and three places where a failure was reported as a
success. They are recorded in [§ Repository review](#repository-review) and
were corrected in commits after `a5c9776`.

A final re-review then found one further class of problem — source *identity*
— recorded in [§ Final re-review](#final-re-review) and corrected in commits
after `a020f83`.

A review of that correction found three narrowly scoped **ordering** defects
within it, recorded in [§ Identity-ordering review](#identity-ordering-review)
and corrected in commits after `52c6d31`.

The source-identity work then passed review, and a final operational review
found one defect at the **operator boundary** rather than inside the
implementation: the manual wrappers did not select the production
configuration. It is recorded in
[§ Operator configuration-default review](#operator-configuration-default-review)
and corrected in a commit after `d991d3b`.

A review of the *validation procedure itself* then found that it installed
systemd artefacts and never removed them, recorded in
[§ Pre-merge validation cleanup review](#pre-merge-validation-cleanup-review)
and corrected in a commit after `7e4fb63`.

| Gate | Outcome |
| ---- | ------- |
| Architecture review | Complete |
| Implementation | Complete |
| Repository review | Complete — nine findings, corrected |
| Final re-review | Complete — source-identity findings, corrected |
| Identity-ordering review | Complete — three findings, corrected |
| Local static and automated validation | Passed |
| Raspberry Pi validation | **Not performed** — procedure prepared for Matthew |

Readiness for Pi validation is **not** claimed until the corrections have
passed review.

Delivered as decided below:

- `src/mgo/operations/` — `errors.py`, `events.py`, `locking.py`,
  `source_identity.py`, `backup.py`, `backup_cli.py`, `support_bundle.py`,
  `support_bundle_cli.py`;
- `SYSTEM_BACKUP_DIRECTORY` in `mgo.core.config`;
- `scripts/operations/backup-database.sh`,
  `scripts/operations/create-support-bundle.sh`;
- `scripts/deploy/mgo-backup.service.template`,
  `scripts/deploy/mgo-backup.timer`,
  `scripts/deploy/garden-observatory.logrotate`,
  `scripts/deploy/verify-operations.sh`;
- installer integration in `scripts/deploy/install-service-identity.sh`;
- `docs/Operations.md`, plus `README.md`, `scripts/README.md`,
  `docs/Service-Identity.md` and `docs/Database.md` updates;
- 513 added tests across `tests/test_operations_events.py`,
  `tests/test_operations_backup.py`, `tests/test_operations_support_bundle.py`,
  `tests/test_operations_deployment.py` and
  `tests/test_operations_source_identity.py` (262 with the implementation, 198
  with the repository corrections, 37 with the source-identity hardening, 16
  with the identity-ordering correction).

`scripts/deploy/mgo.service.template` is **byte-for-byte unchanged**, asserted
by a test that diffs it against `main`. No dependency was added; `pyproject.toml`
and `uv.lock` are unchanged. No migration, no configuration field, no API
endpoint and no dashboard change.

Validation on the development workstation after the corrections: `ruff` passed,
`mypy src` passed (48 source files), `pytest` **1134 passed / 12 skipped**
(baseline 633 + 513 added). Every skip is a POSIX mode-bit, POSIX-behaviour or
symlink-creation assertion that cannot be made on Windows.

Two real defects were found and fixed during implementation, both caused by
Windows resolving a rooted POSIX path against the current drive:

- the `restore-test` protected-location guard compared `/var/lib/...` against a
  resolved path carrying a drive letter, so it never matched and the guard was
  inert wherever the tests run. Comparison is now drive-agnostic;
- the SQLite backup destination inherited WAL from the copied header, leaving
  `-wal`/`-shm` sidecars, so the single published file was an incomplete
  database whose checksum described only part of its contents. The copy is now
  collapsed with `journal_mode=DELETE`, and a test asserts the sidecars are
  absent.

No pull request is opened, nothing is merged, and no Raspberry Pi was accessed.
No Task 11 or Task 12 work has been started.

## Repository review

Nine findings, all genuine. Each is stated as the defect, why it mattered, and
what was changed.

### 1. Configuration backup was omitted

The plan requires "the database and configuration must be backed up". Only the
database was. A restore would have recovered the observation history onto a
machine whose configuration was gone — the operator would have had to
reconstruct `/etc/garden-observatory/mgo.toml` from memory during an incident.

**Corrected.** A recovery set is now three files. See
[§Complete backup sets](#complete-backup-sets).

### 2. Manifests were not bound to the artefacts they describe

`BackupManifest.from_dict()` coerced fields with `str()`/`int()` and caught only
`KeyError`/`TypeError`/`ValueError`, so a manifest could be *parseable* while
meaningless: a boolean where a size belonged, a negative version, a "checksum"
that was arbitrary text, a "filename" that was a path.

Worse, `verify_backup()` compared row counts by iterating **only the keys the
manifest happened to carry**. A manifest with `"table_row_counts": {}` therefore
verified successfully against *any* database — the single most important
verification silently did nothing.

**Corrected.** Structural validation of every field, and binding comparison of
every recorded value against the artefact. Row counts are compared exactly, in
both directions, over the authoritative table set.

### 3. Restore testing permitted a missing manifest

`restore_test()` recorded `row_counts: "skipped (no manifest)"` and still
returned `ok`. A rehearsal that quietly skips its most important assertion is
worse than no rehearsal, because it produces a passing result an operator will
trust.

**Corrected.** Full set verification runs before anything is copied; the
"skipped" path no longer exists.

### 4. `--no-unit` could install an unusable backup service

The installer failed a broken virtual environment only when the API unit was
selected. Once Task 10 could install `mgo-backup.service`, `--no-unit` skipped
`mgo.service` while still installing a backup service against the same
environment — and printed "no systemd unit will be pointed at this virtual
environment", which had become untrue.

**Corrected.** Per-target validation: `uvicorn` for the API unit, `python` for
the backup unit. See [§Installer flag semantics](#installer-flag-semantics).

### 5. Timer activation failures were hidden

`systemctl enable` and `systemctl start` both ended in `|| true`, and the script
then printed "enabled and started" unconditionally. A timer that failed to
enable was reported as enabled: the operator would believe backups were
scheduled when nothing was scheduled at all. For a task whose entire purpose is
"backups happen", this was the most serious finding.

**Corrected.** Every step is checked, names itself on failure, and the resulting
state is verified rather than inferred.

### 6. Installer retention validation was weaker than the runtime

The installer accepted any positive integer; the Python CLI enforces
`1..3650`. `--keep 100000` would have written a unit guaranteed to fail every
scheduled run — a backup service that never once succeeded.

**Corrected.** The installer applies the same bound.

### 7. Diagnostic HTTP could use a proxy or follow a redirect

Only the *initial* URL was validated. `urllib`'s default opener honours
`HTTP_PROXY`/`ALL_PROXY` and follows 3xx responses, so a "loopback only" request
could have been sent to a proxy or redirected to an external host — with the
validation having passed either way. The privacy boundary was one environment
variable from being bypassed.

**Corrected.** Explicit opener with proxies disabled and redirects refused;
literal loopback addresses only.

### 8. Some non-zero commands were treated as successes

`collect_service_status()` returned `available: true` with an empty property set
after `systemctl` failed. "The service exists and told us nothing" is a
different — and far more alarming — diagnosis than "systemctl could not answer".
`collect_journal_disk_usage()` had the same problem.

**Corrected.** Return codes are inspected; failures carry a bounded, sanitised
detail and make the bundle partial.

### 9. Storage aggregation was unbounded

`Path.rglob("*")` walks an entire tree with no limit. The captures directory is
flat *today*; a diagnostic tool must not depend on that remaining true, and must
not spend minutes stat-ing a media archive on a device that is already unwell. A
symlinked directory could also have turned a capture scan into a filesystem
walk, or looped forever.

**Corrected.** Bounded iterative traversal that never descends a symlink and
reports truncation.

## Final re-review

One further class of defect, found after the nine above were corrected. All
three instances share a root cause: **trusting a path across two operations**.

### 10. Symlink refusal was check-then-open

Both the configuration and the database were guarded like this:

```python
if path.is_symlink():
    raise ...
handle = path.open("rb")
```

That is correct for a path that does not change, and only for that. Between the
check and the open, a path can be replaced — what was validated as a regular
file can be opened as a symlink pointing somewhere else entirely. This is the
classic check-to-open (TOCTOU) gap.

The `fstat` added in the previous round did **not** close it. `fstat` proves the
*opened object* is a regular file; it says nothing about whether a symlink was
followed to reach it. Two different questions, and only one was being asked.

**Corrected.** Refusal now happens at the open itself — `O_NOFOLLOW` on Linux —
and the object opened is proven to still be the object the path names.

### 11. The configuration was read twice

The configuration was parsed once to resolve the database path, and then opened
again later to be copied into the recovery set. A concurrent administrative
replacement between those two reads would have produced a set pairing a database
chosen from **one** configuration version with bytes from **another** — a
recovery set describing a pairing that never existed, and one that would look
perfectly consistent to every verification check, because each file is
individually valid.

**Corrected.** The configuration is read exactly once into an immutable
snapshot; the bytes are authoritative for the whole run.

### 12. The database had the same identity gap

Between validation and SQLite's own open, the database path could be repointed.
SQLite opens **by name**, so the tooling cannot simply hand it a descriptor.

**Corrected.** An identity anchor is opened first and held across the connect —
holding it is what prevents the inode being recycled — and the path is
re-verified immediately afterwards, before a page is copied.

### What was deliberately not done

- **`immutable=1`** was rejected as a way to sidestep the database race: the
  production database is live and carries WAL state, so telling SQLite it cannot
  change would produce an *inconsistent* read rather than a safe one.
- **A `/proc/self/fd` URI** was rejected: it would need proof that WAL sidecars
  stay correctly associated, that committed WAL data is still included, and that
  the source stays read-only. The identity-anchor design achieves the same
  guarantee without that burden of proof.
- **The Linux guarantee was not weakened** to make Windows tests convenient.
  Windows has no `O_NOFOLLOW`, so its fallback *detects* a substitution rather
  than *preventing* it. That difference is documented in the module and asserted
  by a test, rather than glossed over.

## Identity-ordering review

The source-identity work above was structurally right but got three orderings
wrong. Each is small; two of them meant a guarantee that had been *claimed* was
not actually being enforced.

### 13. The fallback discarded its pre-open observation

On a platform without `O_NOFOLLOW`, `open_no_follow()` took an `lstat`, rejected
a symlink, and then **threw the observation away**.

That left a regular file replaceable by *another regular file* between the
`lstat` and the `os.open`. From the open onwards, the descriptor and every later
observation describe the replacement — and they all agree with each other, so no
later check can see anything wrong. Only the discarded observation disagreed.

This also made the previous documentation and completion report **inaccurate**:
both stated that pre-open, descriptor and post-open identities were compared.
Two of the three were; the pre-open one was collected and dropped. That wording
is corrected wherever it appeared.

**Corrected.** `open_no_follow()` returns an `OpenedSource` carrying the
observation, and `require_opened_identity()` compares it against the
descriptor's own `fstat`. Applied to both configuration reading and database
anchoring.

### 14. The descriptor closed before the final comparison

`read_regular_file()` closed the descriptor in a `finally` block and *then* ran
the post-read `lstat` and identity comparison. Once closed, an unlinked inode
can in principle be recycled before the comparison runs.

The same module explains, in `SourceAnchor`, that holding the descriptor open is
precisely what prevents inode recycling — so the ordering contradicted the
module's own stated design.

**Corrected.** All comparisons now happen while the descriptor is open; it is
closed in a `finally` afterwards, on every path.

### 15. The database was verified once, not throughout

The anchor was verified immediately after SQLite connected, which proves the
right file was *opened*. Nothing re-checked it after the online copy completed,
so a path replaced during the read had no fail-closed result.

**Corrected.** A second `anchor.verify()` runs after
`source_connection.backup()` and before the temporary copy is accepted.

### How these were confirmed

Each fix was **mutation-tested**: the correction was reverted in place and the
new tests were confirmed to fail, then restored and confirmed to pass. That
exercise found a real weakness in one of the new tests — the descriptor-ordering
test originally compared the *first* `lstat` with the *first* `close`, which the
fallback's pre-open `lstat` satisfied on Windows regardless of the bug. It now
compares the **last** `lstat` with the **first** `close`, and fails against the
reverted code as it should.

## Operator configuration-default review

The source-identity implementation passed review. One operational defect
remained, and it was not in the implementation at all — it was at the boundary
where an operator meets it.

### 16. The manual wrappers did not select the production configuration

`mgo-backup.service` was correct, and had always been:

```ini
Environment=MGO_CONFIG_PATH=/etc/garden-observatory/mgo.toml
ExecStart=... backup --config /etc/garden-observatory/mgo.toml ...
```

The manual wrappers — `scripts/operations/backup-database.sh` and
`scripts/operations/create-support-bundle.sh` — supplied **neither**. They
forwarded arguments to Python and nothing else, and `resolve_config_path()`
deliberately falls back to the tracked development configuration when no
explicit path and no environment value are present.

`sudo` clears the environment, so this was not a corner case: it was what every
documented manual command did. An operator following the documented procedure

```bash
sudo -u mgo /opt/garden-observatory/scripts/operations/backup-database.sh backup
```

would have reached `config/mgo.toml` inside the checkout, and could therefore:

* select a **development database path** rather than the production database;
* fail against development-relative storage paths that do not exist on the Pi;
* create a **support bundle describing the wrong configuration** — the wrong
  storage paths, the wrong camera, preview, motion and notification settings,
  and unknown-key redaction applied to a file nobody asked about;
* believe all of it, because every one of those outputs is internally
  consistent and reports success.

**Scheduled operation was production-safe. Manual operation was not.** That
distinction is the whole finding: the timer would have kept producing correct
recovery sets while an operator investigating an incident by hand was handed a
picture of a machine that was not the one in front of them.

**Corrected.** Each wrapper now exports the canonical production path before
invoking Python, and only when the variable is genuinely unset:

```bash
if [[ ! -v MGO_CONFIG_PATH ]]; then
    export MGO_CONFIG_PATH="/etc/garden-observatory/mgo.toml"
fi
```

`-v` tests whether the *name* is set. The shorter
`: "${MGO_CONFIG_PATH:=...}"` would also have replaced a deliberately empty or
whitespace-only value, which the application treats as a configuration error and
must keep treating as one — silently repairing a broken unit or a typo in a
profile, by pointing the tooling at the live system, would hide exactly the
fault it should surface.

### Where the default belongs

In the wrappers, not in `resolve_config_path()`. Moving it into the library
would make every developer test run and every `uv run` on the development
machine reach for a path that does not exist there, and a platform-conditional
default would be untestable on one of the two platforms. Direct Python execution
with no explicit path and no environment value still loads `config/mgo.toml`.

The wrappers can no longer be described as containing *no* logic. They make one
**execution-environment** decision — which configuration an operator means —
and the comments, `--help` output and `scripts/README.md` now say so. Backup,
verification, retention, redaction and collection decisions remain in Python.

`scripts/deploy/mgo-backup.service.template` was **not** modified: it was
already explicit, it invokes the interpreter directly rather than the wrapper,
and a test now asserts both, so the scheduled path cannot come to depend on
wrapper behaviour.

### How this was confirmed

The tests run the **real wrappers** under `bash` against a fake
`.venv/bin/python` that reports the argument vector and the `MGO_CONFIG_PATH` it
was handed, covering unset, custom, empty and whitespace-only values, argument
forwarding, `--config` precedence, exit-status preservation, the missing
interpreter error and that the wrapper writes nothing. Nothing touches `/etc`,
`/var`, `/opt` or the production virtual environment.

## Pre-merge validation cleanup review

The implementation and the operator-wrapper correction both passed review. The
remaining defect was in the **procedure written to validate them**.

### 17. Pre-merge validation left installed Task 10 units behind

The Raspberry Pi validation procedure:

* installed `mgo-backup.service`;
* installed **and enabled** `mgo-backup.timer`;
* installed `/etc/logrotate.d/garden-observatory`;
* tested reboot persistence, deliberately proving the timer survives a restart;
* and then instructed the operator merely to `git checkout main`.

Every one of those steps is correct in isolation. Together they end with an
**enabled timer** whose `ExecStart` is

```text
/opt/garden-observatory/.venv/bin/python -m mgo.operations.backup_cli
```

After the checkout, `mgo.operations` does not exist — it arrives with Task 10
and pre-Task-10 `main` has never had it. The next scheduled 02:30 run would
therefore fail, on a Pi that had been deliberately returned to a known-good
state, from a branch that was never meant to install anything permanently. The
reboot-persistence check makes it worse rather than better: it proves the timer
outlives everything the operator does to the checkout afterwards.

The root cause is a category error worth naming, because it is not specific to
this task: **installed systemd units and logrotate policies live in `/etc` and
are outside Git.** `git checkout` reverts the code they point at; it does not
revert them. Rollback of a Git branch and rollback of a system installation are
two different operations, and the documentation had collapsed them into one.

The statement

```text
Before merge — rollback is returning to main.
```

was true before any installation validation and false after it. It is not that
the sentence was imprecise; once §13.4 has run, it is wrong.

**Corrected.** The procedure now has three new steps and the rollback section
has three explicit states:

* **§13.1a** records whether the three artefacts already exist **before
  anything is installed**. This is what makes the later removal safe: validation
  removes only what it proved was absent and then installed itself. If an
  artefact already exists the operator stops, records it, and does not delete
  it — it may predate this branch or belong to another deployment.
* **§13.14** is a mandatory pre-merge cleanup that runs after every check
  including the reboot test: disable and stop the timer, remove the two unit
  files and the logrotate policy, `daemon-reload`, `reset-failed`, then verify
  the removed state with `test !`.
* **§13.15/§13.16** restore the pre-validation service state and only then
  return to `main`, ending with
  `systemctl list-timers --all | grep -F mgo-backup` to prove nothing orphaned
  survived, and an endpoint sweep to prove the API is unaffected.

### Removing the tooling is not deleting the data

Stated explicitly in the procedure and asserted by tests. The cleanup touches
`/etc/systemd/system` and `/etc/logrotate.d` only. It never removes
`/var/backups/garden-observatory`, any recovery set inside it, the production
database, the production configuration, captures, observations, or
`mgo.service`. The **validated** recovery set stays deliberately: it is the
evidence that the backup path worked on real hardware, and it is protected by
the same rule that protects a pre-existing one.

### Three rollback states

`docs/Operations.md` §14 now distinguishes them rather than collapsing them:

| State | Rollback |
| ----- | -------- |
| Before any Pi installation validation | `git checkout main`, because nothing was installed |
| After pre-merge installation validation, before merge | The full §13.14 cleanup, then return to `main` |
| After merge and deployment | The same unit removal, plus `git revert` and reinstalling the reverted service-identity state |

### Permanent installation is a post-merge act

The procedure now closes by saying what happens next: the Pi returns to clean
`main`, the units are gone, the recovery set remains, evidence is returned,
repository review happens, and only then may a pull request be created. The
installation that is *meant* to persist is the one that follows the merge.

## Authoritative definition

> **Operations — Create systemd service, log rotation, backup and diagnostic
> scripts.**

The corresponding work-breakdown items:

> **OPS-01 — Create systemd units.**
> Output: API and worker service templates with restart policy.
>
> **OPS-02 — Create backup and diagnostic scripts.**
> Output: Database backup, restore test and support bundle.

The project plan additionally requires that services restart automatically with
sensible backoff; that database integrity checks and backups are scheduled; that
logs are useful and bounded; that operational logs are structured with
timestamp, service, severity, event ID and error code; that a diagnostic bundle
carries logs, a configuration summary and recent health data **without** copying
the media archive; that faults are diagnosable from the dashboard and logs with
no monitor and keyboard attached; that the database and configuration are backed
up; that backup and restore are tested; and that unnecessary SD-card writes are
avoided.

## Starting point

| Item | Value |
| ---- | ----- |
| Branch | `main` |
| HEAD / `origin/main` | `0ef3d04047faef119399c46182103e6f478b8a3a` |
| Working tree | clean |
| Baseline `ruff check .` | passed |
| Baseline `mypy src` | passed (39 source files) |
| Baseline `pytest` | 633 passed |

## Current repository reality

The roadmap wording predates the repository. Task 10 does **not** start from a
deployment without a service.

Already present before this task:

- **A real production systemd service.** `scripts/deploy/mgo.service.template`
  renders to `/etc/systemd/system/mgo.service` and already provides the
  dedicated non-login `mgo:mgo` runtime identity, `SupplementaryGroups=video`,
  direct execution of `.venv/bin/uvicorn`, `MGO_CONFIG_PATH`, `Restart=on-failure`,
  `RestartSec=5`, `TimeoutStopSec=20`, `WantedBy=multi-user.target`,
  `StateDirectory=`/`LogsDirectory=` provisioning at `0750`, `UMask=0027`,
  empty `CapabilityBoundingSet=`/`AmbientCapabilities=`, `NoNewPrivileges=yes`,
  `ProtectSystem=strict`, `ProtectHome=yes`, `PrivateTmp=yes`,
  `RestrictNamespaces=yes` and a restricted `RestrictAddressFamilies=`.
- **An installer and a verifier.** `scripts/deploy/install-service-identity.sh`
  provisions the account, group, filesystem layout and unit, is idempotent,
  supports `--dry-run`, never overwrites an existing production configuration,
  backs up a replaced unit, and refuses to install a unit against a relocated
  virtual environment. `scripts/deploy/verify-service-identity.sh` is read-only.
- **Canonical deployment constants** in `mgo.core.config` as `PurePosixPath`
  values (`SYSTEM_CONFIG_PATH`, `SYSTEM_STATE_DIRECTORY`,
  `SYSTEM_DATABASE_PATH`, `SYSTEM_LOG_DIRECTORY`, …), shared by the production
  example configuration, the unit template, the scripts, the documentation and
  `tests/test_service_identity.py`.
- **A WAL SQLite database** with an explicit numbered migration runner
  (`CURRENT_SCHEMA_VERSION = 2`), a `schema_migrations` version authority, and a
  read-only health check using `PRAGMA quick_check(1)`.
- **Read-only status endpoints** (`/`, `/version`, `/health`,
  `/database/status`, `/camera/status`, `/camera/preview/status`,
  `/motion/status`, `/notifications/status`) that serve cached monitor state and
  perform no work per request.
- **journald as the log destination.** The application logs through
  `logging` to stdout/stderr; the unit sets `PYTHONUNBUFFERED=1`. No file
  handler is configured anywhere. `/var/log/garden-observatory` exists and is
  provisioned, but nothing writes to it.

What is **absent**: any backup, any restore verification, any retention, any
scheduled operations job, any log-rotation policy, any diagnostic bundle, any
structured operational log format, and any operator command-line tooling.

### OPS-01 assessment

| Element | State |
| ------- | ----- |
| API service unit with restart policy | **Already complete.** No material redesign required. |
| Worker service templates | **Intentionally not delivered.** See below. |
| Operations (backup) unit + timer | **Delivered by this task** — real functionality, not a placeholder. |

The existing API unit was audited directive by directive against the Task 10
requirement. `Restart=on-failure`, `RestartSec=5`, `TimeoutStopSec=20` and
`WantedBy=multi-user.target` already express the required restart and boot
policy, and the security sandbox already exceeds what Task 10 needs. **No defect
was found**, so the API unit is left byte-for-byte unchanged. Changing values
purely so that Task 10 appears to have touched the service would be a regression
risk taken for cosmetic reasons.

### Worker-service decision

**No worker unit is created.**

There is no standalone camera, detection, identification or notification worker
executable in the repository. Camera monitoring, database health, motion
analysis and notification publication all run as `asyncio` tasks inside the API
process's lifespan (`src/mgo/api/app.py`), sharing its state objects and its
single camera owner. Nothing exists that could truthfully be run as an
independent service.

Creating a unit now would mean shipping an empty service, a placeholder
executable, a process that sleeps, or a service that imports the API merely to
look functional. Each is a lie told in a `systemd` unit, and each would have to
be undone when the real worker arrives.

OPS-01 is therefore recorded as **satisfied for the current API service and
intentionally incomplete for future workers**. A worker unit will be introduced
only when an actual worker process and lifecycle contract exist. The backup
one-shot service and timer added here are not a substitute: they run real
functionality delivered by this task.

## Missing operations capabilities this task delivers

1. A production-safe SQLite backup tool that works while the API is running.
2. Backup verification and isolated restore testing.
3. Bounded backup retention.
4. A scheduled backup service and timer.
5. A log-rotation policy for MGO-owned file logs.
6. Truthful documentation of journald versus file-log rotation.
7. A bounded, privacy-safe diagnostic support bundle.
8. Structured operational logging for the new tools.
9. Installer and verifier integration.
10. Comprehensive hardware-free tests.
11. Operator documentation and an exact Raspberry Pi validation procedure.

## Selected architecture

A new `src/mgo/operations/` package holds all business logic, so it is
importable, type-checked and testable on the Windows development machine. Shell
wrappers are thin.

```text
src/mgo/operations/
  errors.py            stable error codes and OperationError
  events.py            structured JSON operational events + redaction
  locking.py           bounded, atomic, non-blocking operation lock
  backup.py            SQLite backup, manifest, verify, restore-test, retention
  backup_cli.py        `backup | verify | restore-test | list`
  support_bundle.py    bounded, generated-only diagnostic bundle
  support_bundle_cli.py

scripts/operations/
  backup-database.sh          wrapper for the backup CLI
  create-support-bundle.sh    wrapper for the bundle CLI

scripts/deploy/
  mgo-backup.service.template one-shot backup service
  mgo-backup.timer            daily persistent timer
  garden-observatory.logrotate  policy for /var/log/garden-observatory/*.log
```

Only the Python standard library and existing dependencies are used. No backup
package, no structured-logging framework, no archive library beyond `tarfile`.

### Rejected alternatives

| Alternative | Why rejected |
| ----------- | ------------ |
| `cp mgo.db` (file copy) | Not consistent under WAL. A copy taken while the service is writing can miss committed pages held in the WAL, producing a backup that verifies as "a file" but is not the database. |
| Stop `mgo.service` for a cold copy | Backup must not cost availability. SQLite's online backup API removes the need entirely. |
| `VACUUM INTO` | Writes to the source connection's context and rewrites page layout; the requirement forbids rewriting or vacuuming the production database to take a backup. |
| `sqlite3 .dump` to SQL text | Requires the `sqlite3` binary, produces a text artefact that cannot be integrity-checked as a database, and is far larger. |
| `.backup` via a shell wrapper | Puts business logic in Bash where it cannot be unit-tested on Windows. |
| Business logic in shell scripts | Untestable in CI, no typing, no structured error codes, fragile quoting. |
| `structlog` / `python-json-logger` | A new runtime dependency for what is ~60 lines of `json.dumps`. |
| Copying files from disk into the bundle | Every privacy exclusion then becomes a filter that must be perfect. Generating every member in memory makes the exclusions structural instead. |
| An operations API endpoint / dashboard button | Backup and bundle generation are privileged operator actions; the LAN API is unauthenticated. |
| New `mgo.toml` settings for schedule/retention | These are deployment concerns. The schedule belongs to the timer and retention to the unit's arguments. |

### Backup design decisions

- **Consistency** — `sqlite3.Connection.backup()` from a `mode=ro` source
  connection. One consistent snapshot while the service keeps writing. The
  source is never checkpointed, vacuumed, rewritten or mode-changed.
- **Self-contained artefact** — the *destination* copy inherits WAL from the
  copied header and would leave `-wal`/`-shm` sidecars, so a single published
  `.db` file would not be the whole database. The destination is therefore set
  to `journal_mode=DELETE` before closing, which checkpoints it into one file.
  This touches only the copy. The manifest records the observed value in
  `journal_mode_of_backup`, so the artefact is described truthfully.
- **Atomic publication** — build in a temporary file in the destination
  filesystem, validate, checksum, `fsync` where the platform supports it, then
  `os.replace()`. A failed backup never leaves a file whose name looks complete.
  An existing final name is never overwritten.
- **Naming** — `mgo-<YYYYMMDD>T<HHMMSS>Z.db` plus
  `mgo-<…>Z.manifest.json`; UTC, deterministic, lexically sortable, no secret,
  username or host path.
- **Symlink policy** — a source database that is a symlink is **refused**
  (`BACKUP_SOURCE_UNAVAILABLE`). There is no override flag. Following one would
  let a symlink swap redirect the read outside the approved root.
- **Retention** — runs only after a validated, published backup; keeps the
  newest N complete sets (`--keep`, default 14); deletes `.db` and manifest as
  one set; only ever touches files matching the strict backup name pattern;
  never deletes the newest set; failure is reported without invalidating the
  backup just taken.
- **Concurrency** — an atomic `O_CREAT|O_EXCL` lock file in the backup
  directory. Non-blocking, single attempt, no waiting. Stale reclamation is
  age-based only (default 6 hours) and never PID-based, because a PID is not
  portable and its absence is not proof of death. The application database is
  never used as the lock.
- **No production restore command.** `restore-test` proves recoverability into
  an isolated temporary directory; production restore remains an explicit
  operator disaster-recovery procedure documented in `docs/Operations.md`.

### Diagnostic bundle design decisions

- **Every member is generated in memory**; no file is ever read from disk and
  added to the archive. The database, WAL/shm sidecars, media, raw configuration,
  SSH material and Git files are therefore excluded *structurally* rather than
  by a filter that could be incomplete. No archive member can be a symlink, an
  absolute path or contain `..`, because member names are constants.
- **API collection** is loopback-only (`127.0.0.1`), GET-only, read-only
  endpoints only, with explicit short timeouts, no retries and a bounded read.
  No mutation endpoint is called: no capture, no preview start/stop, no
  notification publication.
- **Configuration summary** is derived from the typed `MGOConfig`, not from the
  raw TOML. Values are whitelisted by role. Unknown/future keys are listed by
  name with their values redacted, so a field added later defaults to redacted.
  Suspicious key names (`secret`, `token`, `password`, `credential`, `private`,
  `api_key`, `key`) are always redacted. Paths appear verbatim only when they
  exactly equal a canonical MGO deployment constant; anything else is reported
  as a role plus existence/writability.
- **Bounds** on endpoint response size, journal lines and bytes, command output,
  subprocess timeout, archive member count and total archive size, so no source
  can fill the SD card.
- **Outcomes** `complete` / `partial` / `failed`, with exit codes `0` / `1` /
  `2`. One failed source never prevents the rest of the bundle.

### Logging decisions

journald stays the primary runtime log. **No always-on duplicate file logger is
added** — it would double SD-card writes, create two authorities and let the two
records diverge. The logrotate policy therefore covers MGO-owned `*.log` files
under `/var/log/garden-observatory` and is written to be safe and idempotent
when the glob matches nothing, which is the current state. It does not, and
cannot, rotate journald; host journald retention is a host-level policy that
this task deliberately does not change for every service on the Pi.

Structured JSON operational events are introduced **for the Task 10 tools
only**. Application-wide structured logging is explicitly deferred: mass-editing
unrelated runtime modules for stylistic consistency is out of scope and is
recorded as a known limitation.

## Scope

### In scope

- `src/mgo/operations/` (errors, events, locking, backup, support bundle, CLIs);
- `SYSTEM_BACKUP_DIRECTORY` in `mgo.core.config`;
- `scripts/operations/` wrappers;
- `mgo-backup.service.template`, `mgo-backup.timer`, the logrotate policy;
- installer and verifier integration;
- `.gitignore` entries for generated artefacts;
- tests;
- `docs/Operations.md` plus `README.md`, `scripts/README.md`,
  `docs/Service-Identity.md` and `docs/Database.md` cross-references.

### Explicit non-goals

- a second API service, a replacement for `mgo.service`, or a competing
  deployment path;
- a fictional worker unit;
- any new API endpoint, dashboard control or bundle download route;
- database schema changes, migrations, or any write to production data;
- media backup, media reconciliation, image retention policy;
- automatic production restore;
- global host journald policy changes;
- new application configuration fields;
- authentication, reverse proxy, TLS, VPN, public exposure, remote/cloud backup;
- camera capture, preview control, motion analysis or notification delivery;
- Task 11 (camera simulator) and Task 12 (acceptance record).

## Compatibility requirements

Every existing endpoint keeps its exact path, fields, types, units and status
vocabulary: `/`, `/version`, `/health`, `/database/status`, `/camera/status`,
`/camera/capture`, `/camera/preview/status`, `/camera/preview/start`,
`/camera/preview/stop`, `/camera/preview/stream`, `/preview`, `/dashboard`,
`/motion/status`, `/notifications/status`, `/captures`, `/captures/{id}`,
`/observations`.

`mgo.service` is unchanged. `mgo.toml` gains no field. The schema stays at
version 2 and no migration is added. `pyproject.toml` gains no dependency.

## Security model

- Backups and bundles are never world-readable: backup files `0640`, the backup
  directory `mgo:mgo 0750`, support bundles `0600`.
- No secret, token, password, credential, private key, environment dump, raw
  configuration, database file, media file or media filename enters any
  artefact.
- No external network access; the bundle talks only to `127.0.0.1`.
- No `shell=True`; every subprocess uses an argument array with an explicit
  timeout and bounded captured output.
- No archive member may be absolute, contain `..`, or be a symlink — guaranteed
  by generating every member.
- Output paths are validated so a caller cannot escape the approved root, and
  `restore-test` refuses to target the production database directory.
- The backup service runs as `mgo:mgo` with no capabilities and
  `NoNewPrivileges=yes`; nothing in Task 10 runs the application as root.
- A failed backup exits non-zero and leaves no apparently complete artefact.
  API availability never depends on backup success.

## Rollback model

Reversible with no data loss. **Three** states, not two — see finding 17.

- **Before any Pi installation validation** — return to `main`; nothing on
  `main` is touched and no system artefact was installed.
- **After pre-merge Pi installation validation, before merge** — the installed
  units are external to Git and survive a checkout, so returning to `main` is
  the *last* step rather than the whole of it: disable and stop
  `mgo-backup.timer`, remove the two unit files and the logrotate policy,
  `systemctl daemon-reload`, preserve every recovery set, verify `mgo.service`,
  then return to `main`.
- **After a future merge** — the same unit removal, plus revert the Task 10
  commits, re-run the installer from the reverted code, and verify
  `mgo.service`. The API unit needs no restart because it never changed.

Existing backups are left intact unless the operator explicitly removes them.
No rollback step deletes the production database, captures or support bundles,
resets history, force-pushes, or overwrites the production configuration. No
automatic rollback script is provided, deliberately: a script that deletes
backups is exactly the thing that must not exist.

## Raspberry Pi validation plan

**Not executed in this task.** No Raspberry Pi is accessed, modified or deployed
to. The exact procedure Matthew runs later lives in
[`docs/Operations.md`](../Operations.md). It begins with a clean-tree check
before any branch change, records the production database SHA-256, size, schema
version, observation count, capture count, service PID, restart count and
preview state before and after, proves the database is unchanged, restores a
pre-existing preview after any intentional restart, uses a unique Task 10
validation backup, deletes no existing backup, rotates only a synthetic log file
created for the test, and confirms the support bundle contains no private
content before it is copied off the Pi.

It now also **records whether the three operational artefacts already exist
before installing anything** (§13.1a), and **removes what it installed before
returning to `main`** (§13.14) — disabling and stopping the timer, deleting the
two unit files and the logrotate policy, reloading systemd and verifying the
removed state — while deleting no recovery data. Permanent installation is a
post-merge act; a feature branch must not leave one behind.

Backups are **not** described as working until that validation has been
performed and confirmed by Matthew.
