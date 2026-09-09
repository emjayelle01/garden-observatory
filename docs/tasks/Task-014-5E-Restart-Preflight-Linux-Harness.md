# Task 14.5E — `restart-api` runtime preflight and Linux test-infrastructure hardening

**Status: implementation complete and validated in a standalone clone on
Windows; pull request open for independent review; native-Linux evidence
recorded on the pull request.**

**Not merged. No production change. No gateway installation. No deployment.
No approval. The production gateway on `mgo-core` remains the pre-PR #17
build, and the Task 14.5B deployment retry remains BLOCKED until this pull
request is reviewed and merged and the installed gateway is aligned under its
own authority.**

---

## 1. Scope

Three bounded findings from the native-Linux review of PR #17 (Task 14.5D-R),
closed in the repository:

1. `restart-api` could restart a clean but runtime-unreadable checkout,
   because it had no runtime-account preflight;
2. three tests failed on POSIX at the PR base -- two retention CLI tilde-path
   tests and one installer carriage-return test;
3. the mutation runner could reuse stale same-size, same-second bytecode and
   report a survivor for code it never ran.

Nothing else. No new gateway action, no sudoers change, no installer change,
no application code, no migration, no dependency, no configuration.

---

## 2. Context

- **Task 14.5B (2026-09-07).** `deploy-main` was invoked from a wrapper whose
  umask was `0077`. The fast-forward and the rollback both wrote every touched
  file `0600 claude:mgo`. Git reported the tree clean; `mgo` could not read
  it. The service survived because it was never restarted.
- **Task 14.5B-R.** Root repaired 46 files to `0644` by an itemised action
  and proved the import as `mgo`; the service stayed on its original PID.
- **Task 14.5C / PR #17 (merged 2026-09-08 as `eecc4286…`).** The gateway
  owns its publication umask (`0022`), validates the runtime as `mgo` before
  anything moves and after every restoration, and writes no bytecode while
  doing so. Proven on native Linux/ARM64 with real modes and a genuine-root
  microprobe.
- **The remaining gap.** `restart-api` still went from "the checkout is the
  approved commit" straight to `systemctl restart`. Against the 14.5B
  checkout it would have stopped a serving process and started one that could
  not import.

---

## 3. Finding A — `restart-api` preflight

### The control flow as found

`action_restart_api`: take the control-plane lock; read and validate the
approval; `require_restart_preconditions` (repository present, canonical path,
named branch, HEAD at the branch tip, clean tree including untracked files, no
stash, no operation in progress, one worktree, expected remote, HEAD equals
the approved SHA, upstream `origin/<branch>` at the approved SHA); `systemctl
cat` proves the unit is installed; then `restart_service`, `await_recovery`
(exit 70 if the bound is missed), and the MainPID and activation timestamp are
logged. The approval is read and never consumed; the lock is released by the
kernel at exit. Every one of those checks is a question to Git or to systemd
about the unit *file*; none asks whether `mgo` can execute the checkout.

### The change

At the last point before service control -- after the unit check and before
`log "restarting"` / `restart_service` -- `restart-api` now calls the merged
`require_runtime_can_execute "$MGO_RUNTIME_ACCOUNT" "$MGO_REPOSITORY"`: the
same probe `deploy-main` makes, as `mgo`, through `runuser` into a constructed
environment, with the production configuration selected, importing
`mgo.core.config` and `mgo.api.app` with the deployed interpreter under `-B`
and `PYTHONDONTWRITEBYTECODE=1`. A refusal is `die "$EX_PRECONDITION"` (65)
with a message stating that the service was not restarted and is still running
the process it was found with. Nothing below the call can be reached without
its answer, so a refused preflight issues no stop, restart, reload, signal or
health wait. The probe function is reused, not duplicated.

### Evidence

`tests/test_deployment_restart_preflight.py` drives the shipped
`action_restart_api` against the disposable production of the umask-safety
suite (a real Git checkout, a real interpreter behind a launcher) with
`systemctl` recorded at the seam and the shipped `restart_service` body put
back so the only route to a restart is the real one. It proves: a readable
runtime restarts exactly as before; the probe runs after every read-only
precondition and immediately before the restart; it runs as `mgo` with both
bytecode guards and writes no bytecode; an unreadable source, an untraversable
package directory, an interpreter that cannot start, an absent interpreter and
a configuration that cannot be imported are each refused with 65 and **zero**
service actions while the tree is clean and approved; the approval is read
once and never cleared; the repository is untouched; the expected-SHA and unit
checks still precede the probe; and the caller's umask (`0022` or `0077`)
changes nothing. Against the pre-change gateway the unreadable-source scenario
exits 0 and reports `MainPID 4242`: the finding, reproduced.

