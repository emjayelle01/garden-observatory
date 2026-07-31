# API — identity, version and health

This document covers the three endpoints that answer *"what is running, and is
it well?"*: `GET /`, `GET /version` and `GET /health`. The specialised status
endpoints (`/database/status`, `/camera/status`, `/motion/status`,
`/notifications/status`) are documented alongside their own subsystems.

Task 8 **added** `GET /version` and **formalised** the pre-existing `/` and
`/health` contracts with tests. It did not create `/health`, which has been in
production since well before this task and reports system, database, camera and
preview state.

## The three endpoints at a glance

| Endpoint | Answers | Cost |
| -------- | ------- | ---- |
| `GET /` | "is the service up, and what is it?" | none — three constants |
| `GET /version` | "which build is deployed?" | none — values resolved once at startup |
| `GET /health` | "is this machine and its subsystems well?" | live system metrics + cached monitor state |

All three return HTTP 200 whenever the application is serving. `/health`
reports trouble in its `status` field, never as an HTTP error.

## `GET /`

Minimal application identity. Its three keys are unchanged and will stay
unchanged — it is the cheapest possible liveness probe.

```json
{
  "name": "Matt's Garden Observatory",
  "version": "0.1.0",
  "status": "operational"
}
```

`status` is the literal `operational`: reaching this endpoint at all is the
signal. It is deliberately **not** a health summary — use `/health` for that.

## `GET /version`

Release and build identity, for support and deployment verification.

```json
{
  "application": "Matt's Garden Observatory",
  "version": "0.1.0",
  "commit": null,
  "python_version": "3.13.14",
  "architecture": "aarch64"
}
```

| Field | Meaning | When unavailable |
| ----- | ------- | ---------------- |
| `application` | Configured application name (`[application].name`). | always present |
| `version` | Installed release version, from package metadata. | `"unknown"` |
| `commit` | Deployed commit SHA, if the deployment supplied one. | `null` |
| `python_version` | Running interpreter version. | always present |
| `architecture` | Machine architecture (`aarch64` on the Pi, `AMD64` on Windows). | always present |

The endpoint is read-only, side-effect free and **deterministic for a running
process**: two requests to the same process always return the same body.

It performs **no** database I/O, **no** hardware detection, **no** subprocess,
**no** network call and **no** filesystem work per request. It does not depend
on the background monitors, so it answers correctly from the first instant the
process serves — including with no camera attached, no usable database and no
Git installed.

### Version authority

`pyproject.toml` `[project].version` is the **single** authoritative release
version. Nothing else declares one.

At runtime it is read from the installed distribution's package metadata
(`importlib.metadata`, distribution name `garden-observatory`) and cached for
the life of the process. Everything that reports a version — the OpenAPI
document, `GET /`, `GET /version`, the lifecycle notification events and the
persisted `application_start` / `application_stop` observations — reads that one
value, so they cannot disagree.

To cut a release, change `pyproject.toml` and run `uv sync`. That is the only
edit.

**Fallback.** If package metadata cannot be read — the distribution is not
installed, or its `dist-info` is unreadable — `version` becomes the literal
`"unknown"`. This is deliberate: an operator can tell "I do not know" from
`0.1.0`, whereas a fabricated number is worse than none. A metadata failure
**never** prevents import or startup, and `/version` still returns 200.

### Build commit — `MGO_BUILD_COMMIT`

`commit` exists because the release version cannot identify a deployment: it
stays `0.1.0` across many commits, so it cannot answer *"is the Pi running the
build I just pushed?"*.

It is populated **only** from the optional environment variable
`MGO_BUILD_COMMIT`, read once at startup:

```bash
MGO_BUILD_COMMIT=$(git rev-parse HEAD) uv run uvicorn mgo.api.app:app
```

or, for the service, as a systemd drop-in:

```ini
[Service]
Environment=MGO_BUILD_COMMIT=d381b6d00aa1deff2303e1890f2fcfea22ab48cd
```

Rules:

- **entirely optional.** Unset is the normal case and reports `null`. A missing
  build identifier is not an application error.
- **validated.** Only 7–40 hexadecimal characters are accepted; the value is
  trimmed and lowercased. Anything else — a path, a branch name, a remote URL,
  a token, empty text — is discarded and reported as `null`, never echoed.
