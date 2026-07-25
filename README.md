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
| `GET /`               | Application identity.                                   |
| `GET /health`         | System health plus the current camera readiness state. |
| `GET /camera/status`  | The latest monitored camera readiness result.          |
| `POST /camera/capture`| Capture one still image; returns its stored metadata.   |
| `GET /observations`   | Recent observation timeline (`?limit=`, `?kind=`).      |

`POST /camera/capture` writes a timestamped JPEG beneath
`camera.capture_directory` and returns `200` with capture metadata (filename,
absolute path, UTC timestamp, dimensions, filesize). Expected failures map to
meaningful statuses: `503` when the camera is disabled/unavailable, `504` on a
capture timeout, `502` on a backend failure, and `500` on a write failure.

## Remote access & deployment

Operator guidance for administering the Raspberry Pi over SSH — generating and
installing keys, verifying key authentication, safely disabling password
authentication (with rollback), the Git-over-SSH workflow, and the deployment
steps — lives in [`docs/Remote-Access.md`](docs/Remote-Access.md). Optional,
non-destructive operator helper scripts are in [`scripts/`](scripts/README.md).
These are operator procedures only; they do not change application behaviour.

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
Git — the intended location is `/etc/mgo/mgo.toml`.

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

A complete example lives at `config/mgo.production.example.toml`. Copy it
outside the repository, edit it for the target machine, and point the service at
it — for example, in the systemd unit:

```ini
Environment=MGO_CONFIG_PATH=/etc/mgo/mgo.toml
```

External production files (including `/etc/mgo/mgo.toml`) must **not** be
committed to the repository.

## Camera readiness

This project implements camera **readiness detection** (described here) and a
first **still-image capture** layer (see `POST /camera/capture` above and the
`mgo.camera` package). It does **not** yet stream video, run inference, or
perform motion detection. The goal is a truthful, hardware-safe foundation that
later capture and analysis features build on.

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
