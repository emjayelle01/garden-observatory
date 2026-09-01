# Task 14.3D — Post-Schema-Advancement Recovery Contract

**Status:** implemented, under review. Not deployed, not approved, not aligned to
the Raspberry Pi.

Task 14.3A made the deployment gateway refuse a code-only rollback once the
database schema has moved. Task 14.3C then installed that gateway on the Pi and
presented the manual recovery procedure for approval. **Approval was explicitly
declined**, because the procedure the gateway now hands an operator was not a
safe one. This task repairs it.

---

## 1. Problem statement

The gateway's exit-79 refusal is correct and is not changed here. What was
missing is the other half: the manual procedure that takes over when the gateway
stops.

`docs/Operations.md` §6.1 was written for a **corrupt or lost database** on a host
whose deployed code already matches the backup. It restores a database and then
starts the service. There was nothing in the document saying that this is the
wrong procedure after a schema-advancing deployment failure, and no procedure
that was the right one.

Task 14.3C also recorded that the gateway had **no way to revoke a deployment
approval**. After a failed deployment the approval that authorised it is still
installed, and clearing it meant editing a root-owned file under `/etc` by hand,
during an incident, with no symlink check and no atomicity.

A third, smaller defect: the approval checkpoint was repeatedly called "§6-B",
a section that does not exist in this repository and never has.

## 2. Failure scenario

1. `deploy-main` fast-forwards the checkout to a build carrying migration 003 and
   restarts the service.
2. The new build starts, applies migration 003, and the database moves to schema 3.
3. A later step fails — preview restoration, or final verification.
4. The gateway probes the database, finds schema 3 where the previous build
   supports schema 2, and **refuses** to roll the checkout back. Exit 79. Nothing
   is restored, nothing is restarted, the database is untouched.
5. An operator follows §6.1: preserves the damaged database, restores a schema-2
   backup, and starts the service.
6. **The still-deployed schema-3 build starts against the schema-2 database and
   applies migration 003 again.** Within seconds the database is back at schema 3
   and the recovery has been silently undone.

Step 6 is the defect. §6.1 is not wrong; it is being asked a question it was not
written to answer. Its own step 6 even says "The application applies any pending
migrations at startup" — true, harmless in its intended context, and fatal here.

## 3. Threat model

Not an attacker model. The adversary is an operator under time pressure holding a
document that looks authoritative and is silently out of scope.

| Hazard | Consequence | Countermeasure |
| --- | --- | --- |
| Following §6.1 after exit 79 | Recovery reverted by re-migration | §5.7 and §6.1 both route exit 79 to §6.2; §6.2 restores code before any start |
| Choosing "the latest" backup | The newest set may post-date the migration, at the schema being escaped | §6.2 requires one literal stem, resolved and printed before use |
| Trusting a backup because it exists | Restoring a corrupt or mismatched set over production | `verify` **and** `restore-test` against that exact file, plus a manifest schema check |
| Fixed evidence filename | A second attempt overwrites the first attempt's evidence | Unique timestamped evidence directory, collision-checked before creation |
| Deleting WAL/SHM | Destroys committed pages the main file may not hold | Sidecars are moved to evidence, never deleted |
| Mode inherited from umask | A world-readable production database | Ownership and mode asserted explicitly after restore |
| Stale approval | A later `deploy-main`/`restart-api` acts on failed authority | `clear-approval`, run before the service is stopped |
| An approval-clearing action that can also write | The gateway becomes sufficient to authorise its own deployment | The action has no content parameter and no write path |
| Symlinked approval path | Root truncates a file chosen by whoever owns the link's parent | Symlink check before regular-file check, before any write |
| Racing a live deployment | Authority changes under a running transaction | The action takes the same control-plane lock |

## 4. The rejected unsafe sequence

Explicitly recorded so it is not reintroduced:

```text
stop service -> preserve database -> restore database -> start service -> restore code
```

Starting the service before the code is restored is the defect. The order is not
a stylistic preference; it is the whole correction.

## 5. The new ordered recovery state machine