- **stable.** It is read once, so the reported value cannot change mid-process.

### Git is not required

MGO never invokes Git and never reads `.git`. There is no subprocess and no
repository parsing anywhere in identity resolution.

That is a deliberate design decision, not an omission. The alternatives were
rejected because neither is trustworthy in the deployment that matters: `git`
is not guaranteed to be installed for the sandboxed service account (which runs
under `NoNewPrivileges=yes` with an empty capability bounding set), and parsing
`.git` correctly means handling loose refs, `packed-refs` and a detached HEAD —
disproportionate complexity, and fragile, for a value that is only ever
advisory.

Consequently `/version` behaves identically with Git installed or absent, and
with `.git` present or absent.

### Privacy and information disclosure

`/version` reports build identity and nothing else. It never discloses:

- absolute source, database, media or configuration paths;
- the hostname (that is `/health`'s deliberate, pre-existing decision, and
  `/version` does not widen disclosure);
- usernames, secrets, API tokens or Git remote URLs;
- command output, stack traces or raw environment data.

The only environment value it can report is `MGO_BUILD_COMMIT`, and only after
it has been validated as a bare SHA.

## `GET /health`

Live system health plus database, camera readiness and preview status. This
endpoint predates Task 8 and its contract is unchanged.

```json
{
  "status": "healthy",
  "application": "Matt's Garden Observatory",
  "hostname": "mgo-pi",
  "architecture": "aarch64",
  "python_version": "3.13.14",
  "uptime_seconds": 27139,
  "cpu_percent": 4.2,
  "memory": {
    "total_bytes": 8318144512,
    "available_bytes": 6934515712,
    "used_percent": 16.6,
    "status": "healthy"
  },
  "disk": {
    "total_bytes": 62742792192,
    "free_bytes": 51203481600,
    "used_percent": 18.4,
    "status": "healthy"
  },
  "temperature": { "celsius": 44.9, "status": "healthy" },
  "database": {
    "status": "healthy",
    "accessible": true,
    "schema_version": 2,
    "expected_schema_version": 2,
    "migration_status": "current",
    "integrity": "ok"
  },
  "camera": {
    "enabled": false,
    "backend": "rpicam",
    "status": "disabled",
    "available": false,
    "detail": "Camera functionality is disabled by configuration.",
    "checked_at": "2026-07-27T13:37:20.802892+00:00"
  },
  "preview": {
    "enabled": false,
    "state": "stopped",
    "owner": null,
    "uptime_seconds": null
  }
}
```

### Guaranteed fields

| Field | Type / unit |
| ----- | ----------- |
| `status` | `healthy` \| `warning` \| `critical` \| `unknown` — the worst component |
| `application` | configured application name |
| `hostname` | deliberate: identifies *which machine* answered |
| `architecture`, `python_version` | strings |
| `uptime_seconds` | whole seconds since boot |
| `cpu_percent` | percentage, 0–100 |
| `memory` | `total_bytes`, `available_bytes`, `used_percent`, `status` |
| `disk` | `total_bytes`, `free_bytes`, `used_percent`, `status` |
| `temperature` | `celsius` (`null` off-Pi), `status` |
| `database` | `status`, `accessible`, `schema_version`, `expected_schema_version`, `migration_status`, `integrity` |
| `camera` | `enabled`, `backend`, `status`, `available`, `detail`, `checked_at` |
| `preview` | `enabled`, `state`, `owner`, `uptime_seconds` |

Byte counts are bytes and percentages are percentages; neither has ever
changed unit.

### What a health request does *not* do

Reading `/health` never runs a migration, opens a SQLite connection, performs
hardware detection, invokes Git, or mutates any application state. The database
and camera sections come from the **cached** results of the background
monitors; only the system metrics (CPU, memory, disk, temperature) are measured
live. That architecture is deliberate and is locked by tests — it is what keeps
a health probe from adding load to a struggling database.

### Status composition

`status` is the worst of the system-resource statuses and the database
severity. A degraded database contributes `warning`; an unhealthy one
contributes `critical`. Camera readiness is reported independently and never
changes the top-level status, so a database fault is never mislabelled as a
camera failure. A stopped preview is reported for visibility only and is not an
error. See [`docs/Database.md`](Database.md) for the database mapping.

### `/health` carries no version

