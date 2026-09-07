# Task 14.5C — Gateway umask containment and rollback readability safety

**Status: implementation complete and validated in a standalone clone; pull
request open for independent review.**

**Not merged. Not installed. No Raspberry Pi access. No production change. No
deployment. The Task 14.5B deployment retry remains BLOCKED until this
correction is reviewed, merged and the installed gateway on `mgo-core` is
re-aligned to it.**

---

## 1. Scope

One correction to `scripts/deploy/mgo-validate`, the deployment gateway: it
must publish runtime files under its own umask, and it must not call a
rollback successful until the runtime account can execute what was restored.

Nothing else. No new gateway action, no sudoers change, no installer change,
no application code, no migration, no dependency, no configuration, no
permission repair.

---

## 2. The incident

Task 14.5B, 2026-09-07 15:56Z. `deploy-main` was invoked exactly once to move
production from `c416b6bf7ddcbbedcf8ebcc8af2cdba8b7e1425d` to merged `main`
`b733d0b88032c082d277432a410f1ec8ba2a38fe`. The wrapper that invoked
`sudo -n /usr/local/sbin/mgo-validate deploy-main` had set `umask 077` to
protect its own log stage.

What happened, in the gateway's own terms:

1. `sudo` preserved the caller's umask (it only ORs in `0022`).
2. The gateway set no umask of its own for the administrative runner. Its two
   `umask 0077` statements are subshells around the lock and the temporary
   directory, and nothing else.
3. `git merge --ff-only`, run as `claude` through that runner, created every
   file the target touched as `0600 claude:mgo`.
4. `require_runtime_can_execute` -- correctly -- found that `mgo` could not
   import `mgo.core.config, mgo.api.app`, and the pre-restart failure path was
   taken.
5. `rollback_repository` reset the checkout to `c416b6bf`, resynchronised the
   environment, proved `HEAD`, branch and a clean tree, and returned success.
   It rewrote the same files under the same umask.
6. The gateway exited 70: `deployment failed; rollback succeeded; the service
   was never restarted`.

Every word of that was true, and the restored checkout was as unreadable to
`mgo` as the deployed one. Git reported it clean because Git tracks the
executable bit and nothing else. The service kept serving only because its
modules were already in memory; a restart would have failed at import.

Task 14.5B-R (17:33Z) found 34 tracked files and 12 bytecode files at `0600`
-- the bytecode from a diagnostic import under the same umask -- and restored
exactly those 46 to `0644` by an itemised root action, then proved the import
as `mgo` with bytecode writing disabled. Production stayed on PID `435158`,
`NRestarts=0`. That was the operational correction; this task is the code
correction, and the deployment must not be retried on the strength of the
first alone.

---

## 3. Root cause

Two defects, both in the gateway and neither in the wrapper:

- **The publication umask was the caller's.** The privileged gateway
  delegated the single property that decides whether the runtime account can
  read what it deploys -- the mode files are created with -- to whatever shell
  happened to invoke it. A wrapper that chose a safe umask was not a fix; it
  was a coincidence the 14.3L deployment had enjoyed.
- **Rollback proofs were Git proofs.** `rollback_repository` verified commit,
  branch and cleanliness and called that restoration. The property that
  mattered -- can `mgo` load this -- was proven for the deployed target
  (§9a) and never for the restored one.

---

## 4. The correction

All in `scripts/deploy/mgo-validate`; anchors are function names.

| Change | Where |
| --- | --- |
| `MGO_PUBLICATION_UMASK="0022"`, a fixed constant beside the other production constants | after `MGO_ROOT_HOME` |
| `publish_as_admin`: a subshell that sets the publication umask and calls the existing `run_as_admin`; `git_admin_publish` on top of it for the two working-tree transitions | after `git_admin` |
| The fast-forward uses `git_admin_publish` | `action_deploy_main`, step 8 |
| The rollback reset uses `git_admin_publish` | `restore_checkout` |
| The frozen sync uses `publish_as_admin` | `sync_environment` |
| The checkout as found is validated as `mgo` before the previous build's schema is asked and before the fetch; failure is exit 65, nothing moved | `action_deploy_main`, after `require_uv_available` |
| `rollback_repository` takes the runtime account and gains a `runtime` stage after `verification`: the restored checkout must import as `mgo` before the function returns success | `rollback_repository` |
| `report_rollback_failure`: the `runtime` stage is reported as an **INCOMPLETE** rollback that names what was restored, what was not, and that the service must not be restarted; every other stage keeps its existing message; exit 78 throughout | before `fail_before_restart` |
| Both failure handlers pass `$MGO_RUNTIME_ACCOUNT` and report through `report_rollback_failure`; the post-restart handler restarts only after the restored build has been proven loadable | `fail_before_restart`, `fail_after_restart` |
| Every probe that imports the application as `mgo` runs with `-B` and `PYTHONDONTWRITEBYTECODE=1` | `require_runtime_can_execute`, `build_supported_schema`, `database_schema_version` |

