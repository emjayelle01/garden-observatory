# Task 12 remediation — approved deployment gateway

## Status

**Entry-boundary corrections implemented; awaiting final confirmation. The
gateway is not installed, there has been no Raspberry Pi validation, and the
production gateway is unchanged. Physical camera acceptance remains pending.**

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
| Final confirmation (re-run) | **Not performed** |
| Installation on the Raspberry Pi | **Not performed** — requires re-review and a separately approved SHA |
| Raspberry Pi validation of the gateway | **Not performed** |

Nothing here has run on the Raspberry Pi. The gateway installed there is still
the Task 10 one, and physical camera acceptance remains pending in full.

Nothing in this task changes the gateway that is installed on the production
Raspberry Pi. It changes what the *repository* ships. Installing it is a
separate, separately authorised act.

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

`/usr/local/sbin/mgo-validate` on the Pi is not repository-managed — no copy of
it is tracked here — and it is pinned to Task 10:

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

## Boundaries

This task changes no application runtime, API, schema, camera, preview, capture,
motion, notification or database behaviour. It does not touch `src/mgo/`,
`config/`, `migrations/`, `pyproject.toml` or `uv.lock`.

It does not access the Raspberry Pi, install the gateway, alter sudoers, alter
the approval file, deploy anything, restart any service, or begin physical
camera acceptance. Those require repository review and separate authorisation.

Task 13 is not begun.