Version identity lives in `/version` (complete) and `/` (minimal). `/health`
answers "is this well?", not "what is deployed?", and adding a version field
would enlarge a production-validated contract for no need. This is an
intentional decision and is asserted by a test, so changing it later is a
deliberate contract change rather than an accident.

## `GET /dashboard` — browser interface, not a JSON contract

`GET /dashboard` returns **HTML**, not JSON. It is a browser interface, and it
has **no response-body contract**: nothing should parse it, and its markup may
change freely without that being an API change. It is documented here only so
the endpoint list is complete.

```text
GET /dashboard   ->   200, text/html
```

It is a **static shell**. The route returns a constant document and performs
no work: no health collection, no hardware detection, no database I/O, no
subprocess, no monitor, no camera or preview action and no state mutation.

All of its live values are fetched **by the browser** from the contracts
documented here and alongside their own subsystems — `/health`, `/version`,
`/motion/status` and `/notifications/status`. It therefore *consumes* existing
contracts and defines none of its own, and it can never report something the
API did not say. The browser issues GET requests only.

`GET /` is unchanged: it remains the minimal three-key JSON identity endpoint
and is **not** redirected to the dashboard. `/preview` is likewise unchanged.
Adding `/dashboard` changed no existing route, field, type, unit or status
value.

See [`docs/Dashboard.md`](Dashboard.md) for the page itself.

## Camera endpoints and the managed preview lifecycle

Task 12 added two opt-in preview policies (`preview.auto_start` and
`preview.restore_after_capture`) and routed every camera *mutation* through one
coordinator. **No endpoint path, response field, status value or error mapping
changed.** This section documents the behaviour a client can now observe.

### Which operations are coordinated

| Endpoint | Kind | Coordinated |
| -------- | ---- | ----------- |
| `POST /camera/preview/start` | mutation | Yes |
| `POST /camera/preview/stop` | mutation | Yes |
| `POST /camera/capture` | mutation | Yes |
| `GET /camera/preview/status` | read | No |
| `GET /camera/preview/stream` | read | No |
| `GET /health` | read | No |

Coordinated operations are serialised: a start, stop, capture or shutdown
requested while a capture transaction is open waits for it rather than racing
it. Reads are deliberately *not* serialised, so preview status and `/health`
keep answering during a capture.

### Auto-start

`preview.auto_start = true` makes exactly one preview start attempt during
application startup, through the same `PreviewService.start()` path an API
request uses — the same first-frame validation, the same idempotence, no
duplicate process. It happens **only in the application lifespan**: no request,
and no page load of `/preview` or `/dashboard`, ever starts preview.

#### Two kinds of startup failure, treated differently

An **expected operational failure** — camera absent, camera busy, tool missing,
permission denied, encoder failure, process exit during startup, no first frame,
or a stream that cannot be read — does not stop the application from serving. An
operator needs the API most when the camera is broken. `GET /health`,
`GET /camera/status` and `GET /camera/preview/status` all keep returning 200,
with preview truthfully:

```json
{
  "state": "failed",
  "owner": null,
  "started_at": null,
  "uptime_seconds": null,
  "last_error": "Preview process exited during startup with code 1: ..."
}
```

`last_error` carries the subsystem's own bounded operational message, which is
the actionable detail. Nothing retries in a loop; an operator restarts preview
explicitly with `POST /camera/preview/start`.

An **unexpected programming failure** — any exception that is not a
preview-domain error, from a backend, from startup validation or from the
startup stream reader — is **fatal to application startup**. It propagates out
of the lifespan unchanged, after the same cleanup a normal shutdown performs
(monitors stopped, camera released, no orphan process). It is deliberately not
caught: a defect that presented itself as an absent camera would be diagnosed as
a hardware problem and could persist unnoticed for as long as the camera stayed
plausible.

Whenever such a fault does settle preview state, `last_error` is a single
constant:

```text
Preview startup failed unexpectedly.
```

It never contains the exception's message, arguments, `repr` or traceback. An
exception message is arbitrary application data and may carry a filesystem path,
a username, an environment value, a secret, a memory address or control
characters, none of which belongs in an API response. The full exception and
traceback go to the application log instead. The same applies to a stream-read
failure, which publishes the fixed operational reason
`stream read failed during startup` rather than the underlying error text.

### Capture and restoration