```text
A  recognise and freeze          service still UP, nothing mutated
B  select and prove the set      service still UP, verify + restore-test
C  revoke approval               service still UP, authority removed
D  stop and preserve             service DOWN, failed state moved to unique evidence
E  restore old code              exact SHA, admin account, clean tree proven
F  restore dependencies          uv sync --frozen, same as the gateway
G  restore configuration         only if the deployment changed it
H  restore database              temp-then-rename, explicit mgo:mgo 0640
I  pre-start compatibility gate  code, deps, config, integrity, schema, evidence, approval
J  controlled start              start, then validate everything
K  preserve and close            evidence kept, gap recorded, no auto-backup
```

Two properties carry most of the safety. **Everything that can fail without an
outage happens while the service is still running** (A–C), so a failed proof
costs nothing. And **nothing starts the service until Stage I has passed** (E–I),
which is what makes re-migration impossible rather than merely unlikely.

## 6. Approval-clearing design

New public gateway action:

```text
mgo-validate clear-approval
```

Implemented as two functions plus one action, deliberately separated:

- `require_safe_approval_object` — is this object *safe to write*: not a symlink,
  a regular file, root-owned, not group- or world-writable.
- `clear_approval_file` — the atomic clear: a temporary created **in the approval
  file's own directory**, owner and mode copied from the object being replaced,
  then `mv -f`. A temporary under `/run` would make the rename cross a filesystem
  and stop being atomic.
- `action_clear_approval` — takes the control-plane lock, then decides among
  absent / already-empty / clear-it.

`require_safe_approval_object` is deliberately **not** `validate_approval_file`.
The validator refuses an empty file, because an empty file authorises nothing —
correct for `deploy-main`, wrong for revocation, which must succeed precisely
when the authority is already gone. One function could not answer both questions
without breaking one of them.

**It can only remove authority.** No parameter carries replacement content and no
branch writes a byte; the only artefact it can produce is an empty file. This
asymmetry is what allows the action to exist at all: installing an approval stays
a deliberate act by a human with root.

**It is idempotent.** Absent and already-empty both exit 0 and neither performs a
write. Recovery procedures are re-run under pressure, and a revocation that
failed the second time would push an operator toward doing it by hand — the exact
thing this replaces.

Exit statuses reuse the gateway's existing vocabulary: `EX_REQUEST` (64) for an
unsafe approval object, matching how every other unusable approval state is
reported, and `EX_PRECONDITION` (65) if the clear itself fails.

## 7. Privilege analysis

**The sudoers policy is unchanged — byte-for-byte.** It grants one account the
right to execute one absolute path:

```text
claude ALL=(root) NOPASSWD: /usr/local/sbin/mgo-validate
```

The policy deliberately carries no argument pattern, and the gateway's own action
parser is the command boundary. Adding an action therefore changes the gateway's
**internal** action set and not the sudo command boundary. No second executable,
no shell grant, no wildcard, no `SETENV`, no service-manager or Git or uv grant.

Had `clear-approval` required any of those, the correct outcome would have been
to abandon the design rather than widen the boundary. It did not: the action
writes one file that root already owns, inside a process root already runs.

## 8. Evidence-preservation rules

- The failed database is **moved**, not copied, to
  `/var/lib/garden-observatory/recovery-evidence/<UTC stamp>/mgo.db.failed`.
  Moving leaves the activation path empty for Stage H and makes partial
  overwrite impossible.
- The `-wal` and `-shm` sidecars are moved beside it. They are evidence: they may
  hold committed pages the main file does not. **They are never deleted.**
- A sidecar may legitimately be absent. Absence is recorded, not assumed, and the
  failing `mv` is not silenced.
- The evidence directory name is unique per attempt and collision-checked with
  `test ! -e` before creation, so a second attempt cannot overwrite the first.
- The fixed name `mgo.db.damaged` is **not used** by §6.2. It remains in §6.1,
  whose single-shot corruption scenario it fits, and where changing it would
  rewrite history this task has no reason to touch.

## 9. Exact-set verification rules

One literal stem, resolved and printed before use. From it: three derived
filenames, a regular-file and non-symlink check on each, a manifest
`schema_version` check against the **old** build's supported schema, recorded
SHA-256 for all three, then repository-defined `verify` and `restore-test`
against that same exact `.db`, then a re-hash proving the proofs changed nothing.

