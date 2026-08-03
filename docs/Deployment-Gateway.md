# The deployment gateway

How application code reaches production, who decides that it may, and what
happens when a deployment fails halfway.

The gateway is `scripts/deploy/mgo-validate`, installed to
`/usr/local/sbin/mgo-validate`. It is the **only** supported way to move the
production checkout. There is no fallback and no second path.

## 1. Threat and failure model

This is a single Raspberry Pi on a trusted LAN, administered over SSH by one
person, running one service as one dedicated account. The gateway is not
defending against a determined attacker with a shell on the box — someone who
has that already has the camera. It defends against the failures that actually
happen to a system like this one:

| Failure | What the gateway does about it |
| ------- | ------------------------------ |
| Deploying a commit nobody approved | The approval SHA is the authority; a commit it does not name cannot be deployed |
| Deploying whatever `origin/main` happens to be *now* | The remote SHA is matched against the approval **before** the fetch |
| A rewritten or diverged history landing in production | Only a strict fast-forward from the deployed commit is accepted |
| A silent downgrade | A target behind the deployed commit is refused |
| Deploying over local edits | A dirty tree, an untracked file, a stash or an interrupted Git operation all refuse |
| Drifting off the locked dependency set | `uv sync --frozen`, never a resolving sync |
| Running the new code against the old environment | The sync happens before the restart, and the restart only if it passed |
| Reporting success when the API never came back | Recovery requires an active unit **and** a healthy endpoint, within a bound |
| Leaving production half-deployed | Any failure after the first mutation restores the captured commit, environment and preview state |
| Silently taking the camera away | A preview that was running is restored; one that was not is left alone |
| An operator reaching for a weaker path | The old `install` action is gone and the wrapper has no fallback |

The gateway is also deliberately *boring* about privilege: root is used for one
thing, and everything else is pushed back down to an unprivileged account.

## 1a. One control plane, one lock

Every mutating action — `deploy-main`, `restart-api` and the installer — takes
one exclusive lock before it reads anything mutable:

```text
/run/lock/mgo-deployment.lock
```

They contend for the same three things: the checkout, the service and the
camera. Without the lock, two `deploy-main` calls can both read the same old
`HEAD`, both pass the same approval and ancestry proofs, both fast-forward,
restart the service twice, race each other's preview restoration and then run
two incompatible rollbacks over one repository. A `restart-api` arriving
between the restart and the final verification is the same class of problem.

- **Non-blocking.** A busy control plane exits **75** immediately. It is never
  retried and never queued: the second caller cannot know what the first will
  do to the checkout, so waiting would only make the collision later.
- **Held for the whole action**, on a fixed descriptor nothing closes —
  including through rollback and final verification. A lock released between
  the restart and the final check is not a lock.
- **No PID file, no staleness protocol.** `flock` is released by the kernel
  when the holder exits, however it exits.
- `show-approval` is read-only and is never blocked.

**The lock file is itself a security boundary.** Any unprivileged process that
can open it read-only can hold an exclusive `flock` on it and deny every
deployment and restart indefinitely. So it must be a **root-owned `0600`
regular file** reached through a real directory — never a symlink, directory,
FIFO, socket or device, and never group- or world-readable. That is verified
*before* the lock is taken.

When absent it is created under `umask 0077` with `noclobber`, so it is `0600`
from the moment it exists and two simultaneous first-run callers cannot both
create it — the loser validates the winner's file rather than replacing it. An
unsafe object is **refused, never followed and never replaced**: replacing it
would silently drop whatever lock a legitimate holder has on the old inode. The
installer may tighten the mode of an existing root-owned regular lock file
during the first installation, and may never touch its inode.

**First installation is the exception.** The gateway currently on the Pi
predates this lock and knows nothing about it, so the separately authorised
first installation must happen with no deployment or restart in progress.
After that, the gateway and the installer share the lock permanently.

## 2. The approval file is the authority

```text
/etc/garden-observatory/claude-approved-sha
```

Root-owned, not group- or world-writable, and containing exactly one line of
exactly forty lowercase hexadecimal characters — nothing else. No branch name,
no path, no comment, no second line, no surrounding whitespace.

The parse is **byte-exact**, anchored on the file's own length rather than on a
newline count. That distinction matters: the bytes `<valid sha>\nmain` with no
final newline contain exactly one newline, so a line count passes and reading
the first line returns a valid SHA — while a whole trailing line is silently
ignored. A file must therefore be exactly 40 bytes, or 41 with a single final
LF, and that final byte is compared as a hex value because command substitution
silently drops a NUL and would otherwise let a trailing NUL pass as a newline.
CR, CRLF, embedded control bytes, trailing spaces and trailing data are all
refused for the same reason.