`POST /camera/capture` releases an active preview so the capture owns the camera
exclusively — unchanged from before. With
`preview.restore_after_capture = true`, a preview that was **running** when the
capture began is restarted once the capture attempt finishes. A preview that was
stopped stays stopped: a capture never starts a preview nobody asked for.

The capture response is **unchanged** and carries no restoration field:

```text
success  filename  absolute_path  timestamp  width  height
filesize_bytes  backend  capture_id
```

Error mappings are unchanged: 503 camera unavailable, 504 capture timeout, 502
backend failure, 500 write failure, 500 other camera-domain failure, 500 archive
failure.

### When restoration fails

The capture's own outcome is always the response:

| Capture | Restoration | Response | Preview status afterwards |
| ------- | ----------- | -------- | ------------------------- |
| succeeded | succeeded | 200 with metadata | `running` |
| succeeded | **failed** | **200 with metadata** | `failed`, with `last_error` |
| failed | succeeded | the capture's own error code | `running` |
| failed | **failed** | **the capture's own error code** | `failed`, with `last_error` |

A successful capture is never reported as a failure because preview did not come
back — a client told "capture failed" would retry, and the archive would gain a
second record for an image captured once. Conversely a preview failure never
replaces a capture failure.

**Preview restoration failure is visible only through
`GET /camera/preview/status`** (and the application log). A client that reads
only the capture response will not learn that preview did not return; poll
preview status if that matters to it.

## Compatibility promise

- `/` keeps its exact three keys and their values. Only the *source* of
  `version` changed — from a hard-coded literal to package metadata — and the
  resolved value is identical.
- `/health` keeps every field name, type, unit, status value and threshold it
  had before Task 8. Nothing was removed, renamed or restructured.
- `/version` is additive: a new route with no persistent state, no schema
  change and no configuration change.

No client, script, probe or dashboard needs to change in either direction, and
reverting the branch restores the previous behaviour exactly.

## Configuration

**None.** `/version` adds no configuration. A release version is build/package
identity, not machine configuration, so nothing was added to `mgo.toml` and no
second configurable version value exists. `MGO_BUILD_COMMIT` is an optional
*deployment* input, not application configuration.

## Production validation on the Raspberry Pi

Run this **after** the branch is reviewed and merged, or when validating the
feature branch on the Pi. It is read-only apart from the checkout change and
the service restarts.

1. Confirm the Pi is on the merged Task 7 `main` baseline:
   `git -C <checkout> rev-parse HEAD` and `systemctl status mgo.service`.
2. Record the current responses for comparison:
   ```bash
   for e in / /health /database/status /camera/status /motion/status /notifications/status; do curl -fsS "http://127.0.0.1:8080$e" > "/tmp/before$(echo $e | tr / _).json"; done
   ```
3. Back up the production configuration:
   `sudo cp /etc/garden-observatory/mgo.toml /etc/garden-observatory/mgo.toml.bak`.
4. Check out the feature branch: `git fetch origin && git checkout task-008-api-version-foundation`.
5. `uv sync` — required, because the release version is read from package
   metadata.
6. `uv run ruff check .`, `uv run mypy src`, `uv run pytest` — all must pass.
7. `sudo systemctl restart mgo.service`.
8. Verify the service identity is unchanged: `systemctl show mgo.service -p User -p Group`.
9. Verify each endpoint returns 200: `GET /`, `GET /version`, `GET /health`,
   `GET /database/status`, `GET /camera/status`, `GET /motion/status`,
   `GET /notifications/status`.
10. Confirm `/version` matches what is actually deployed — `version` equals
    `pyproject.toml` `[project].version`, and `architecture` reads `aarch64`.
11. Confirm no path, secret or Git remote information appears in `/version`,
    `/health` or `/`. `commit` should be `null` unless `MGO_BUILD_COMMIT` was
    set.
12. `sudo systemctl restart mgo.service` again.
13. Confirm `/version` is byte-identical across the restart.
14. Review `journalctl -u mgo.service -n 200` for metadata-resolution or
    startup errors — there should be none.
15. Return the Pi to `main` unless instructed otherwise:
    `git checkout main && uv sync && sudo systemctl restart mgo.service`.

Optionally, to verify the build-commit path end to end, add a systemd drop-in
setting `MGO_BUILD_COMMIT` to the deployed SHA, restart, and confirm `/version`
reports it — then remove the drop-in.