"Latest" is called out as specifically wrong here: after a failed
schema-advancing deployment the newest set on disk may have been taken *after*
the migration, at the schema being escaped.

## 10. Compatibility gates

Offline, before start (Stage I): repository `HEAD` equals the recorded previous
SHA; tree clean including untracked; dependency sync succeeded; configuration
validated; restored database integrity `ok`; restored schema equals the old
build's supported schema; migration inventory matches; no stale sidecars at the
activation path; Stage D evidence intact; approval clear.

The integrity and schema checks are read-only by construction — `mode=ro` plus
`PRAGMA query_only`, the same shape as the gateway's own probe. The document
states explicitly that a migration command must not be used to "check" the
schema, because running one applies one.

After start (Stage J): unit state, `MainPID`, `NRestarts`, active-enter
timestamp, running version, `/health`, `/database/status`, migration inventory,
preview state and producer count, motion state, event-capture state, retention
endpoint availability appropriate to the restored build, protected capture
evidence, configuration hash and section inventory, and media aggregates by count
and bytes only. Then a final confirmation that approval is still clear.

## 11. Failure and stop behaviour

Every stage can stop, and stopping is always the cheaper outcome. A–C stop with
the service still running and nothing mutated. D–I stop with the service down and
all evidence intact. The document says plainly: escalate rather than retry a
destructive step, because repeating a restore over a half-restored database is
how a recoverable incident stops being one.

## 12. Testing strategy

Gateway behaviour is executed, not described: each test sources the shipped shell
and calls the real function against temporary paths, in the style the suite
already uses. Every approval path under test is a `tmp_path` file — no test names
or resolves `/etc/garden-observatory/claude-approved-sha`.

Covered: clearing a valid approval; never printing the SHA; idempotence on an
already-empty file; refusal of a symlink, a directory, a non-root owner and
group- or world-writable modes; a metadata failure leaving the file untouched;
metadata copied before the rename; the temporary created beside the approval
file; no write path that could grant; no Git, uv, systemd, database,
configuration or media seam reachable from the action; both clear outcomes
exiting 0; safety checked before any temporary is made; and the deliberate split
between "safe to clear" and "valid authority". Plus extra-argument refusal, and
the lock-ordering test extended to the new action.

Documentation is covered by contract tests asserting the structural properties of
§6.2 — its existence, the cross-references from §5.7 and §6.1, code-before-start
ordering, exact-set requirements, evidence preservation, explicit ownership and
mode, approval clearing before service stop, and the absence of the fixed
`mgo.db.damaged` name from the new section. Ordering assertions are used rather
than paragraph snapshots, so prose can be improved without breaking them.

## 13. Known limitations

- **The procedure has never been executed.** It is a reviewed document, not a
  rehearsed drill. Production restoration remains unrehearsed.
- `restore-test` proves isolated recoverability only. It never starts a service
  from the restored database and never reproduces production ownership or paths.
- Media is not in a recovery set and cannot be restored from one.
- Backups live on the same filesystem as the database and media, so device
  failure is uncovered.
- `clear-approval` is proven by unit-level execution against temporary paths, not
  against a real `/etc` on the Pi.
- Equal schema versions mean the old build will *open* the database. They do not
  mean arbitrary data transformations are reversible — unchanged from 14.3A, and
  still true.

## 14. Future Pi alignment and runbook approval

This task publishes a pull request. It does not deploy, install or approve
anything. Before deployment can be reconsidered, in order:

1. this change is reviewed and merged;
2. the merged gateway is installed on the Pi through
   `scripts/deploy/install-mgo-validate.sh`, replacing the Task 14.3A build
   currently installed, by the same two-phase root checkpoint Task 14.3C used;
3. the installed action set is re-proven to be exactly the four public actions,
   with the sudo boundary still one executable;
4. the **merged** §6.2 is presented for explicit documentary approval; and
5. a deployment window and deployment authority are granted separately.

Approval of this design record is not approval of the runbook, and neither is
authority to deploy.