What did not change: the four public actions; the sudoers policy and the
installer (byte-identical); the lock (`umask 0077`, `0600`), the temporary
directory (`umask 0077`, `0700`) and the approval temporary (`mktemp` and
`chmod --reference`); the schema-aware refusal and exit 79; the recovery
authority split of `docs/Operations.md` §6.2. No `chmod` was added anywhere.
The publication umask lives in one subshell and the gateway's own shell keeps
whatever it inherited.

---

## 5. Reproduction before implementation

`tests/test_deployment_umask_safety.py` drives the real `action_deploy_main`
-- the shipped shell, sourced -- against a disposable upstream, checkout,
interpreter launcher and `uv` double, from a caller whose umask is `0077`.
Every seam through which the gateway's fixed production path leaves the
process is a double that maps it onto the disposable checkout; no production
path is ever in command position, and the module audits its own AST for that.

Run against unmodified `main` at `b733d0b8`, the incident scenario produced,
byte for byte, the incident:

```text
mgo-validate: deployment failed before the restart: the runtime account cannot execute the deployed environment
mgo-validate: restoring <previous>
mgo-validate: deployment failed; rollback succeeded; the service was never restarted
exit 70
```

with the kernel model recording every rewritten file at `600` and the
simulated runtime account refused after the rollback. Twenty of the module's
twenty-four tests failed against the unmodified gateway; all twenty-four pass
against the corrected one.

Two hosts, one contract. Where the kernel honours umask (Linux, the Pi) the
modes are read from the filesystem and the simulated runtime account is refused
by them for real. Git for Windows' runtime ignores umask and `chmod` for mode
bits -- `stat` reports `644` after `umask 0077` -- so the harness also keeps a
kernel model: every publication command records the umask it ran under and the
paths it wrote, and the simulated account is refused by the mode that umask
would produce. Where the kernel is honest the model is checked file by file
against it, so a run on Linux proves the model the Windows run relies on.

---

## 6. Coverage

Scenarios A--H of the task brief, each executed end to end:

| Scenario | Test |
| --- | --- |
| A. Successful deployment under `0077`: changed sources, new package directory, resynchronised dependency, all readable; probes as `mgo`; no bytecode; gateway shell umask unchanged | `test_a_deployment_from_a_restrictive_caller_publishes_a_readable_runtime` |
| B. Clean tree, unreadable source: refused with exit 65 naming runtime unreadability; no fetch, merge, reset, sync or restart | `test_an_unreadable_current_checkout_is_refused_before_any_repository_mutation` |
| C. Pre-restart failure under `0077`: exact previous commit restored, readable, resynchronised, imported as `mgo` after the reset and before success; no restart | `test_a_pre_restart_rollback_restores_a_readable_runtime_and_proves_it` |
| C'. The counterfactual: a reset under `0077` leaves the incident modes and is refused | `test_content_rollback_alone_would_have_left_the_incident_modes` |
| D. Restored runtime unusable: INCOMPLETE, exit 78, no `rollback succeeded`, no restart, evidence kept -- before and after the restart | `test_a_restored_runtime_that_cannot_execute_is_not_a_successful_rollback`, `test_a_post_restart_rollback_whose_runtime_cannot_execute_is_not_restarted` |
| E. Post-restart failure, equal schema: reset, sync, `mgo` probe, then exactly one rollback restart, recovery and preview restoration | `test_a_post_restart_rollback_validates_the_restored_runtime_before_restarting` |
| F. Schema advanced under `0077`: exit 79, nothing restored or restarted, deployed target readable | `test_a_schema_advancement_refuses_rollback_and_leaves_a_readable_target` |
| G. No `__pycache__` or `.pyc` under either umask; every probe carries `-B` and `PYTHONDONTWRITEBYTECODE=1` | `test_runtime_validation_writes_no_bytecode` |
| H. Publication umask scoped to the subshell; lock, temporary directory and `mktemp` file keep their modes | `test_the_publication_umask_is_scoped_to_the_publication`, `test_sensitive_objects_do_not_inherit_the_publication_umask` |

Twenty-one mutations were added to `tests/mutation_register.py`
(`TASK_14_5C_MUTATIONS`): the publication umask removed, made restrictive, or
leaked out of its subshell; each of the three publications reverted to the
inherited umask; the current-runtime preflight removed or moved after the
fetch; the restored runtime validated as the owner instead of `mgo`, not
validated, or validated after success was reported; a restart after a failed
restored-runtime proof; an incomplete rollback reported as success; each
bytecode guard removed singly and together, and the schema probe's; the
schema-advancement refusal weakened; exit 79 altered; the lock and the
temporary directory created under the publication umask. Two existing anchors
that the fix moved (`sync-resolves`, `merge-failure-not-transactional`) were
rewritten to the new text.

---

## 7. What remains before a deployment retry

1. Independent review and merge of the pull request.
2. Re-alignment of the installed gateway on `mgo-core` to the merged bytes,
   through `scripts/deploy/install-mgo-validate.sh`, under its own authority.
3. Fresh deployment readiness checks: the production checkout proven readable
   by `mgo` (Task 14.5B-R left it so), a new non-pruning recovery set, and a
   new approval.
4. A separately authorised retry of the Task 14.5B deployment, from a wrapper
   that sets no umask -- not because the gateway now needs it, but because a
   wrapper has no business choosing one.