Eight register entries (`restart-preflight-*`, `restart-proceeds-after-failed-
preflight`) remove the preflight, move it after the restart, run it as
`claude` or root, ignore its failure, restart anyway, change its exit status,
or strip its bytecode suppression; each is detected by that module.

---

## 4. Finding B — POSIX test portability

### Retention CLI tilde tests

`test_a_tilde_path_would_otherwise_have_expanded_to_an_absolute_one` and
`test_the_tilde_refusal_does_not_echo_the_supplied_value` failed on Linux for
the `~someone/mgo.toml` case only. Root cause: the *tests* called
`Path(value).expanduser()`, and `~user` is where the platforms differ.
`ntpath` derives it from `USERPROFILE` without asking whether the account
exists; `posixpath` asks the account database and returns the value unexpanded
when it does not, which `Path.expanduser` turns into `RuntimeError`. `someone`
has no account on the Pi. The production gate was correct throughout: it
refuses `~` forms *before* expanding them.

Correction, in the tests only: a `_deterministic_homes` helper points `HOME`,
`USERPROFILE` and `USERNAME` at a directory under `tmp_path` and, where the
`pwd` module exists, answers `getpwnam` for exactly the one named account with
a directory under `tmp_path`, refusing every other name. No real home is read,
no host account is needed, and the expansion is asserted to land inside
`tmp_path`. The general resolver's tilde test uses the same helper. Nothing is
skipped, xfailed or weakened; the contract that the old gate *would* have
accepted these values is still what is tested.

### Installer carriage-return test

`test_a_template_with_carriage_returns_is_refused` converted the whole
template to CRLF. The installer's `validate_unit_structure` checks structure
first with whole-line anchors (`^\[Unit\]$` and so on) and the carriage-return
check last, so on Linux `[Unit]\r` failed as `missing [Unit] section` before
the CR check was reached. The fixture carried two defects and could not say
which one a refusal was for. Error precedence is not a documented interface.

Correction, in the tests only: the fixture now carries exactly one CR, at the
end of the `Description=` line, which no structural check anchors, and asserts
that the refusal names carriage returns and not structure. Two companions
prove structure independently: a CRLF-throughout template is refused as
`missing [Unit] section` (Linux), and a template without its `[Unit]` line and
no CR anywhere is refused for its structure on every host. The CR rejection,
the structural checks and the installer itself are unchanged.

---

## 5. Finding C — bytecode-safe mutation execution

Root cause: a `.pyc` is validated against its source by whole-second mtime and
byte length. Consecutive candidates rewriting one Python source to the same
length inside one second are indistinguishable to the import system, so the
second candidate ran bytecode compiled from the first -- or from the committed
source -- and its targeted tests passed against code that was never run.
`PYTHONDONTWRITEBYTECODE=1` or `-B` alone stop *writing*, not *reading*; the
14.5D-R rerun passed only because that run happened to write nothing.

Correction, in `scripts/dev/run-mutations.py`: every candidate's interpreter
starts with `-B`, `PYTHONDONTWRITEBYTECODE=1` and a fresh, empty, uniquely
named `PYTHONPYCACHEPREFIX` under a task-owned root created with
`tempfile.mkdtemp`. With a cache prefix set the import system never consults
the `__pycache__` beside a source, so pre-existing bytecode is never read. The
candidate cache is removed afterwards by its resolved literal path, refusing
anything that is not a direct child of the root; the root is removed when the
run ends. The restore still comes first in the `finally`, before the cache is
touched. No sleep, no timestamp spacing, no repository cache is removed, and
every existing mutation ID and anchor is unchanged.

`tests/test_mutation_runner.py` states the condition as a fact: the source's
mtime is pinned to a whole-second instant before the bytecode is written and
to a later fraction of the same second after the source changes to the same
length. It proves the stale bytecode is what runs without isolation, that
`-B` plus `PYTHONDONTWRITEBYTECODE=1` alone still read it, that a candidate
reads its current source and writes nothing, that the prefix is what redirects
the read, that caches are unique, task-owned and removed by literal path, that
an interrupted candidate restores the asset and removes its cache, and -- end
to end through the runner's own `_apply` over a disposable two-file project
with every write held inside one second -- that the previous invocation reports
`NOT DETECTED` for a mutation it never ran while the shipped invocation
detects it in either order and leaves no `.pyc` anywhere in the tree.

---

## 6. What this task does not do

No production gateway installation; no deployment; no approval installed or
cleared; no service restart, stop, reload or signal; no reboot; no production
repository mutation; no production dependency sync; no migration; no database
or configuration change; no backup, restore or pruning; no retention-unit
installation; no daemon reload; no retention, motion or event-capture
enablement; no capture; no media access; no merge or auto-merge; no daylight
commissioning; no Task 15 work. Production gateway alignment and the Task
14.5B deployment retry remain separately authorised tasks.
