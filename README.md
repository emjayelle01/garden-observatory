# Matt's Garden Observatory (MGO)

A small, Raspberry Pi–hosted observatory service. It persists system-health
snapshots and other events as immutable **observations**, and exposes a
FastAPI API for inspecting current state.

The project is built and validated on a Windows development machine and
deployed to a Raspberry Pi. All functionality must degrade safely when
Raspberry Pi–specific hardware or tooling is absent.

## Requirements

- Python 3.13
- [uv](https://docs.astral.sh/uv/) for dependency management

## Getting started

```bash
uv sync
```

Run the development API (auto-reload):

```bash
uv run uvicorn mgo.api.app:app --reload
```

Or bind explicitly for local checks:

```bash
uv run uvicorn mgo.api.app:app --host 127.0.0.1 --port 8000
```

## Validation

```bash
uv run ruff check .
uv run mypy src
uv run pytest
```

## API

| Endpoint              | Description                                             |
| --------------------- | ------------------------------------------------------- |
| `GET /`               | Minimal application identity.                           |
| `GET /version`        | Release and build identity.                             |
| `GET /health`         | System health plus database, camera readiness and preview. |
| `GET /database/status`| The latest monitored database-health check result.      |
| `GET /camera/status`  | The latest monitored camera readiness result.          |
| `POST /camera/capture`| Capture one still image; returns its stored metadata.   |
| `GET /camera/preview/status` | Current live-preview lifecycle status.           |
| `POST /camera/preview/start` | Start the live preview process.                  |
| `POST /camera/preview/stop`  | Stop the live preview process.                   |
| `GET /camera/preview/stream` | Browser MJPEG live-preview stream.               |
| `GET /preview`        | Simple browser live-preview page.                       |
| `GET /dashboard`      | Local operational dashboard (browser page).             |
| `GET /motion/status`  | The latest monitored motion-detection result.           |
| `GET /notifications/status` | Notification framework status and counters.       |
| `GET /captures`       | Capture catalogue (metadata only), newest first.        |
| `GET /captures/{id}`  | Stored metadata for a single capture.                   |
| `GET /observations`   | Recent observation timeline (`?limit=`, `?kind=`).      |

`POST /camera/capture` writes a timestamped JPEG beneath
`camera.capture_directory` and returns `200` with capture metadata (filename,
absolute path, UTC timestamp, dimensions, filesize). Expected failures map to
meaningful statuses: `503` when the camera is disabled/unavailable, `504` on a
capture timeout, `502` on a backend failure, and `500` on a write failure.

### Identity, version and health

Three endpoints answer *"what is running, and is it well?"*:

| Endpoint | Answers | Cost per request |
| -------- | ------- | ---------------- |
| `GET /` | is the service up, and what is it? | none — three constants |
| `GET /version` | which build is deployed? | none — values resolved once at startup |
| `GET /health` | is this machine and its subsystems well? | live system metrics plus cached monitor state |

```json
{
  "application": "Matt's Garden Observatory",
  "version": "0.1.0",
  "commit": null,
  "python_version": "3.13.14",
  "architecture": "aarch64"
}
```

`pyproject.toml` `[project].version` is the **single** authoritative release
version, read at runtime from the installed distribution's package metadata and
resolved once per process. Everything that reports a version — the OpenAPI
document, `GET /`, `GET /version`, the lifecycle notification events and the
persisted start/stop observations — reads that one value, so they cannot
disagree. Cutting a release is one edit plus `uv sync`. If package metadata is
ever unreadable, `version` becomes `"unknown"` — truthful, and never a startup
failure.

`commit` is populated only from the **optional** `MGO_BUILD_COMMIT` environment
variable, validated as a 7–40 character hex SHA and read once. Unset — the
normal case — reports `null`. **Git is never invoked and `.git` is never read**,
so `/version` behaves identically whether or not Git is installed. Nothing was
added to `mgo.toml`: a release version is build identity, not machine
configuration.

`/version` performs no database I/O, no hardware detection, no subprocess and
no network call, and does not depend on the background monitors — so it is
truthful with no camera, no usable database and no Git. `/health` keeps every
field it had, still does no per-request database or hardware work, and
deliberately carries **no** version field.

Full response contracts, fallback behaviour, the privacy exclusions, the
compatibility promise and the production validation procedure live in
[`docs/API.md`](docs/API.md).

## Dashboard

`GET /dashboard` is the **local operational dashboard** — the browser page an
operator opens to see whether the appliance is well. With the application
running, open:

```text
http://127.0.0.1:8000/dashboard
```

(or `http://<pi-hostname>:8080/dashboard` on the Pi). It shows application
identity and version, overall health, hostname, uptime, CPU utilisation and
temperature, memory, disk, database, camera readiness, preview state, motion
status and notification status.

It is deliberately inert and read-only. The route returns a **static HTML
shell**: requesting it collects no health, touches no hardware, opens no
database connection and starts nothing — in particular, **opening the
dashboard never starts a preview or camera process**. Every live value is
fetched by the browser from the four existing contracts (`/health`,
`/version`, `/motion/status`, `/notifications/status`), so the page cannot
disagree with the API, and the browser only ever issues GET requests.

Values refresh every 10 seconds on a non-overlapping loop, and the four
sources succeed or fail independently — one failure never discards the others.
A source that fails **after** succeeding at least once is marked **stale** and
keeps its last good reading rather than being blanked or zeroed; a source that
has **never** answered is marked **unavailable** and says plainly that no
successful reading exists yet, rather than claiming to show one. A response
that is not the expected endpoint payload — an empty object, a scalar — is
rejected before it can touch a card, so it is never badged live. Nothing
claims a healthy state before an API response has supplied one. There is no
external stylesheet, script, font or CDN, so it works with no internet access.

This is **local** functionality on a trusted LAN: no authentication, accounts
or public exposure are added. `GET /` is unchanged and remains the minimal
JSON identity endpoint; `/preview` remains where preview is controlled.
**Bird recognition is future work** and the page says so.

Purpose, architecture, data sources, refresh and stale-data design, card
behaviour, accessibility and privacy boundaries, the testing approach, and the
local and Raspberry Pi validation procedures live in
[`docs/Dashboard.md`](docs/Dashboard.md).

## Database

MGO keeps everything it records — the observation timeline and the capture
catalogue — in a single SQLite database at `[storage].database_path`
(`data/mgo.db` in development, `/var/lib/garden-observatory/db/mgo.db` in
production).

The schema is versioned by ordered numbered SQL files under `migrations/`,
tracked in a `schema_migrations` table and applied automatically at startup:

- each migration runs in **its own transaction**, so a failure rolls back
  completely and the schema is never left part-way through;
- re-running is a no-op once the database is current;
- an existing **unversioned** database is verified against the supported table
  shape and adopted without touching its data — it is never assumed to be empty;
- a database recording a version **newer** than the running build is refused,
  and startup fails rather than opening it.

Connections enforce foreign keys, use WAL journalling (verified, not assumed)
and a finite `busy_timeout`. A migration failure prevents the application from
starting, so it never serves against a schema it cannot trust.

```toml
[database]
health_check_interval_seconds = 60   # read-only health check cadence
busy_timeout_seconds = 5.0           # finite wait for a competing writer
```

The `[database]` section is optional — configuration files without it load
unchanged with the same values the application already used.

### `GET /health` and `GET /database/status`

A background monitor runs a **read-only** check (`PRAGMA quick_check(1)`) and
both endpoints serve that cached result: neither performs database I/O per
request, and neither can run a migration, create a table or repair anything.
`/health` gains a `database` section — every pre-existing field keeps its name
and meaning — and the database now contributes to the top-level status:

| Database state | Meaning | `/health` status |
| -------------- | ------- | ---------------- |
| `healthy`  | Sound, at the expected schema version, foreign keys on, WAL in use. | `healthy` |
| `degraded` | Usable, but the schema is behind, foreign keys are off, or WAL is not in use. | `warning` |
| `unhealthy`| Unreachable, corrupt, unversioned, or newer than this build supports. | `critical` |

Camera readiness is reported independently, so a database fault is never
mislabelled as a camera failure.

Backup and restore are **not** implemented in this task. The file layout, the
migration and adoption rules, the health-check policy, operator troubleshooting
and the production validation procedure live in
[`docs/Database.md`](docs/Database.md).

## Remote access & deployment

Operator guidance for administering the Raspberry Pi over SSH — generating and
installing an SSH key, verifying key authentication, a convenient workstation
alias, the Git-over-SSH workflow, and the deployment steps — lives in
[`docs/Remote-Access.md`](docs/Remote-Access.md). Keys are for convenience on
the trusted private LAN; password authentication intentionally stays enabled as
a fallback (SSH hardening is out of current scope). Optional, non-destructive
operator helper scripts are in [`scripts/`](scripts/README.md). These are
operator procedures only; they do not change application behaviour.

## Service identity

In production MGO runs as a dedicated, non-login system account — `mgo` — with
its persistent data outside any operator's home directory. Administrative SSH
access and the runtime identity are completely separate: the service account
cannot log in, holds no Linux capabilities, and belongs to no administrative
group.

| Location | Owner | Mode | Contents |
| -------- | ----- | ---- | -------- |
| `/etc/garden-observatory/` | `root:mgo` | `0750` | `mgo.toml` (`0640`) — readable, never writable, by the service |
| `/var/lib/garden-observatory/` | `mgo:mgo` | `0750` | `db/`, `media/captures/`, `queues/`, `state/` |
| `/var/log/garden-observatory/` | `mgo:mgo` | `0750` | file-based logs (the journal remains primary) |
| `/var/backups/garden-observatory/` | `mgo:mgo` | `0750` | database backups and their manifests (`0640`) |

Its only supplementary group is `video`, which grants camera device access.

## Operations

Backups, log rotation and diagnostics are documented in
[`docs/Operations.md`](docs/Operations.md).

A **daily backup** of the SQLite database runs at 02:30 local time via
`mgo-backup.timer`. It uses SQLite's online backup API, so it is taken **while
the API keeps serving** — no step in the normal backup procedure stops
`mgo.service`. Each backup is validated, checksummed and published atomically
alongside a JSON manifest, and the newest 14 complete sets are retained.

```bash
scripts/operations/backup-database.sh backup
```

```bash
scripts/operations/backup-database.sh list
```

```bash
scripts/operations/backup-database.sh restore-test /var/backups/garden-observatory/<backup>.db
```

There is deliberately **no `restore` command**: `restore-test` proves a backup
can be recovered, while restoring over the live database stays an explicit
operator disaster-recovery procedure documented in `docs/Operations.md`.

A **diagnostic support bundle** collects health, status, a bounded journal slice
and a redacted configuration summary into one archive, so a fault can be
diagnosed without attaching a monitor and keyboard:

```bash
scripts/operations/create-support-bundle.sh --output-directory /tmp
```

Every member is generated in memory, so the bundle structurally cannot contain
the database, its WAL sidecars, media, media filenames, the raw configuration,
SSH material or Git credentials. Inspect it before sending it anywhere.

MGO's runtime logs live in the **journal** (`journalctl -u mgo.service`), which
is bounded by host-level journald retention. The Task 10 logrotate policy covers
only MGO-owned `*.log` files under `/var/log/garden-observatory` and does not —
and cannot — rotate the journal.

> Backups are configured and covered by automated tests, but the Raspberry Pi
> validation has not yet been performed. See `docs/Operations.md` §13.
Nothing is world-readable or world-writable.

Provision it once on the Pi, then verify:

```bash
sudo bash scripts/deploy/install-service-identity.sh
```

```bash
sudo bash scripts/deploy/verify-service-identity.sh
```

The account, groups, directory layout, ownership and permission model, the
systemd unit, the migration steps for an existing deployment, and
troubleshooting live in [`docs/Service-Identity.md`](docs/Service-Identity.md).
This is a deployment foundation only — the API, camera pipeline, motion
detection and notifications are unchanged.

## Configuration

Configuration is loaded and validated from `config/mgo.toml`. Invalid values
(for example a non-positive interval or an unsupported backend) are rejected at
startup with a clear error.

### Camera section

```toml
[camera]
enabled = false            # camera functionality is off unless enabled
backend = "rpicam"         # "rpicam", "libcamera", or "null"/"none"
# device_index = 0         # optional preferred device; unset = no preference
detection_interval_seconds = 60
capture_directory = "data/captures"
```

Defaults are deliberately safe: the camera is **disabled**, no hardware is
assumed to exist, and no image capture is ever attempted.

### Production configuration

By default the application loads the tracked development configuration at
`config/mgo.toml` (camera disabled). Production deployments keep their
machine-specific settings in an **external** file that is never committed to
Git — the canonical location is `/etc/garden-observatory/mgo.toml`, owned
`root:mgo` with mode `0640` (see [Service identity](#service-identity)).

Selection is controlled by the `MGO_CONFIG_PATH` environment variable:

| Precedence | Source                                         |
| ---------- | ---------------------------------------------- |
| 1          | An explicit path passed to `load_config(...)`. |
| 2          | The `MGO_CONFIG_PATH` environment variable.    |
| 3          | The repository default, `config/mgo.toml`.     |

`MGO_CONFIG_PATH` accepts absolute paths, `~` (expanded to the user's home),
and relative paths (resolved against the current working directory). Surrounding
whitespace is stripped. A set-but-empty or whitespace-only value is rejected
with a `ValueError`, and a selected file that does not exist fails immediately
with a `FileNotFoundError` naming the resolved path — there is **no** silent
fallback to `config/mgo.toml`. Invalid TOML or an invalid configuration (bad
thresholds, unsupported backend, non-positive interval, and so on) fails at
startup exactly as it does for the default file.

A complete example lives at `config/mgo.production.example.toml`. The service
identity installer seeds `/etc/garden-observatory/mgo.toml` from it on a first
install (and never overwrites an existing one); the systemd unit then points the
service at it:

```ini
Environment=MGO_CONFIG_PATH=/etc/garden-observatory/mgo.toml
```

There is no implicit discovery of that path — `MGO_CONFIG_PATH` is what selects
it, so the precedence table above is unchanged and an existing deployment
pointing at an older location keeps working.

External production files (including `/etc/garden-observatory/mgo.toml`) must
**not** be committed to the repository.

## Camera readiness

This project implements camera **readiness detection** (described here), a
**still-image capture** layer (see `POST /camera/capture` above and the
`mgo.camera` package), a **capture archive**, a **live preview** with **browser
MJPEG streaming**, and a first **motion-detection foundation** (see below). It
detects meaningful *scene change*; it does **not** yet run inference or recognise
birds or any other object. The goal is a truthful, hardware-safe foundation that
later analysis features build on.

The distinct camera capabilities are:

- **still capture** — one JPEG per request via `POST /camera/capture`;
- **capture archive** — catalogued capture metadata via `GET /captures`;
- **live preview** — a shared preview process streamed to the browser as MJPEG
  (`GET /preview`, `GET /camera/preview/stream`);
- **motion detection** — scene-change detection over the preview frames
  (`GET /motion/status`);
- **bird recognition** — *future work*; not implemented.

A background monitor evaluates readiness at startup and then every
`detection_interval_seconds`, keeping the latest result in application state.
The API endpoints read that state and never run detection commands themselves.

### Statuses

| Status                 | Meaning                                                      | `available` |
| ---------------------- | ------------------------------------------------------------ | ----------- |
| `disabled`             | Camera functionality is disabled by configuration.           | `false`     |
| `waiting_for_hardware` | Enabled, but no supported/usable camera was detected.        | `false`     |
| `available`            | Enabled and a supported camera device was detected.          | `true`      |
| `error`                | Detection hit an unexpected runtime/software/parsing error.  | `false`     |

`available` never implies that image capture has succeeded — only that a
supported device was enumerated.

### Detection and how it behaves off-Pi

The `rpicam`/`libcamera` adapters run a bounded, non-shell
`... --list-cameras` command (argument array, timeout, safe output capture) and
require positive evidence — an enumerated device — before reporting
`available`. The presence of a command alone is never treated as a camera.

`device_index` narrows what counts as ready. When it is **unset**, any
enumerated camera makes readiness `available`. When it is **set**, that exact
index must be enumerated to report `available`; if it is absent while other
cameras are present, readiness is `waiting_for_hardware` and the detail reports
which indexes were actually enumerated.

On **Windows and CI** (no Raspberry Pi camera tooling):

- with the camera **disabled** (the default), readiness is `disabled`;
- with the camera **enabled**, a missing command is reported as
  `waiting_for_hardware` — an expected environment condition, not an
  application error.

Either way the application starts and serves normally. A `camera_status`
observation is persisted only when the readiness **materially changes**
(a change of `status` or `available`); repeated identical checks do not create
duplicate observations.

## Motion detection

MGO includes a lightweight **motion-detection foundation**. It answers one
question — *has the camera scene changed meaningfully since the previous analysed
frame?* — and nothing more. "Motion" means recent **visual activity**, not bird
presence.

What it **does**:

- consumes JPEG frames from the **existing live preview stream** (it never starts
  a second camera process);
- reduces each frame to a small greyscale image and compares it with the
  **previous analysed frame** (a rolling reference, not a fixed quiet scene);
- reports motion when a large enough proportion of pixels changed since that
  previous frame;
- keeps the latest result in application state, exposed at `GET /motion/status`;
- persists a `motion_status` observation only on a **material transition**.

The production camera watches four bird feeders on a tree in an open garden, so
wind, moving leaves, shadows and birds may all trigger it. A lasting change (a
bird that lands and stays, a feeder that settles in a new position) reads as
motion only while it is *changing*, then settles back to `no_motion`; `no_motion`
does **not** mean nothing (or no bird) is there. Bird recognition remains future
work.

What it **does not** do (all future or out of scope):

- it does **not** recognise birds, or identify or classify any object;
- it does **not** track objects or draw bounding boxes;
- it does **not** trigger a still capture automatically;
- it uses **no** heavy AI/ML framework (no TensorFlow, PyTorch, YOLO or OpenCV).

Motion detection is **disabled by default**. Detailed architecture, algorithm,
thresholds, baseline behaviour, API state meanings, troubleshooting and the
production-validation procedure live in
[`docs/Motion-Detection.md`](docs/Motion-Detection.md).

### `GET /motion/status`

Read-only and truthful: it reflects the latest result recorded by the background
monitor and never runs a frame comparison itself. It returns `200` whenever the
application is healthy — including the `disabled` and `waiting_for_frames`
states. The `status` field is one of:

| Status                  | Meaning                                                    |
| ----------------------- | ---------------------------------------------------------- |
| `disabled`              | Motion detection is off by configuration.                  |
| `waiting_for_frames`    | Enabled, but no preview frame is available yet.            |
| `establishing_baseline` | The first frame has become the rolling reference.          |
| `no_motion`             | The frame-to-frame change stayed within threshold.         |
| `motion_detected`       | The frame-to-frame change exceeded the threshold.          |
| `error`                 | A frame could not be decoded or the detector failed.       |

### Motion section

```toml
[motion]
enabled = false                    # motion detection is off unless enabled
analysis_interval_seconds = 1.0    # how often a frame is analysed
analysis_width = 160               # small analysis resolution (bounds CPU/memory)
analysis_height = 90
pixel_difference_threshold = 20    # per-pixel luminance change treated as noise
changed_pixel_ratio_threshold = 0.08  # frame-to-frame changed-pixel ratio => motion
cooldown_seconds = 5.0             # suppresses recording a repeated motion event
```

The `changed_pixel_ratio_threshold` default of **0.08** is based on real IMX708
production measurements (above quiet-scene frame-to-frame variation, below clear
controlled motion); per-site tuning may still be needed.

Defaults are hardware-safe: motion detection is **disabled**, no frames are
consumed, and no analysis runs. The `[motion]` section is optional — configuration
files without it load unchanged with motion disabled. Invalid values (a
non-positive interval, out-of-range dimensions or thresholds, a negative
cooldown) are rejected at startup with a clear error. To actually receive frames
when enabled, `[preview]` must also be enabled and running; otherwise motion
detection sits truthfully in `waiting_for_frames`.

## Notifications

MGO includes an event-driven **notification framework foundation**. Producers
(application startup/shutdown, the camera monitor, the motion monitor) publish
typed events to a central manager, which fans them out to pluggable providers —
business logic never calls a delivery transport directly. Only the
transport-free `log` (application log) and `null` (discard) providers exist so
far; Telegram/email are future work, as is any notification *policy*.

```toml
[notifications]
enabled = false     # notifications are off unless explicitly enabled
provider = "log"    # "log" or "null"
```

Defaults are safe: notifications are **disabled** and every published event is
dropped. The `[notifications]` section is optional — configuration files
without it load unchanged. `GET /notifications/status` reports the enabled
state, configured providers, publish/failure counters and the last event
timestamp; notifications are **not** persisted (observations remain the
persistent timeline). Architecture, the event model, the provider contract and
the future-transport path live in
[`docs/Notifications.md`](docs/Notifications.md).
