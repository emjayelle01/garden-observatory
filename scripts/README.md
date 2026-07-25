# MGO operator scripts

Small, **operator-run** helper scripts for administering the Raspberry Pi that
hosts Matt's Garden Observatory. They are not part of the application, are not
imported by any Python code, and do not run automatically. Run them by hand on
the Pi (or, where noted, the workstation).

See [`docs/Remote-Access.md`](../docs/Remote-Access.md) for the full procedure.

All scripts are POSIX/Bash, use `set -euo pipefail`, are idempotent, quote their
inputs, and never edit files tracked in this repository. The SSH scripts only
touch the Pi's **live** `/etc/ssh/sshd_config.d/` drop-in directory (additive
and reversible); they never edit the main `sshd_config`.

| Script | Run as | What it does |
| ------ | ------ | ------------ |
| `ssh/verify-key-auth.sh` | normal user | Non-destructive checks: `~/.ssh` and `authorized_keys` permissions, key count, and the effective `PasswordAuthentication` setting. Changes nothing. |
| `ssh/disable-password-auth.sh` | root (`sudo`) | Writes an additive drop-in disabling password auth, **after** confirming you have an authorized key and `sshd -t` validates. Backs up any existing drop-in and reloads SSH. Refuses if it would risk lockout. |
| `ssh/enable-password-auth.sh` | root (`sudo`) | Rollback: removes the drop-in and reloads SSH. |
| `deploy/update-main.sh` | normal user (uses `sudo` for the service restart) | Aligns the repo to `origin/main` by fast-forward, runs `uv sync`, restarts `mgo.service`, and prints status + a health probe. Refuses on a dirty tree. Non-destructive. |

> Safety: never disable password authentication until you have verified key
> authentication in a **separate** SSH session. Keep a session (or a console)
> open during any SSH change so you can run `ssh/enable-password-auth.sh` to roll
> back.