**Only Matthew writes this file.** The gateway only ever reads it, and no action
it exposes can change it. Installing an approval is how a deployment is
authorised; clearing it is how that authorisation is withdrawn.

Malformed or unsafely permissioned approval state exits **64** and is never
silently normalised. Tidying up the authority would mean the thing deciding what
may be deployed is whatever the parser was willing to salvage.

```bash
sudo -n /usr/local/sbin/mgo-validate show-approval
```

## 3. Boundaries

**Caller.** Exactly one account — `claude` — may invoke the gateway, through
`sudo`, and the sudoers rule grants that account exactly one absolute path. The
gateway independently verifies that it is running as root and that the recorded
sudo caller is the expected account.

**Input.** The action word, and nothing else. No repository path, service name,
ref, remote, branch, command or username is accepted from the caller; every
production value is a fixed constant in the script. Extra arguments are refused.
There is no `eval` anywhere, and the approval file's content is never evaluated
as shell input.

**Repository.** One fixed path, which must resolve without traversing a symlink,
must be on `main`, must be clean, and must have `origin` pointing at this
repository.

## 4. The three actions

| Action | Does | Never does |
| ------ | ---- | ---------- |
| `show-approval` | Prints the approved SHA on stdout, alone | Anything else |
| `deploy-main` | Deploys `origin/main` at the approved SHA, transactionally | Deploys any other ref, merges, rebases, resets forward, pushes |
| `restart-api` | Restarts the service at the already-deployed approved SHA, on **whatever branch is checked out** | Fetches, merges, syncs, starts preview, captures |

`restart-api` is deliberately **branch-aware** where `deploy-main` is
main-only. It also serves separately authorised Pi validation of an approved
feature SHA, and tying it to `origin/main` would push that work back to
bespoke, hand-written restart instructions — the very thing this gateway
exists to remove. It requires a named branch (not detached) whose `HEAD` is the
approved SHA, tracking the **identically named** branch on the expected remote,
also at the approved SHA, with a clean tree including untracked files, no
stash, no operation in progress and exactly one worktree. For `main` that is
precisely the old `origin/main` requirement. The branch is discovered from the
checkout and is never accepted from the caller, and the action still fetches,
merges and syncs nothing.

Nothing else exists. An unsupported action exits 64 before any privilege is
used.

## 5. Why `install` is gone

The previous gateway had an `install` action. It was pinned to
`task-010-operations`, required the checkout to *be on* that branch, and invoked
the service-identity provisioner. It never fetched application code and never
advanced the checkout — so despite its name, it could not deploy.

On 2026-08-01 the Task 12 deployment tried it. It exited 128 inside its own
precondition check when `origin/task-010-operations` no longer existed.
Production was untouched, which was the right outcome, but there was no
repository-managed way to deploy at all; production was moved to the merged SHA
by a bespoke, individually authorised sequence of direct Git commands.

That deployment was correct and verified. The problem was that it could not be
repeated without another hand-written authorisation. Invoking `install` now
fails with a message naming `deploy-main` for application code and
`scripts/deploy/install-service-identity.sh` for service identity. **Deploying
code and provisioning service identity are different operations and no longer
share a verb.**

## 5a. The caller's environment is an input surface

Everything this gateway runs gets an environment it **constructed**, not one it
inherited.

Commands run as `claude` or `mgo` go through `env -i`, which empties the
environment first — so `GIT_DIR`, `GIT_WORK_TREE`, `GIT_INDEX_FILE`,
`GIT_CONFIG*`, `GIT_SSH*`, `UV_PROJECT`, `UV_CONFIG_FILE`, `VIRTUAL_ENV`,
`PYTHONPATH`, `PYTHONHOME`, `CURL_HOME`, `TMPDIR` and every proxy variable are
gone by construction rather than by remembering to remove them. Any one of them
could point a root-invoked deployment at a different repository, index,
configuration, interpreter or remote.

