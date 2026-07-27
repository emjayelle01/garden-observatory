# MGO operator scripts

Small, **optional, operator-run** helper scripts for administering the Raspberry
Pi that hosts Matt's Garden Observatory. They are not part of the application,
are not imported by any Python code, and do not run automatically. Run them by
hand on the Pi.

See [`docs/Remote-Access.md`](../docs/Remote-Access.md) for the SSH and
deployment workflow, and [`docs/Service-Identity.md`](../docs/Service-Identity.md)
for the production runtime account and filesystem layout.

All scripts are POSIX/Bash, use strict mode, are idempotent, quote their inputs,
and never edit files tracked in this repository or the Pi's SSH configuration.

| Script | Run as | What it does |
| ------ | ------ | ------------ |
| `ssh/verify-key-auth.sh` | normal user | **Read-only** checks that key-based login is ready: `~/.ssh` and `authorized_keys` exist with the right permissions and at least one usable public key is installed. Reports the current `PasswordAuthentication` value for information only. Changes nothing. |
| `deploy/update-main.sh` | normal user (uses `sudo` for the service restart) | Aligns the repo to `origin/main` by fast-forward, runs `uv sync`, re-asserts the runtime group's read access, restarts `mgo.service`, and probes the four status endpoints. Refuses on a dirty tree. Non-destructive. |
| `deploy/install-service-identity.sh` | **root** (`sudo`) | Provisions the production runtime identity: the non-login `mgo` system account and group, `/etc/garden-observatory`, `/var/lib/garden-observatory` (with `db`, `media/captures`, `queues`, `state`), `/var/log/garden-observatory`, their ownership and permissions, and the rendered systemd unit. Idempotent; never overwrites an existing production configuration; backs up any existing unit. Supports `--dry-run`. |
| `deploy/verify-service-identity.sh` | normal user (more checks with `sudo`) | **Read-only** verification of the service identity: account type, non-login shell, locked password, group membership, directory ownership/modes, configuration readable-but-not-writable, unit identity directives, and the owner of the running process. Exits non-zero on any problem. Changes nothing. |

`deploy/mgo.service.template` is the tracked systemd unit that
`install-service-identity.sh` renders to `/etc/systemd/system/mgo.service`. It is
a template, not an installed unit — do not copy it into place by hand.
