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
| `GET /camera/preview/status` | Current live-preview lifecycle status.           |
| `POST /camera/preview/start` | Start the live preview process.                  |
| `POST /camera/preview/stop`  | Stop the live preview process.                   |
| `GET /camera/preview/stream` | Browser MJPEG live-preview stream.               |
| `GET /preview`        | Simple browser live-preview page.                       |
| `GET /motion/status`  | The latest monitored motion-detection result.           |
| `GET /captures`       | Capture catalogue (metadata only), newest first.        |
| `GET /captures/{id}`  | Stored metadata for a single capture.                   |
| `GET /observations`   | Recent observation timeline (`?limit=`, `?kind=`).      |

`POST /camera/capture` writes a timestamped JPEG beneath
`camera.capture_directory` and returns `200` with capture metadata (filename,
absolute path, UTC timestamp, dimensions, filesize). Expected failures map to
meaningful statuses: `503` when the camera is disabled/unavailable, `504` on a
capture timeout, `502` on a backend failure, and `500` on a write failure.

## Remote access & deployment

Operator guidance for administering the Raspberry Pi over SSH — generating and
installing an SSH key, verifying key authentication, a convenient workstation
alias, the Git-over-SSH workflow, and the deployment steps — lives in
[`docs/Remote-Access.md`](docs/Remote-Access.md). Keys are for convenience on
the trusted private LAN; password authentication intentionally stays enabled as
a fallback (SSH hardening is out of current scope). Optional, non-destructive
operator helper scripts are in [`scripts/`](scripts/README.md). These are
operator procedures only; they do not change application behaviour.

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