Each account gets exactly what it needs: `HOME` **read from the account
database and proven absolute** (never the caller's), `USER`, `LOGNAME`, a fixed
`PATH` and `LC_ALL=C`; the runtime probe additionally gets
`MGO_CONFIG_PATH`. SSH to the expected repository still works, because the
deployment key lives in the account's own fixed home.

The root side sets `HOME=/root`, the fixed safe `PATH`, `LC_ALL=C` and a fixed
root-owned `TMPDIR` (`/run/mgo-validate-tmp`, `0700`), and explicitly unsets the
whole list above. `TMPDIR` matters more than it looks: it decides where response
bodies and staging files are written.

`curl` is invoked with `--disable` as its **first** option, so no `.curlrc`
belonging to root or anyone else can add `--location`, change the proxy or alter
the timeout behind the gateway's back — plus `--no-location` and
`--max-redirs 0` for good measure.

Request validation happens before any of this: an unsupported action is refused
before the process so much as creates a directory.

## 6. Why Git and `uv` run as `claude`

The production checkout is owned by `claude:mgo`. Running Git or `uv` as root
would leave root-owned objects and virtual-environment files inside a tree the
unprivileged account has to keep managing — the next ordinary `git status` or
`uv sync` would then fail, and the fix would be another privileged command.

Running them as `mgo` would be worse: the runtime account would gain write
access to the code it executes.

So the gateway raises privilege only to lower it again. Every Git and `uv`
invocation goes through one `runuser` helper, and the sudoers rule grants the
runtime account nothing at all.

## 7. Why the restart runs as root

`systemctl restart mgo.service` is the one operation that genuinely requires
privilege. Granting `claude` arbitrary `systemctl` through sudoers would be a
much wider grant than the job needs, so the narrow grant is *this script*, and
the script performs exactly that one restart.

## 8. Strict fast-forward only

Two separate facts are proven before the checkout moves:

- the deployed commit is an ancestor of the approved SHA — the move is a
  fast-forward;
- the approved SHA is not behind the deployed commit — this is not a downgrade.

`reset` is used in exactly one place, the rollback path, because going backwards
is not a fast-forward and cannot be expressed as one. The forward deployment
never resets, never rebases and never merges anything but a fast-forward.

## 9. Frozen dependencies

`uv sync --frozen`, always. A resolving sync would let production install a
dependency set that the lockfile never recorded and the test suite never ran
against. A missing `uv`, a failed sync, or a sync that modified a tracked file
each stop the deployment — none of them is a degraded success.

## 9a. Runtime readiness

Before the restart, the gateway proves the **runtime account** can actually
load the deployed code: that the deployed interpreter and launcher are
executable, and that `mgo.core.config` and `mgo.api.app` import as `mgo` with
the production configuration selected.

An executable bit on the launcher proves almost nothing. Without this, a
missing module or an unreadable path surfaces as `203/EXEC` or an `ImportError`
*after* the code has already moved, and the diagnosis happens in the journal
instead of in the deployment.

It is a probe, not a rehearsal: import only. No lifespan is entered, no camera
is opened, no stream is read and nothing is written. A failed probe takes the
pre-restart rollback path.

## 10. Preview-state preservation

`preview.auto_start` is `false` in production, so a restart leaves preview
stopped. That is correct behaviour, and it is also a small debt: if preview was
running before the deployment, the deployment took the camera away.

So the gateway records the preview state **before** it touches anything, and
afterwards:

- preview was running, and is now stopped → exactly one `POST /camera/preview/start`, then poll until it reports running, then require exactly one `rpicam-vid`;
- preview was running, and already is → no request at all;
- preview was **not** running → it must still not be running, and there must be **zero** producers.

Preservation of a non-running preview is *proven*, not assumed. Returning
success without looking would make "left alone" mean "not checked" — so a
previously stopped or failed preview that is now running, or a producer alive
behind a preview that reports stopped, is a **failure** that takes the
post-restart rollback path.

Drift into running is deliberately not papered over with a stop request. A
camera that started itself is a fault to surface, and issuing an unasked-for
stop would be a second unrequested mutation on top of the first.

A `libcamera-vid` in any final state is a failure: something other than this
deployment is holding the camera.

Completing a deployment is never a reason to start a camera nobody asked for.
The gateway **never** opens the preview stream, never inspects a frame and never
calls the capture endpoint — deployment restores an operating state, it does not
exercise the camera.

## 10b. The preview baseline

Before anything moves, the gateway establishes what the camera is actually
doing — and refuses to proceed if it cannot.

The status document is **parsed as JSON** by the system interpreter
(`/usr/bin/python3`), reading the response file directly. Pattern matching is
gone: a `grep` could find `"state":"running"` inside a truncated or
garbage-padded response and hand it back as a deployment baseline, and it could
not tell a top-level field from one nested inside `camera`. The parser requires
valid UTF-8, no NUL, a JSON **object** at the top level with no leading or
trailing data, exactly one top-level `state` key — duplicates are refused even
when they agree — and a string value. Nested `state` keys never substitute for
the real one. The application is deliberately not imported to do this: parsing
a response must not depend on the code being deployed.

There is **no fallback state**. A missing, empty, duplicated, conflicting or
unrecognised `state` field is a refusal, not an "unknown": inventing a state
would make that invention the baseline an entire deployment measured itself
against.

Only three states are settled enough to deploy against — `running`, `stopped`,
`failed`. The transient pair, `starting` and `stopping`, stop the deployment
before mutation: a camera mid-transition is not a baseline.

The reported state is then reconciled against the running processes:

| State | Required |
| ----- | -------- |
| `running` | exactly one `rpicam-vid`, zero `libcamera-vid` |
| `stopped` | zero producers |
| `failed` | zero producers |

A disagreement is **refused, never repaired**. It means the camera is not in
the state anyone thinks it is, and a deployment that later "restored" such a
baseline would be restoring a fiction. Preflight observes only — it never
starts or stops preview.

The same vocabulary applies during restoration and final verification: a
malformed status response is a failure there too, not a truthful non-running
state.

## 10a. Final verification

After preview restoration and **before anything claims success**, the gateway
re-reads the whole picture and only then concludes the deployment landed:

approval still parses and still names this commit · branch is `main` · `HEAD`,
local `main` and `origin/main` all equal the approved SHA · the tree is clean
**including untracked files** · no stash · no Git operation in progress · the
service is active · health answers exactly 200 · preview matches the recorded
pre-deployment operating state · the producer count matches it · no
`libcamera-vid`.

Every earlier step checked its own outcome, which is not the same as checking
the *result*. A failure here is a deployment failure like any other and takes
the post-restart rollback path — and `deployed` is never printed before it
passes.

Cleanliness is checked with `--untracked-files=all` everywhere — after the
sync, during rollback verification and here. An untracked file is drift the
deployment must not carry, though ignored paths such as `.venv` remain ignored.

## 11. Transaction rollback

The pre-deployment commit, preview state, service PID and activation timestamp
are captured before the first mutation. Only those captured values are ever
restored to. **A rollback target is never accepted from a caller.**

**The transaction opens before the merge is invoked, not after it succeeds.** A
non-zero exit from Git is not proof that nothing moved: the command can fail
part-way through a checkout, on a permission or I/O error, or after the ref has
already been updated. So a failed `merge --ff-only` enters the rollback path
like any other failure. Immediately after a *successful* merge — and before the
environment sync — the checkout is verified (branch, `HEAD`, local `main`,
`origin/main`, clean including untracked, no operation in progress); that
failing rolls back too.

| When it fails | What happens | Exit |
| ------------- | ------------ | ---- |
| Before the merge is invoked | Nothing to undo | 64 / 65 / 75 |
| The merge itself, or the check straight after it | Checkout and environment restored; **no restart** | 70 |
| After the fast-forward, before the restart | Checkout and environment restored; the service is **not** restarted and the preview is **not** touched, because nothing disturbed them | 70 |
| At or after the restart | Checkout and environment restored, the service restarted **once**, health proven, preview returned to its recorded state | 70 |
| The rollback itself | Evidence preserved, no loop, no repeated restart, the failed stage named, and no claim that production was restored | 78 |

A successful rollback still reports failure. `deployment failed; rollback
succeeded` means production is where it started — not that the deployment
worked.

A pre-restart rollback proves rather more than that the commit came back. The
service was never restarted, so it should still be the *same* process, still
serving, with the camera exactly as it was found — and a PID and a timestamp do
not say that. It therefore verifies branch, `HEAD`, local branch, a clean tree
including untracked files, no stash, no operation in progress, the unchanged
PID and activation timestamp, an active service, an exact-200 health response,
the preview state matching the captured baseline, and the producer count
matching it. Any one of those failing means the rollback is incomplete: **78**.

Exit codes: **64** bad request or unusable approval, **65** precondition
failure, **75** another control-plane action holds the lock, **70** deployment
failed and production was restored, **78** deployment failed **and**
restoration failed — production state is not known to be good and needs a
person.

## 12. Already current

When `HEAD`, `main`, `origin/main` and the approved SHA are all the same commit,
`deploy-main` says so and exits successfully without changing the checkout,
running `uv sync`, restarting the service or touching preview. Running it twice
is safe.

## 13. Installation

```bash
sudo bash scripts/deploy/install-mgo-validate.sh --dry-run   # report only
sudo bash scripts/deploy/install-mgo-validate.sh             # install
```

The installer validates the gateway with `bash -n` and the policy with
`visudo -cf` **before** installing either — an invalid file under
`/etc/sudoers.d` can lock every account out of `sudo` on the host. **A dry run
validates too**: it promises to validate everything, so a host without `visudo`
fails in both modes rather than reporting a success it did not earn.

Both files are written atomically, `root:root`, `0755` for the gateway and
`0440` for the policy, then verified on disk by checksum, owner and mode, and
the *installed* policy is re-validated with `visudo -cf` before success —
validating what was about to be written is not the same as validating what
landed.

The transaction records **both** previous states before the first mutation,
and "absent" is a recorded state whose restoration is removal. Any failure
after that point — a temporary file, a copy, a rename, either checksum, either
metadata check, or the installed-policy validation — restores **both** targets.
The host is never left with half a control plane. A restoration that itself
fails exits **78** and says so; it never claims the host is clean.

Every mutating command checks its own status. None of this relies on `errexit`
inside a function invoked as an `if !` condition, where Bash disables it — an
unchecked failure there would fall through to the rename and publish a
truncated file that `sudo` would happily execute.

The installer is idempotent in the strict sense: when both files are already
correct in content **and** owner **and** mode, and the installed policy is
valid, it verifies and exits before creating a temporary file — same inode,
same modification time, nothing written. Matching content with the wrong owner
or mode is *not* current; it is a defect, and it is repaired transactionally.

It never touches the approval file, the repository or the service. It
provisions the deployment control plane and nothing else.

## 14. Sudoers boundary

```text
claude ALL=(root) NOPASSWD: /usr/local/sbin/mgo-validate
```

One account, one absolute path. No shell, no arbitrary `systemctl`, no arbitrary
Git or `uv`, no installer, no wildcard over a directory, nothing for the `mgo`
runtime account and nothing for any group.

The rule carries no argument pattern on purpose. Sudo argument matching is
textual and would have to be kept in step with the script forever. The gateway's
own action parser is the command boundary: one word, from a closed set.

## 15. Routine deployment

1. Merge to `main` and push, then confirm GitHub's `main` is the commit to deploy.
2. **Matthew** installs that SHA in `/etc/garden-observatory/claude-approved-sha`.
3. On the Pi, as `claude`:

```bash
sudo -n /usr/local/sbin/mgo-validate show-approval
```

Confirm it prints the intended SHA, then:

```bash
bash scripts/deploy/update-main.sh
```

or equivalently `sudo -n /usr/local/sbin/mgo-validate deploy-main`. The wrapper
adds nothing but a friendlier name and a clear error when the gateway is absent.

4. Read the output. A successful deployment reports the recovery time, the new
   `MainPID` and the activation timestamp.
5. **Matthew** clears the approval file when the deployment window closes.

## 16. Failure and recovery

| Exit | Meaning | What to do |
| ---- | ------- | ---------- |
| 64 | Bad request, or the approval file is missing, malformed or unsafely permissioned | Fix the approval file; the message says which property failed |
| 65 | A precondition failed — dirty tree, wrong branch, stash, operation in progress, wrong remote, service down, remote SHA does not match the approval, or not a fast-forward | Resolve the named condition. Nothing was deployed |
| 70 | The deployment failed and production was restored | Read the reason, fix it, deploy again. Production is where it started |
| 78 | The deployment failed **and** the rollback failed | **Stop.** The message names the stage that failed. Do not re-run the gateway; inspect the checkout, the service and the journal by hand |

The gateway never retries by itself and never loops. One deployment attempt, at
most one rollback attempt, then it reports and stops.

## 17. Application deployment versus service-identity provisioning

Two different operations, two entry points, two names:

| Concern | Script | Run as | Frequency |
| ------- | ------ | ------ | --------- |
| Move the application code to an approved commit | `mgo-validate deploy-main` | `claude` via sudo | Every release |
| Provision the runtime account, directories and systemd units | `scripts/deploy/install-service-identity.sh` | root | Rarely, and by hand |
| Install the deployment control plane itself | `scripts/deploy/install-mgo-validate.sh` | root | Rarely, and by hand |

Conflating the first two is what produced the defect this document exists to
describe.

## 18. What deployment never does

No capture is taken. The preview stream is never opened and no frame is ever
inspected. No image is read, saved or decoded. Production configuration is never
edited, `preview.auto_start` and `preview.restore_after_capture` are never
enabled, motion settings are never changed, and camera geometry, focus,
exposure, AWB, ROI and lens position are never touched.

Deployment moves code and restores the operating state it found. Everything
about what the camera can *see* belongs to physical acceptance, which is a
separate, separately authorised activity.
