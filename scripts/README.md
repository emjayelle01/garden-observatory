# MGO operator scripts

Small, **optional, operator-run** helper scripts for administering the Raspberry
Pi that hosts Matt's Garden Observatory. They are not part of the application,
are not imported by any Python code, and do not run automatically. Run them by
hand on the Pi.

See [`docs/Remote-Access.md`](../docs/Remote-Access.md) for the full workflow.

All scripts are POSIX/Bash, use `set -euo pipefail`, are idempotent, quote their
inputs, and never edit files tracked in this repository or the Pi's SSH
configuration.

| Script | Run as | What it does |
| ------ | ------ | ------------ |
| `ssh/verify-key-auth.sh` | normal user | **Read-only** checks that key-based login is ready: `~/.ssh` and `authorized_keys` exist with the right permissions and at least one usable public key is installed. Reports the current `PasswordAuthentication` value for information only. Changes nothing. |
| `deploy/update-main.sh` | normal user (uses `sudo` for the service restart) | Aligns the repo to `origin/main` by fast-forward, runs `uv sync`, restarts `mgo.service`, and prints status + a health probe. Refuses on a dirty tree. Non-destructive. |
