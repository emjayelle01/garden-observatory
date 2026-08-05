# Task 12 remediation — approved deployment gateway

## Status

**Implementation and repository review complete. The second Raspberry Pi
staging validation passed, the legacy wildcard sudoers policy is retired, and
the reviewed gateway and its replacement policy are installed on the production
Raspberry Pi. Installed-policy boundary validation passed.
Application deployment through `deploy-main` has not been exercised,
`restart-api` has not been exercised, and physical camera acceptance remains
pending.**

**The first Raspberry Pi staging-validation attempt is recorded below as
incomplete, not as passed.** One gateway-focused test escaped its temporary
harness and invoked the Pi's installed production gateway through `sudo`.
Production was unchanged by it, but the hard boundary was breached, the run was
stopped, and the repository test isolation has been corrected. See
[Raspberry Pi staging validation — incomplete](#raspberry-pi-staging-validation--incomplete).

| Gate | Outcome |
| ---- | ------- |
| Remediation definition | Complete (this record, first commit) |
| Gateway implementation | Complete |
| Rollback transaction | Complete |
| Installer and sudoers | Complete |
| Documentation | Complete |
| Repository review | **Round 1 complete** — seven blocking defects found |
| Review corrections | Complete — all seven corrected |
| Re-review | **Round 2 complete** — five further blocking defects found |
| Re-review corrections | Complete — all five corrected |
| Final review | **Complete** — four further blocking defects found |
| Final-review corrections | Complete — all four corrected |
| Final confirmation | **Round one complete** — four further blocking defects found |
| Final-confirmation corrections | Complete — all four corrected |
| Mutation register | Complete — checked in and re-run in full against the current tip |
| Final confirmation (round two) | **Complete** — three further blocking defects found |
| Entry-boundary corrections | Complete — all three corrected |
| Raspberry Pi staging validation (first attempt) | **Incomplete** — stopped on a test-isolation boundary breach |
| Test-isolation correction | Complete — wrapper entry points now run behind a disposable-copy harness |
| Final confirmation (re-run) | **Complete** |
| Raspberry Pi staging validation (second run) | **Passed** — 2026-08-04, at `71d3755` |
| Legacy wildcard sudoers policy | **Retired** — 2026-08-05, archived under `/root` |
| Installation on the Raspberry Pi | **Passed** — 2026-08-05, from `71d3755` |
| Installation review | **Passed** |
| Installed-policy boundary validation | **Passed** — bounded refusals and refused negative sudo probes |
| Application deployment through `deploy-main` | **Not exercised** |
| `restart-api` through the installed gateway | **Not exercised** |
| Physical camera acceptance | **Pending** |

The reviewed gateway and its replacement sudoers policy are installed on the
production Raspberry Pi, and the legacy wildcard policy that stood beside them
has been retired and archived. What that proves is bounded: the control plane
is in place and refuses what it is supposed to refuse. **No application code
has been deployed through it.** `deploy-main` and `restart-api` have never run
against the installed gateway, so the deployment transaction itself is still
unexercised in production, and physical camera acceptance remains pending in
full.

The first staging-validation attempt ran repository tests on the Pi, changed
nothing, and did not pass; it is recorded below as incomplete and stays that
way. The second run is a separate, later event.

## Why this task exists

The Task 12 production deployment could not use the deployment gateway. The
attempt failed closed, which was the right outcome, but it left the project
without a repeatable deployment path: production was moved to the merged SHA by
a bespoke, individually authorised sequence of direct Git commands.

That is not a defect in the deployment that was performed — it was correct,
verified and non-destructive. It is a defect in the *control model*: there was
no repository-managed, testable way to deploy, so every deployment needed a
human-authored instruction naming each command. The next deployment would have
needed another one.

## The confirmed defect

### The installed gateway cannot deploy application code

This section describes **Event A**, the 2026-08-01 `install`-action failure. It
is not the mechanism that refused the 2026-08-04 staging test escape; that was a
different action and a different refusal, recorded under
[Event B](#event-b--the-2026-08-04-staging-test-escape).

`/usr/local/sbin/mgo-validate` on the Pi is not repository-managed — no copy of
it is tracked here — and its supported actions are `show-approval`, `install`
and `restart-api`. There is no `deploy-main`. It is pinned to Task 10:

```bash
readonly FEATURE_BRANCH="task-010-operations"
```

Its `install` action:

- requires `origin/task-010-operations` to exist;
- requires the production checkout to *be on* that branch;
- invokes `scripts/deploy/install-service-identity.sh`;
- provisions identity, systemd and operations artefacts;
- never fetches application code;
- never advances the production checkout.

So `install` cannot deploy merged `main`, and on 2026-08-01 it exited 128 inside
its own precondition check when `rev-parse origin/task-010-operations` found no
such ref. Production was untouched by the failure.

The name is the trap: an operator reading `install` reasonably expects it to
install the new code. It installs *service identity*. Those are different
operational concerns and this task stops them sharing a verb.

### The repository helper does not meet the approval boundary

`scripts/deploy/update-main.sh` predates the approval model. It:

- fetches and pulls `main` without consulting the approved SHA at all;
- uses `uv sync` rather than `uv sync --frozen`, so a deployment may resolve
  dependencies the repository never locked;
- prints a warning and continues when `uv` is missing, deploying code against
  a stale environment;
- performs broad recursive `sudo chgrp`, `sudo chmod` and `sudo find` over the
  whole checkout;
- invokes `sudo systemctl restart` directly;
- treats endpoint probes as best effort, so a deployment that left the API
  unreachable still reports completion;
- has no rollback: a failure after the fast-forward leaves production
  half-deployed;
- does not preserve the preview operating state.

Two incomplete control models is one too many. This task replaces both with a
single authoritative workflow.

## What the remediation must deliver

One durable, repository-managed, testable deployment path that:

- uses the root-owned approval SHA as the deployment authority;
- deploys only `origin/main`;
- performs only a strict fast-forward;
- runs Git and `uv` as the unprivileged `claude` account;
- runs only the root-required service operation as root;
- fails closed;
- rolls back a partially applied deployment;
- preserves the pre-deployment preview state;
- removes the ambiguous legacy `install` action;
- installs atomically as `/usr/local/sbin/mgo-validate`;
- needs no bespoke direct-Git authorisation for future deployments.

## Architecture

A repository-managed privileged gateway at `scripts/deploy/mgo-validate`,
installed to `/usr/local/sbin/mgo-validate`, exposing exactly three public
actions:

| Action | Purpose |
| ------ | ------- |
| `show-approval` | Print the approved SHA and nothing else |
| `deploy-main` | Deploy `origin/main` at the approved SHA, transactionally |
| `restart-api` | Restart the service at the already-deployed approved SHA |

`install` no longer exists. Invoking it fails non-zero with a bounded message
naming `deploy-main` for application deployment and the service-identity
installer for provisioning. No feature-branch constant survives.

## Privilege model

The gateway runs as root only through `sudo`, and only for the caller `claude`.
Everything that touches the repository or the virtual environment is executed
back down as `claude`; only the service restart needs root. Git and `uv` never
run as root and never run as `mgo`.

Every production path is a fixed constant in the script. The gateway accepts no
repository path, service name, ref, remote, branch, command or username from the
caller — the action parser is the entire input surface, and it takes one word.

## Ordering

The proofs come before the mutation, in this order:

1. validate approval;
2. validate local preconditions;
3. query remote `main`;
4. match the remote SHA to the approval;
5. fetch;
6. match local `origin/main` to the approval;
7. prove ancestry;
8. mutate the checkout.

Nothing before step 8 changes production state.

## Transaction model

The pre-deployment SHA and preview state are captured internally before any
mutation, and only those captured values are ever used to restore. A rollback
SHA is never accepted from a caller.

| Failure point | Response |
| ------------- | -------- |
| After fast-forward, before restart | Restore the checkout and environment; do not restart; exit non-zero |
| Restart, health or preview restoration | Restore checkout and environment, restart once, restore recorded preview state; exit non-zero reporting `deployment failed; rollback succeeded` |
| Rollback itself | Preserve evidence, do not loop, distinct high-severity exit code, name the failed stage, claim nothing about production |

## Preview policy

Deployment preserves the operating state it found. A preview that was running is
restarted once after health succeeds — but only if the restart left it stopped,
because `preview.auto_start` remains `false` in production. A preview that was
stopped or failed stays that way: completing a deployment is not a reason to
start a camera nobody asked for. The stream is never opened, no frame is
inspected, and no capture endpoint is called.

## Repository review — seven blocking defects corrected

Review round 1 found seven defects. All are corrected; none had reached the
Raspberry Pi, because nothing here has ever been installed.

### Finding 1 — approval parsing was not byte-exact

`wc -l <= 1` plus one `read` of the first line accepted `<valid sha>\nmain`
with no final newline: one newline passed the count, the read returned a valid
SHA, and a whole trailing line was ignored.

The parse is now anchored on the file's own length — exactly 40 bytes, or 41
with a single final LF — and that final byte is compared as a **hex value**,
because command substitution silently drops a NUL and would otherwise let a
trailing NUL pass for a newline. CR, CRLF, embedded control bytes, trailing
spaces, empty second lines and trailing data are all refused, and `wc -l` is
gone.

### Finding 2 — the installer relied on errexit inside a conditional

`if ! install_file ...` disables errexit for everything the function runs, so
an internal `install` failure could fall through to the rename and publish a
truncated file that `sudo` would execute.

Every mutating command now checks its own status and returns immediately, and
a failed step removes its own temporary file. Nothing in the installer depends
on errexit inside a conditional call.

### Finding 3 — the installer had no real transaction

Both previous states are now recorded before the first mutation, with
**"absent" as a recorded state** whose restoration is removal. Any failure —
temporary file, either copy, either rename, either checksum, either metadata
check, or the installed-policy validation — restores **both** targets. The
installed policy is re-validated with `visudo -cf` after installation, because
validating what was about to be written is not validating what landed. A
restoration that itself fails exits **78** and never claims the host is clean.

### Finding 4 — an identical installation still rewrote files

The installer set a flag and carried on through installation. It now verifies
and exits **before** any temporary file exists, leaving inode and modification
time untouched. Matching content with the wrong owner or mode is explicitly
*not* current: that is a defect, and it is repaired transactionally.

### Finding 5 — HTTP 200 was not enforced

`curl -f` accepts every 2xx and reports a redirect as success. Health,
preview-status and preview-start now capture the status and compare it to
exactly `200`, never follow redirects, keep proxies disabled and literal
loopback, write bodies to `mktemp` files that are always removed, and **never
interpret a body from a non-200 response**.

### Finding 6 — a non-running preview was not preserved, only skipped

Restoration returned success immediately when preview had not been running,
which made "left alone" mean "not checked". It now proves the state: a
previously non-running preview must still be non-running with **zero**
producers, and drift into running is a failure that enters the post-restart
rollback path. It is deliberately not papered over with a stop request — a
camera that started itself is a fault to surface, and an unasked-for stop would
be a second unrequested mutation.

### Finding 7 — there was no final verification

A new stage runs after preview restoration and before any success message,
re-reading approval, branch, `HEAD`, local `main`, `origin/main`, tree
cleanliness **including untracked files**, stash, in-progress operations,
service state, exact-200 health, preview state and producer counts. A failure
takes the rollback path, and `deployed` is never printed before it passes.

### Also corrected

- **Untracked files** are now included in every cleanliness check — after the
  sync, in rollback verification and in final verification. Ignored paths such
  as `.venv` remain ignored.
- **Runtime readiness** no longer tests an executable bit. It proves, as the
  runtime account with the production configuration selected, that the deployed
  interpreter and launcher are executable and that `mgo.core.config` and
  `mgo.api.app` import. Import only — no lifespan, no camera, no stream, no
  writes. A failure takes the pre-restart rollback path.
- **Dry run fails closed.** The help says it validates everything, so a host
  without `visudo` now fails in dry-run mode too rather than reporting a
  success it did not earn.

## Re-review round 2 — five blocking defects corrected

Re-review found five further defects. All are corrected; none reached the
Raspberry Pi, because nothing here has ever been installed.

### Finding 1 — no exclusive deployment transaction

Nothing stopped two mutating invocations running at once. Two `deploy-main`
calls could both read the same old `HEAD`, both pass the same approval and
ancestry proofs, both fast-forward, restart the service twice, race each
other's preview restoration and then run two incompatible rollbacks over one
repository. A `restart-api` arriving between the restart and the final
verification was the same problem.

All three mutating entry points — `deploy-main`, `restart-api` and the
installer — now take one non-blocking `flock` on
`/run/lock/mgo-deployment.lock`, on a fixed descriptor held for the whole
action **including rollback**, exiting **75** when it is busy. No retry, no PID
file, no staleness protocol: the kernel releases it when the holder exits.
`show-approval` is read-only and is not blocked.

The first installation is the exception, and is documented as such: the gateway
currently on the Pi predates this lock, so that separately authorised run must
happen with no deployment or restart in progress.

### Finding 2 — the pre-deployment preview state was not proven

`preview_state_from_status` returned `unknown` when the field was missing, and
that invented state became the baseline the whole deployment measured itself
against. Nothing reconciled the reported state against the running producers.

There is now no fallback: missing, empty, duplicated, conflicting, malformed or
unrecognised all refuse before mutation. Only `running`, `stopped` and `failed`
are settled enough to be a baseline; `starting` and `stopping` stop the
deployment. The state is then reconciled against the processes — one
`rpicam-vid` for running, none for stopped or failed, never a `libcamera-vid` —
and a disagreement is refused rather than repaired. Preflight starts and stops
nothing. The same vocabulary now applies during restoration and final
verification.

### Finding 3 — a failed fast-forward bypassed the rollback

`git merge --ff-only` exiting non-zero was treated as "nothing happened" and
exited directly. It is not: the command can fail part-way through a checkout,
on a permission or I/O error, or after the ref has already been updated.

The transaction now opens **before** the merge is invoked. A failed merge takes
the pre-restart rollback path, and immediately after a successful merge — before
the environment sync — the checkout is verified (branch, `HEAD`, local `main`,
`origin/main`, clean including untracked files, no operation in progress), with
that failing rolling back too.

The pre-restart rollback itself was also strengthened. A PID and a timestamp do
not say production is healthy, so it now proves branch, `HEAD`, local branch, a
clean tree including untracked files, no stash, no operation in progress, the
unchanged PID and activation timestamp, an active service, an exact-200 health
response, the preview state matching the captured baseline and the producer
count matching it. It still never restarts the service, because nothing
disturbed it.

### Finding 4 — the installer followed unsafe target types

Regular-file tests, checksums, `stat` and `cp` all followed symlinks, so a link
to matching content could be read as a current installation, its target copied
instead of the link recorded, the link replaced on installation and an ordinary
file restored where a link had been. A directory, FIFO, socket or device could
be misclassified as absent.

Sources must now be regular non-symlink files; targets must be absent or
regular non-symlink files, and any other type is refused rather than repaired.
Ownership is compared numerically instead of by rendered name. Backups moved
out of `/etc/sudoers.d` — a backup there is a second policy file in a directory
sudo includes — into a root-owned `0700` transaction directory under `/run`,
preserving content, numeric owner, numeric group and mode. Every artefact is
removed after success and after successful rollback, and a cleanup failure is a
non-zero result, never reported as success.

### Finding 5 — restart-api lost branch-aware validation

`restart-api` had been folded onto the main-only deployment precondition, which
would have pushed future Pi validation of an approved feature SHA back to
bespoke restart instructions — the very thing this gateway exists to remove.

It now has its own read-only precondition: a named branch (not detached) whose
`HEAD` is the approved SHA, tracking the identically named branch on the
expected remote at the approved SHA, with a clean tree including untracked
files, no stash, no operation in progress and exactly one worktree. The branch
is discovered from the checkout, never accepted from the caller. For `main`
that is exactly the previous `origin/main` requirement. `deploy-main` remains
strictly main-only.

### Also corrected

Every temporary file the gateway creates is now removed through one tracked
helper whose failure is checked: an HTTP helper that leaked its own response
body no longer reports success. The removal is scoped to a single path — never
a pattern, never a directory.

## Final review — four blocking defects corrected

Final review found four more. All are corrected; none reached the Raspberry Pi,
because nothing here has ever been installed.

### Finding 1 — the lock object was not secured

The lock was opened with append redirection, with no check on what it was or
what mode it had. A readable lock file is a denial-of-deployment primitive: any
unprivileged process that can open it read-only can hold an exclusive `flock`
on it and block every deployment and restart indefinitely.

The lock must now be a **root-owned `0600` regular file** reached through a real
directory, verified before the lock is taken. When absent it is created under
`umask 0077` with `noclobber`, so it is private from the moment it exists and a
simultaneous first-run caller never replaces the winner's inode. A symlink,
directory, FIFO, socket, device or wrongly owned file is refused, never followed
and never replaced — replacing it would drop a legitimate holder's lock. The
installer may tighten the mode of an existing root-owned regular lock file
during first installation, and may never touch its inode.

### Finding 2 — the status document was pattern-matched, not parsed

`grep` could find `"state":"running"` inside a truncated or garbage-padded
response and return it as a deployment baseline, and could not distinguish a
top-level field from one nested inside `camera`.

The system interpreter now parses the response file as JSON: valid UTF-8, no
NUL, a top-level object with no leading or trailing data, exactly one top-level
`state` key — duplicates refused even when they agree — and a string value.
Nested keys never substitute. The response is handed over **by path**, so
arbitrary remote bytes never pass through the shell, and the application is not
imported to do it: parsing a response must not depend on the code being
deployed.

### Finding 3 — curl obeyed configuration

`curl` read `~/.curlrc`, so a configuration file belonging to root could add
`--location`, change the proxy or alter timeouts behind the gateway's back —
at root. Every invocation now begins with `--disable`, and carries
`--no-location` and `--max-redirs 0` alongside the existing exact-200
comparison and literal loopback address.

### Finding 4 — the caller's environment was inherited

`GIT_DIR`, `GIT_WORK_TREE`, `UV_PROJECT`, `PYTHONPATH`, `VIRTUAL_ENV`,
`CURL_HOME`, `TMPDIR` and the proxy variables could each redirect a
root-invoked deployment at a different repository, index, configuration,
interpreter or remote.

Commands run as `claude` and `mgo` now go through `env -i` and receive only what
they need, with `HOME` read from the account database and proven absolute rather
than taken from the caller. The root side fixes `HOME`, `PATH`, `LC_ALL` and a
root-owned `TMPDIR`, and explicitly unsets the whole list. Request validation
was also moved ahead of all of it, so an unsupported action is refused before
the process creates anything. The canonical-path proof, previously only in
deployment, now also guards `restart-api`.

## Final confirmation — four blocking defects corrected

Final confirmation found four more. All are corrected; none reached the
Raspberry Pi, because nothing here has ever been installed.

### Finding 1 — the implementation had not passed the complete mutation set

The previous round disclosed that its 56 earlier mutations were not re-run
after the latest code changes, and then aggregated the historical results into
a current-tip claim of "70 of 70". That claim was not available: a mutation
written against code a later round rewrote goes stale silently, and four had
already been found stale in round two.

The root cause was that the mutations existed only as actions someone took.
There was no machine-readable register, so "re-run them all" meant
"reconstruct them from memory", which is exactly the thing that cannot be
audited.

There is now a register in the repository — `tests/mutation_register.py`, run
by `scripts/dev/run-mutations.py`. Each entry names one deliberate defect, the
asset it applies to, and the tests that must fail because of it. Each is
applied to a byte-exact copy, tested, restored, and the restoration confirmed
by digest. An entry whose `old` text no longer appears exactly once fails as
**stale** rather than passing quietly, which is the failure mode this round was
called to fix.

Two mutations are deliberately **not** registered, with the reason recorded
beside them: removing the SHA pattern's anchors, and removing the NUL check.
Both are equivalent mutants — the byte-exact size reconciliation and strict
JSON parsing respectively make them unobservable — and registering a defect
that cannot change behaviour would mean recording a permanent false negative.

Building the register also found real gaps in the tests, which is the process
working rather than a separate defect. Eleven mutations initially survived, and
each is now caught: the redirect refusals were asserted only by absence; the
cleanliness count was a lower bound that dropping one still satisfied; the
non-blocking `flock` option was invisible behind its own double; the rollback
verification asserted that three facts were read but not that they were
compared; a restart-api check was masked by a second check that happened to
catch the same case; the installer's backup flags were asserted against a
comment that named them; and the parser's type and decode checks were refusing
by traceback rather than by contract.

### Finding 2 — the root temporary directory was not secured

`/run/mgo-validate-tmp` was treated as safe whenever `-d` succeeded, and then
`chmod`ped. `-d` follows symlinks, so a link planted at that path satisfied the
check and the `chmod` was applied to whatever it pointed at. Nothing proved
numeric ownership before the path was exported as `TMPDIR`.

It is now proven: `/run` must resolve to its own physical path; the temporary
path must not be a symlink (checked first); it must be absent or a real
directory; an existing one must be numeric `0:0` with mode exactly `0700`. An
unsupported type or a non-root owner is refused. Nothing is chmodded, chowned
or replaced — adopting a directory something else created is an operator's
decision. When absent it is created under `umask 0077`, private from the moment
it exists. Every temporary file names the directory explicitly with
`mktemp --tmpdir=`, so its location stops being a property of the environment.

`show-approval` no longer prepares it. The directory is created only for the
two actions that mutate anything; the read-only action takes no lock, creates
no directory, writes no temporary file and changes nothing.

### Finding 3 — environment isolation did not begin at process entry

Both scripts used `#!/usr/bin/env bash`, which resolves the interpreter through
an inherited `PATH` before either could sanitise anything, and the root side
merely unset a named list and called the remainder constructed.

Both now name `/bin/bash` directly, and both require a constructed environment
as the first thing `main` does — re-executing themselves through
`/usr/bin/env -i` when they are not already in one. The re-execution is what
`BASH_ENV`, `ENV`, `LD_PRELOAD` and `LD_LIBRARY_PATH` require: all four have
already acted by the time a script's first statement runs, so only a fresh
interpreter escapes them. The second process then asserts the three fixed
values itself and removes every other exported variable, which reduces a forged
marker to a guard against re-executing twice.

The check is an allowlist over the whole environment. "Nothing else is here" is
not a statement a denylist can make. The named `unset` register is kept as
defence in depth and as a reviewable list, not as the mechanism.

The JSON parser is additionally started with `env -i` and `-I`, so
`PYTHONINSPECT`, `PYTHONSTARTUP`, `PYTHONWARNINGS`, `PYTHONPATH` and the user
site directory cannot reach it even if something upstream reintroduced them.
`claude` and `mgo` keep their existing minimal environments, including
`claude`'s fixed account `HOME` so the approved deployment SSH key still works.
`SSH_AUTH_SOCK` is not inherited. The installer establishes the same boundary,
so neither script depends on sudo's optional `env_reset` or `secure_path`.

### Finding 4 — stale installer transaction state was hidden by idempotence

The installer used one fixed transaction directory. A cleanup failure left it
behind, and on a later run — with both targets already correct — the installer
returned success before examining it. "Installation succeeded but cleanup
failed" became "verified; nothing changed", with the previous sudoers policy
still on disk.

`/run/mgo-validate-install` is now a **parent**: root-owned, `0700`, refused if
it is a symlink or the wrong type, owner or mode. Each run creates its own
uniquely named workspace inside it while holding the deployment lock, and
removes only that workspace — no pattern, no wildcard, nothing that could reach
another run's directory.

Anything found in the parent at entry is a bounded refusal naming the
condition, exit **65**, and it is checked **before** the idempotent-success
return, so "both targets are correct" can no longer answer "a previous run did
not finish". The leftovers are preserved for inspection rather than destroyed,
and every subsequent run keeps refusing until they are removed deliberately. A
dry run reports the same condition and removes nothing.

## Entry-boundary corrections — three blocking defects corrected

Final confirmation found three more. All are corrected; none reached the
Raspberry Pi, because nothing here has ever been installed.

### Finding 1 — environment isolation started too late

The scripts used a fixed `/bin/bash` shebang and then re-executed through
`env -i`. That does not stop the *first* interpreter processing `BASH_ENV` and
`ENV`, importing exported shell functions, or honouring `SHELLOPTS`,
`BASHOPTS`, `CDPATH` and `GLOBIGNORE` — all of which happen during Bash's own
startup, before the script's first statement. Re-execution cannot undo commands
that have already run.

Both privileged scripts now begin `#!/bin/bash -p`, and the internal
re-execution runs `/bin/bash -p` too, so neither process is the weaker of the
two. The `env -i` boundary is retained as defence in depth.

Because the shebang carries the mode, the script has to be **executed** rather
than handed to an interpreter. Every documented invocation is now
`sudo ./scripts/deploy/install-mgo-validate.sh`; the `sudo bash …` form is gone
from the installer's own header and usage text, the wrapper's error message,
`docs/Deployment-Gateway.md`, `docs/Remote-Access.md` and `scripts/README.md`.

The sudoers policy gained a command-scoped environment boundary:
`Defaults!MGO_VALIDATE env_reset`, plus `env_delete` lines naming the shell
startup, loader, interpreter, Git, uv, curl, SSH, proxy and temporary-file
variables. `env_delete` wins over `env_keep`, so they go even if something else
adds them back. There is deliberately no `SETENV` — with it,
`sudo BASH_ENV=/tmp/evil mgo-validate` would be permitted and every reset would
be a suggestion. The grant is still one account, one absolute path, via a
single `Cmnd_Alias`.

**Recorded plainly:** `LD_PRELOAD` and `LD_LIBRARY_PATH` are honoured by the
dynamic loader before the interpreter exists. No shebang, shell option or
re-execution can speak for code that ran before Bash started. That boundary
belongs to the operating system and to sudo — which is why the sudoers policy
deletes them — and this gateway does not claim otherwise.

### Finding 2 — a dry run returned success for a refused installation

`--dry-run` reported unsafe or stale transaction state and then printed
"nothing was changed" and exited zero. A validation command that answers "fine"
and then refuses when run for real has told the operator nothing.

The dry-run contract is now: clean state exits 0; an unsafe transaction parent,
an invalid source, an invalid policy, a missing `visudo`, an unsupported target
type or an unsafe lock object exit non-zero; and stale transaction state exits
**65**, the same code the real installation uses, so a wrapper reading the
status learns the same thing from either mode. Nothing is created, removed or
rewritten in any outcome, and the run states in its first line that it holds no
lock and is validating a point-in-time view.

`transaction_parent_state` now also fails closed: `find` on an unreadable
directory exits non-zero with no output, and treating that as an empty parent
would turn the one condition the check exists to detect into a clean answer.

### Finding 3 — source validation happened outside the lock

The installer validated the repository gateway and policy, checksummed them,
and only then acquired the shared deployment lock. A concurrent `deploy-main`
could fast-forward the checkout between `bash -n`, `visudo -cf`, the checksum
and the install — and the host would run bytes nothing had looked at, with a
clean report.

The lock is now taken **before any source is read for validation**. Under it,
the run creates its uniquely named workspace, verifies it, copies both sources
into it, and validates, checksums and installs from that snapshot. Nothing
after the snapshot reads the live checkout. A source that changes before the
lock is harmless because the snapshot is what is validated; a source that
changes after it is harmless because the snapshot is what is installed.

What landed is validated too — `/bin/bash -p -n` against the installed gateway
and `visudo -cf` against the installed policy — because a checksum says the
bytes match, not that they still work under the interpreter and parser that
will read them. Either failure restores both targets transactionally.

The workspace itself is verified after creation: a real directory, not a
symlink, numeric `0:0`, mode exactly `0700`, and inside the fixed parent. The
staged copies are checked the same way at `0600`. Cleanup no longer reports
success while the tracked path survives as a regular file, a symlink or
anything else.

The currency decision moved into the transaction, because "current" now has to
mean "matches the bytes this run validated". A fully current installation still
touches no target: same inode, same modification time, no `install` and no
rename against either target.

## Raspberry Pi staging validation — incomplete

The first staging-validation attempt on the Raspberry Pi is recorded as
**TASK 12 GATEWAY PI STAGING VALIDATION INCOMPLETE**. It is not a pass, and it
must not be summarised as one.

The run used the `claude` account on host `mgo-core` (`aarch64`), against a
disposable clone at
`290db535e1d25e1b4080c956b378e907c8b7bf54`. The clone was removed afterwards.

**Correction to the initial staging record.** The escaped 2026-08-04 request did
not fail a main-versus-`task-010-operations` branch precondition. The installed
legacy gateway did not implement `deploy-main`, so it refused the action as
unsupported. The approval file was also empty. The `task-010-operations` branch
precondition applied to the separate 2026-08-01 `install`-action failure. This
supersedes the explanation first written in this section and repeated in the
message of commit `6b27891`, which is left in history as it was written.

### Two separate gateway events

They are easy to conflate — both involve the same installed legacy gateway, and
both ended with production untouched — but the reason each one changed nothing
is different, and only one of them involves the branch pin.

| | Event A | Event B |
| --- | ------- | ------- |
| When | 2026-08-01 | 2026-08-04 |
| What ran | `mgo-validate install` | `sudo -n /usr/local/sbin/mgo-validate deploy-main` |
| Who ran it | The authorised production deployment | An unsafe test that escaped its harness |
| Why nothing changed | The action required `origin/task-010-operations`, which was unavailable, so it exited **128** inside its own precondition check | The action was **not supported** by the installed gateway, so it reached the unsupported-action refusal |
| Branch pin involved | Yes — `FEATURE_BRANCH="task-010-operations"` | **No** |

### Event A — the 2026-08-01 install-action failure

The authorised Task 12 production deployment first tried the gateway's `install`
action. That action was hardcoded to `task-010-operations`, required
`origin/task-010-operations` to exist, and invoked Task 10's service-identity
and systemd provisioner (`scripts/deploy/install-service-identity.sh`) rather
than deploying application code at all. The ref was unavailable in the
production checkout, so the action exited **128** inside its own precondition
check. Production remained untouched. This is described in full under
[The confirmed defect](#the-confirmed-defect), and it remains the correct
explanation of that failure.

### Event B — the 2026-08-04 staging test escape

One gateway-focused test — `test_the_wrapper_reports_a_missing_gateway_and_stops`
— executed the tracked `scripts/deploy/update-main.sh` directly. That wrapper
contains the fixed production path `/usr/local/sbin/mgo-validate`, so on the Pi,
where that path exists, the test did not reach the missing-gateway branch it was
written for. It reached the wrapper's delegation, and ran:

```text
sudo -n /usr/local/sbin/mgo-validate deploy-main
```

against the real control plane. The run was stopped there.

**Why the installed gateway changed nothing.** The path that exists on the Pi is
still the **legacy Task 10 gateway**, whose supported actions are
`show-approval`, `install` and `restart-api`. It does not implement
`deploy-main` at all, so the request reached its unsupported-action refusal —
the same refusal any unrecognised word would have met. The approval file was
also empty, so no valid approved SHA existed for any action that needed one.
Nothing was fetched, nothing was merged, no `uv sync` ran, no service was
restarted and no preview transition occurred, and no installed file changed.

That is a property of the *installed* gateway's own refusal, not of the test —
the test had already crossed the boundary. **A gateway that accepted
`deploy-main` could have changed production**, which is precisely what the
gateway this branch ships is for. The refusal is not evidence that the boundary
held; it is evidence about which gateway happened to be installed that day.

**Neither of Event A's mechanisms explains this.** The branch pin and the
missing ref both belong to the `install` action. This gateway had no
`deploy-main` to run at all, so the refusal happened where an unrecognised
action word is refused — at the action parser, before anything about the
repository was examined. Nothing in this subsection may be explained by which
branch production happened to be on; that belongs to Event A alone.

### Production non-interference — the evidence

Recorded from the Pi at the time of the run:

| Fact | Value |
| ---- | ----- |
| Production commit | `1aec2245010a1bd971d028be235c1864af6b46b3`, unchanged |
| Production branch and working tree | Unchanged |
| `mgo.service` `MainPID` | `70709`, unchanged |
| `mgo.service` `NRestarts` | `0` |
| Preview PID | `71087`, unchanged |
| Preview `started_at` | Unchanged |
| Configuration checksum | `8346e732c2545ff369f6c4f0e3fc2e415d10993d8fe6b4b1b2c67600555183da`, unchanged |
| Archive/database records | 8 |
| Physical capture files | 5 |
| Database and camera health | Healthy |
| Captures taken | None |
| Stream accessed | No |
| Images opened or decoded | None |
| Installed files changed | None |
| Approval file | Empty and unchanged |
| Disposable clone | Removed |

No file under `/usr/local/sbin`, `/etc/sudoers.d`, `/etc/garden-observatory`,
`/opt/garden-observatory` or `/var/lib/garden-observatory` was modified.

### What the attempt did establish

These results are real and are worth keeping:

* the tracked sudoers source passed the Pi's real `/usr/sbin/visudo -cf`;
* both privileged scripts passed `/bin/bash -p -n` on the Pi;
* privileged Bash on the Pi blocked `BASH_ENV`, `ENV` and exported shell
  functions;
* `uv sync --frozen`, Ruff and mypy passed on `aarch64`;
* 596 of 597 focused tests passed.

### What the attempt did not establish

* the remaining focused test;
* the full suite;
* the acceptance-document suite;
* the mutation register;
* the unprivileged installer dry run.

No gateway was installed by that attempt, and the production gateway was
unchanged by it. Physical camera acceptance remained pending. The installation
that did eventually happen was a separate, later, separately authorised act and
is recorded under [Root installation](#root-installation).

## The unsafe test, and the correction

### Root cause

The suite's design is to execute the shipped shell rather than describe it, and
that is right for every function the gateway exposes: they can be sourced and
called against temporary directories. The wrapper is the exception. Its entire
job is to resolve one fixed host path and hand control to whatever is there, so
executing the tracked file makes the *host* decide what the test does.

On a workstation with no gateway installed, that reaches the expected
missing-file branch and the test passes for the wrong reason. On the Pi the path
exists, so the same test invokes the real control plane through `sudo`. The
module docstring's claim that nothing in the suite touched a real `sudo` or the
Raspberry Pi was false for as long as that path existed, and nothing checked it.

### The isolated wrapper harness

Wrapper entry points now execute a **disposable copy**. The harness reads the
tracked wrapper, requires
`readonly GATEWAY="/usr/local/sbin/mgo-validate"` to appear exactly once,
rewrites only that constant to a path inside a private temporary root, keeps LF
line endings, and refuses to run a copy whose executable lines still name
`/usr/local/sbin`. A controlled fake `sudo` is placed first on the child's
`PATH`; it records its complete argument vector to a temporary log, executes
nothing, and exits `42`. No host file is removed and the tracked wrapper is
unchanged — its production constant remains part of the production contract and
is still asserted statically.

The two entry-point tests are now:

* **missing gateway** — the temporary gateway is absent and the fake `sudo` is
  present as a tripwire. The wrapper must exit `1`, name the temporary path and
  the direct installer command, and the fake-sudo log must not exist or be
  empty. If the wrapper reaches for `sudo` with no gateway installed, that is
  the test failing rather than an incident.
* **delegation** — a harmless executable exists at the temporary gateway path.
  The wrapper must exit exactly `42`, the fake `sudo` must be invoked exactly
  once, and its arguments must be exactly `-n`, the temporary gateway path,
  `deploy-main`. The old static `"exec sudo" in body` check proved none of that.

### The suite-wide audit

The whole module was audited, not just the failed test, for anything that
executes `update-main.sh`, `mgo-validate`, `install-mgo-validate.sh`, `sudo`,
`systemctl`, `runuser`, `curl`, `flock`, a network Git remote, or a production
path. The wrapper execution was the only unsafe host-reaching case. The
remaining executions are `bash -n` syntax checks, sourced functions with
explicit doubles, temporary repositories and paths, and direct executions of the
*repository copies* of the gateway and installer, which refuse an unprivileged
caller before they touch anything.

`test_no_test_can_reach_the_host_control_plane` now enforces this structurally,
by parsing the test module's own AST. It fails when a function that starts a
process also names the tracked wrapper, when what an executing call actually
runs resolves to the wrapper — including through a `parametrize` alias or a
local variable — when an installed path appears in command position inside
something the suite runs, or when a real privileged command is invoked. It is
deliberately not a substring search: the previous version of this promise was a
sentence in a docstring, and a search for `sudo` would have been satisfied by
its own assertion. A companion test asserts the audit's registers are not empty,
because the cheapest way to delete a rule is to empty the table it reads.

The module docstring was rewritten only after that enforcement existed.

### Mutations

Nine mutations were added and all are detected: executing the tracked wrapper
instead of its copy; leaving the production constant in the copy; removing the
fake-sudo `PATH` isolation; letting fake `sudo` run in the missing-gateway case;
changing the delegated action so the exact argument vector matters; discarding
the gateway's exit code; emptying the audit's executor register; permitting
`UPDATE_MAIN` to be executed; and permitting an executed
`/usr/local/sbin/mgo-validate`. Every one is caught by a static audit or by a
harness guard that refuses before a child process starts, so no mutation can
invoke a real `sudo` or reach a host control-plane path.

## Raspberry Pi staging validation — the successful second run

A second staging validation ran on 2026-08-04, after the test-isolation
correction, and **passed**. It is a separate, later event from the incomplete
first attempt recorded above, which stays classified as incomplete.

The run used the `claude` account on host `mgo-core` (`aarch64`) against a
disposable clone at `71d3755cd3807acffa09173fe743c9ef109faa9f`. The clone was
removed afterwards.

| Check | Result |
| ----- | ------ |
| Ruff | Passed |
| mypy | Passed — 50 source files |
| Gateway-focused suite | **654 passed** |
| Acceptance-document suite | **28 passed** |
| Full Pi suite | **2333 passed, 1 skipped** |
| The one skip | The intentionally single-branch disposable clone has no local `main` ref |
| Mutation register | **All 166 mutations detected** |
| Stale mutations | **Zero** |
| Outer command tripwire | Clean |
| Exec tracing | Clean |
| Real `sudo` executed | **None** |
| Installed gateway invoked | **Never** |
| `systemctl`, `runuser`, `flock`, `curl`, remote Git | None executed during guarded test phases |
| Unprivileged installer dry run | Exited zero |
| Production | Unchanged |
| Disposable clone | Removed |

The run was instrumented from outside the suite, because a suite cannot be the
only witness to its own isolation: a `PATH` directory of tripwire shims caught
`PATH`-resolved calls and `strace -f -e trace=execve` caught absolute-path
bypasses. Both were needed, and both were self-tested before the run.

## Power-failure baseline transition

A power failure rebooted the Pi at **2026-08-04 14:34:59 SAST**. Normal
filesystem journal recovery completed and the service started successfully at
**2026-08-04 14:35:10 SAST**.

The post-boot baseline used for the installation was:

| Fact | Value |
| ---- | ----- |
| `mgo.service` | Active |
| `MainPID` | `1494` |
| `NRestarts` | `0` |
| Database | Healthy, schema version 2, current, integrity ok |
| Camera | IMX708 available |
| Preview | Stopped |
| Preview PID | None |
| `rpicam-vid` | Zero |
| `libcamera-vid` | Zero |
| `preview.auto_start` | Remained `false` |
| Production branch | `main` |
| Production SHA | `1aec2245010a1bd971d028be235c1864af6b46b3` |
| Production tree | Clean |
| Configuration checksum | `8346e732c2545ff369f6c4f0e3fc2e415d10993d8fe6b4b1b2c67600555183da` |
| Archive/database records | 8 |
| Physical capture files | 5 |
| Approval file | Empty |

**The power failure, not the gateway installation, is what changed the
baseline.** The earlier records in this repository show `MainPID` `70709` and a
running preview process `71087`; both belong to the pre-power-failure service.
The reboot ended that process and, because `preview.auto_start` remains `false`,
the preview correctly did not come back. Attributing either change to the
installation would be wrong, and it would also be the more comfortable story,
which is exactly why it is written down here.

The installation preserved the post-power baseline: `MainPID` `1494` remained
`1494`, `NRestarts` remained `0`, stopped preview remained stopped, and zero
camera producers remained zero.

## Legacy sudoers retirement

On **2026-08-05** the legacy policy `/etc/sudoers.d/mgo-claude-validation` was
retired by an authorised root operator block. Its checksum was
`dd3f71c2f69c1c7dfbfd602aafd3ad9d9bf02d7686f9b656078ba99d9186333e` and it held
three directives:

```text
Defaults:claude !requiretty
claude ALL=(root) NOPASSWD: /usr/local/sbin/mgo-validate
claude ALL=(root) NOPASSWD: /usr/local/sbin/mgo-validate *
```

The third is a **wildcard grant**, which is why the policy could not remain
active beside the reviewed replacement.

Established by the retirement:

* the active legacy file was a regular, root-owned, mode `0440` file;
* the archived copy is byte-for-byte identical and passes `visudo` validation;
* the archive at `/root/mgo-legacy-sudoers-retirement-71d3755` is `root:root`
  `0700`;
* the active legacy file was removed;
* the complete active sudoers configuration remained valid;
* no other Claude or gateway sudoers reference remained immediately after
  retirement;
* the wildcard grant was **not** restored.

**The first retirement attempt stopped before any mutation.** The operator block
asserted two active directives; the file held three, because
`Defaults:claude !requiretty` was absent from the record the block was written
against. Under `set -Eeuo pipefail` the assertion ended the run before the
archive was created and before anything was removed. That is a **safe stopped
attempt, not a failed retirement transaction** — nothing was half-done, and the
host was exactly as it had been. The corrected block asserted all three
directives in order and completed.

## Root installation

On **2026-08-05**, from source SHA
`71d3755cd3807acffa09173fe743c9ef109faa9f`, an authorised root operator block
installed the reviewed gateway and its policy.

| Target | Checksum | Metadata |
| ------ | -------- | -------- |
| `/usr/local/sbin/mgo-validate` | `3e26a7cea23d15944f8f0b8c9949e3bb79ce555c95c64a4b45a174e038f0bf3e` | Regular non-symlink file, `root:root`, mode `0755`, shell syntax valid |
| `/etc/sudoers.d/mgo-validate` | `a34191d54a7139f107834861c53e6fa4db96501bfbea67ba457adc09055cbebc` | Regular non-symlink file, `root:root`, mode `0440`, passes `visudo -cf` |

The complete active configuration passes `visudo -c`.

Effective Claude privilege after installation:

* one `NOPASSWD` grant, for the exact gateway path;
* **no legacy wildcard rule**;
* **no `SETENV`**;
* command-scoped `env_reset`;
* eleven command-scoped `env_delete` groups;
* no arbitrary shell, no arbitrary `systemctl`, no arbitrary Git, no arbitrary
  `uv`, and no installer permission.

Installed state:

* `/run/lock/mgo-deployment.lock` is a root-owned `0600` regular file, and it
  can be acquired non-blockingly when idle;
* `/run/mgo-validate-install` is a root-owned `0700` directory, and the
  transaction parent is empty;
* the post-install root dry run reported both installed targets current, and
  inode, mtime, size, ownership and mode were unchanged across it;
* the approval file remained empty and unchanged.

## Bounded installed-gateway validation

The installed gateway was exercised only at its refusal boundary.

**`show-approval`** — exit `64`, stdout empty, no SHA printed, and a bounded
refusal for an empty or malformed approval file.

**The removed legacy `install` action** — exit `64`, stdout empty, and a bounded
message that distinguishes application deployment from service-identity
provisioning. No Git, service or preview action occurred.

**Negative sudo probes** — `/bin/true`, `/bin/bash -c true` and
`systemctl restart mgo.service` were each refused, and each exited non-zero. The
effective policy listing independently confirmed that the gateway path is the
only command grant.

**Five gateway or sudo probes were executed in total** — `show-approval`, the
legacy `install` action, and the three negative probes — together with **one
additional `sudo -n -l` policy-listing command**, run separately to establish
the effective policy from the unprivileged account. Six invocations, not four:
the count is written out because a probe count is the kind of number that gets
rounded in a summary and then cited as evidence.

That listing was needed because `/etc/sudoers.d` is mode `0750` and unreadable
to `claude`, so `test -e` on any path inside it is always false — it fails with
`EACCES`, not `ENOENT`, and a shell cannot tell the two apart. Absence inside
that directory can only be established from a privileged context or from the
effective policy, never from an unprivileged existence test.

## Production non-interference — the installation

After installation:

| Fact | Value |
| ---- | ----- |
| Production branch | `main`, unchanged |
| Production SHA | `1aec2245010a1bd971d028be235c1864af6b46b3`, unchanged |
| Production tree | Clean |
| Stash | Empty |
| Production worktrees | One |
| `mgo.service` | Active |
| `MainPID` | `1494`, unchanged |
| `NRestarts` | `0`, unchanged |
| Service activation timestamps | Unchanged |
| Database | Healthy and unchanged |
| Camera | Available |
| Preview | Stopped |
| Camera producers | None appeared |
| Configuration checksum | `8346e732c2545ff369f6c4f0e3fc2e415d10993d8fe6b4b1b2c67600555183da`, unchanged |
| Archive/database records | 8 |
| Physical capture files | 5 |
| Approval file | Empty and unchanged |

No `deploy-main`, no `restart-api`, no application-code deployment, no
production fetch, merge or `uv sync`, no service operation, no preview
transition, no capture, no stream access, and no image opened or decoded.

## Retained evidence and remaining limitations

Retained on the Raspberry Pi pending a separately reviewed closeout:

```text
/root/mgo-gateway-install-71d3755
/root/mgo-gateway-preinstall-backup-71d3755
/root/mgo-legacy-sudoers-retirement-71d3755
```

The unprivileged source clone was removed. Several unprivileged helper scripts
remain under `/tmp`. **None of this is cleaned by the record that documents it**
— root-evidence cleanup and `/tmp` helper cleanup require their own reviewed
instruction.

What is still not established:

* `deploy-main` has **not** been invoked through the new installed gateway;
* `restart-api` has **not** been invoked through the new installed gateway;
* no live application deployment has yet proved the installed transaction path;
* the approval file remains empty;
* physical camera acceptance remains pending;
* Matthew has **not** given visual camera sign-off;
* the 24-hour and 48-hour physical camera gates have **not** started;
* retained root evidence has not yet been cleaned;
* `retirement-evidence.txt` and `preinstall-evidence.txt` were created by
  successful root blocks but were not subsequently read.

An installed control plane is not a proven one. Everything above says the
gateway is in place and refuses correctly; the first authorised `deploy-main` is
what will say whether it deploys correctly.

## Boundaries

This task changes no application runtime, API, schema, camera, preview, capture,
motion, notification or database behaviour. It does not touch `src/mgo/`,
`config/`, `migrations/`, `pyproject.toml` or `uv.lock`.

Nothing in this repository installs itself. The gateway and sudoers changes
recorded above were made on the Raspberry Pi by separately authorised root
operator blocks, run by Matthew, and this record documents them after the fact.

The branch alters no approval file, deploys no application code, restarts no
service and begins no physical camera acceptance. The approval file is still
empty, `deploy-main` and `restart-api` have never run against the installed
gateway, and physical acceptance requires its own authorisation.

Two separately authorised staging-validation runs and two root operator phases
are recorded above. The first staging attempt is incomplete, it changed nothing
on the host, and the test-isolation correction that followed it was made
entirely in this repository with no Raspberry Pi access.

Task 13 is not begun.
