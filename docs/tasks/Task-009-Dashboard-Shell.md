# Task 9 — Dashboard shell

## Status

**Complete and validated. Approved for pull request, fast-forward merge and
deployment from `main`.**

| Gate | Outcome |
| ---- | ------- |
| Implementation | Complete |
| Repository review | Complete |
| Corrective review | Complete |
| Local static and automated validation | Passed |
| Manual browser validation | Passed |
| Raspberry Pi validation | **Passed** — performed and confirmed by Matthew |

Delivered exactly as decided below, with no deviation:

- `src/mgo/api/dashboard_page.py` — the self-contained page and
  `render_dashboard_page()`;
- `src/mgo/api/app.py` — `GET /dashboard` registered on the production
  application object (two additions: the import and the route);
- `tests/test_dashboard.py` — 123 tests covering the route, its isolation, the
  browser source contract, the refresh-state model, payload validation,
  formatting safety, privacy and the compatibility of the endpoints it
  consumes;
- `docs/Dashboard.md`, plus `README.md` and `docs/API.md` updates.

A fourth, corrective commit followed a repository review that found a real
truthfulness defect: the failure path assigned `stale` unconditionally, so a
source failing on the **first** refresh claimed to be "showing the last
successful reading" it had never fetched, and `apply()` treated any
non-throwing render as success, so a structurally invalid 200 response (an
empty object, a scalar) was badged `Live`. Both are fixed — see
"Stale-data design" below.

No dependency was added; `pyproject.toml` and `uv.lock` are unchanged. No
existing API contract, status vocabulary, configuration file, migration or
systemd unit was touched.

Validation on the development workstation: `ruff` passed, `mypy src` passed (39
source files), `pytest` 633 passed (baseline 510 + 123 added). The page was
also loaded in a real browser against a local `uvicorn` run and exercised —
live values, the refresh cadence, non-overlapping cycles, the responsive
layout, and every failure state: first-load partial failure, first-load total
failure, recovery, failure after success, and malformed payloads both before
and after a successful reading.

### Raspberry Pi validation

**Passed.** The Raspberry Pi validation was **performed and confirmed by
Matthew**, not by an automated agent. Matthew confirmed that all prescribed
Task 9 Raspberry Pi validation steps — the procedure in
[`docs/Dashboard.md`](../Dashboard.md) — passed.

The validated runtime-code SHA was:

```text
e0d0511e2bbc571d3b5a8558051b8faeadea6662
```

Only this task record changed after that SHA. Runtime code, tests,
dependencies, configuration, migrations and deployment scripts are unchanged
from the commit that was validated on the Pi, so the merged result runs the
code that passed.

No detailed Pi readings are reproduced here: this record states the confirmed
outcome only, and does not restate measurements, timings, process identifiers
or journal contents that would be second-hand.

No Task 10, Task 11 or Task 12 work has been started.

## Authoritative definition

> **Dashboard shell — Show hostname, uptime, CPU temperature, memory and disk.**

The corresponding work-breakdown item:

> **WEB-01 — Create dashboard shell.**
> Output: Overview page with placeholder camera and health cards.

The Phase 0 objective is a local operational foundation, and the first software
milestone is *a healthy local dashboard that survives reboot*. The dashboard
must report **real** application and subsystem state, never assumed state.

## Starting point

| Item | Value |
| ---- | ----- |
| Branch | `main` |
| HEAD / `origin/main` | `0c556f51e46b067f4119d0e9d3e6e948de823cea` |
| Baseline `ruff check .` | passed |
| Baseline `mypy src` | passed (38 source files) |
| Baseline `pytest` | 510 passed |

## Current repository reality

The repository is far past the point the plan assumed. Task 9 does **not**
start from an application without a browser interface.

Already present before this task:

- `src/mgo/api/app.py` — the single module-level FastAPI object production
  serves (`uvicorn mgo.api.app:app`). There is **no** router and **no**
  application factory; every route is registered directly on that object.
- `GET /preview` — an existing browser page. It is served by
  `src/mgo/api/preview_page.py`, which holds one module-level string constant
  (`_PREVIEW_PAGE`) returned by `render_preview_page()`. The document is a
  complete, self-contained HTML page with inline `<style>` and inline
  `<script>`; it fetches its data from the preview endpoints.
- **No** templating engine and **no** static-file mounting exist. `Jinja2` is
  not installed, `StaticFiles` is not mounted, and there is no `templates/` or
  `static/` directory. Runtime dependencies are exactly `fastapi`, `pillow`,
  `psutil` and `uvicorn[standard]`.
- `httpx` is **not** a test dependency, so FastAPI's `TestClient` is
  unavailable. Existing route tests (`tests/test_app_routes.py`,
  `tests/test_version_api.py`, `tests/test_database_api.py`) drive the ASGI
  interface of the production `app` object directly with a hand-built HTTP
  scope, which exercises real routing and response serialisation without
  sockets, a server or an extra dependency.
- A background-monitor architecture (health, database, camera, motion) whose
  status endpoints read cached state and perform no work per request.

**There is no dashboard.** That is the gap Task 9 closes.

### Contracts the dashboard consumes

These were read from the implementation, not assumed.

`GET /health` (`mgo.core.health.collect_health` plus the API-layer composition
in `app.health`) returns:

```text
status              healthy | warning | critical | unknown  (worst component)
application         configured application name
hostname            socket.gethostname()
architecture        platform.machine()
python_version      platform.python_version()
uptime_seconds      whole seconds since boot (system uptime, not process)
cpu_percent         float percentage 0-100
memory              total_bytes, available_bytes, used_percent, status
disk                total_bytes, free_bytes, used_percent, status
temperature         celsius (float or null), status
database            status, accessible, schema_version,
                    expected_schema_version, migration_status, integrity
camera              enabled, backend, status, available, detail, checked_at
preview             enabled, state, owner, uptime_seconds
```

Key details:

- **Memory has no `used_bytes` field.** It reports `available_bytes`, so used
  bytes can only ever be *derived* for display. Disk likewise reports
  `free_bytes`.
- **Temperature is Celsius.** `_temperature_c()` shells out to
  `vcgencmd measure_temp` and returns `None` on `FileNotFoundError`,
  `ValueError` or a non-zero exit — the normal Windows/CI case. `celsius` is
  then `null` and `temperature.status` is `unknown`. A `null` temperature is a
  truthful absence, not an error, and must never render as `0 °C`.
- `preview.owner` and `preview.uptime_seconds` are `null` while preview is
  stopped. `PreviewState` is `stopped | starting | running | stopping |
  failed`.
- `camera.status` is `disabled | waiting_for_hardware | available | error`.
- The database projection embedded in `/health` is the compact
  `DatabaseHealth.health_dict()` — deliberately **without** `database`
  (the filename), `journal_mode`, `foreign_keys`, `detail` or `checked_at`,
  which only the fuller `/database/status` carries.

`GET /version` returns exactly `application`, `version`, `commit`,
`python_version`, `architecture`. `commit` is `null` unless a valid
`MGO_BUILD_COMMIT` was supplied; `version` is `"unknown"` if package metadata
cannot be read.

`GET /motion/status` returns exactly `enabled`, `status`, `detected`, `score`,
`threshold`, `frames_available`, `detail`, `evaluated_at`. `status` is
`disabled | waiting_for_frames | establishing_baseline | no_motion |
motion_detected | error`. `enabled` is derived from the status, so the two can
never disagree.

`GET /notifications/status` returns exactly `enabled`, `providers` (a list),
`total_events_published`, `total_delivery_failures`, `last_event_at` (nullable
ISO timestamp).

## Architecture decision

**Follow the existing `/preview` pattern exactly.** A new module,
`src/mgo/api/dashboard_page.py`, holds the complete HTML document as a
module-level constant and exposes `render_dashboard_page() -> str`. The route
is registered directly on the module-level `app` in `src/mgo/api/app.py` with
`response_class=HTMLResponse`, mirroring `preview_page()`.

The alternatives were considered and rejected:

- **Jinja2 templates** — adds a runtime dependency and a `templates/`
  packaging concern (the wheel packages `src/mgo` only, so template files would
  need explicit build configuration) to serve one static document that has no
  server-side variables. The dashboard renders **nothing** server-side; every
  value arrives from the API in the browser. A templating engine would have
  nothing to template.
- **`StaticFiles` with separate CSS/JS** — introduces a mount, a packaging
  concern and three HTTP requests where one suffices, for a page served on a
  LAN to one or two viewers.
- **Any SPA framework or JS toolchain** — categorically out of scope, and
  disproportionate for an appliance overview page.

The established standalone-page pattern is sufficient, so it is used. No
restructuring of the FastAPI application is undertaken.

## Rendering decision

One self-contained HTML document: semantic markup, inline `<style>`, inline
`<script>`. No external stylesheet, script, font, image or CDN — the appliance
must work with no internet access. Layout is a small responsive CSS grid; no
framework.

## Dependency decision

**No new dependency**, Python or JavaScript. The implementation uses only
FastAPI/Starlette's existing `HTMLResponse` and standard browser APIs
(`fetch`, `Promise.allSettled`, `setTimeout`, `textContent`). `pyproject.toml`
and `uv.lock` are unchanged.

## Scope

### In scope

- `GET /dashboard` — an additive, read-only, side-effect-free HTML route;
- a self-contained dashboard page with cards for application identity, overall
  health, hostname, uptime, CPU utilisation, CPU temperature, memory, disk,
  database, camera (placeholder), preview, motion and notifications;
- a non-overlapping browser refresh loop with truthful partial-failure and
  stale-data handling;
- a `<noscript>` fallback;
- comprehensive deterministic tests, including browser-source contract tests;
- documentation.

### Explicit non-goals

- changing `/`, `/health`, `/version` or `/preview`;
- changing any existing status vocabulary, field name, type or unit;
- API version prefixes; authentication; user accounts; public exposure;
  reverse proxies; TLS; VPN; CORS changes;
- React, Vue, Angular, any SPA framework, npm or Node.js tooling;
- WebSockets, server-sent events or long polling;
- new health or subsystem monitors;
- camera capture redesign, automatic preview startup, preview control
  duplication or live stream embedding;
- motion-triggered capture, bird detection, species identification or AI
  inference;
- Telegram/email transports or notification policy;
- database backup/restore, log rotation, diagnostic scripts, systemd changes,
  deployment or production configuration changes;
- Task 10 operations work, Task 11 camera-simulator work, Task 12
  acceptance-record work;
- any access to, or deployment on, the Raspberry Pi.

## Route decision

`GET /dashboard`, returning HTTP 200 with `text/html`.

`GET /` is **not** changed, redirected or repurposed. Task 8 protects its exact
three keys (`name`, `version`, `status`) and it remains the minimal JSON
identity/liveness endpoint. `/preview` is untouched and remains the place where
preview is controlled.

The route handler builds no state and performs no work: it returns a constant
string. Requesting `/dashboard` therefore collects no health, inspects no
hardware, opens no database connection, starts no monitor, starts no preview,
publishes no notification and mutates nothing.

## Data-source decision

The browser consumes four existing contracts and nothing else:

| Source | Supplies |
| ------ | -------- |
| `GET /health` | overall status, application name, hostname, uptime, CPU, temperature, memory, disk, database, camera, preview |
| `GET /version` | application, release version, build commit, Python version, architecture |
| `GET /motion/status` | enabled, status, detected, score, threshold, frames available, evaluated-at |
| `GET /notifications/status` | enabled, providers, published count, failure count, last event |

`/database/status`, `/camera/status` and `/camera/preview/status` are
deliberately **not** fetched: every field the dashboard needs from them is
already in `/health`, and duplicating requests would add load for nothing.

The browser never inspects `/proc`, reads thermal files, invokes `vcgencmd`,
runs a subprocess, queries SQLite, detects a camera, reads configuration or
infers status from HTTP success. The API contracts are the only source of
truth.

## Refresh decision

- Default interval **10 seconds**, defined as a single named constant.
- An immediate refresh on load, then a **completion-scheduled** loop: the next
  refresh is scheduled by `setTimeout` only after the previous cycle has fully
  settled. `setInterval` is not used, so cycles cannot overlap or stack up.
- A re-entrancy guard prevents a manual/visibility-triggered refresh from
  running concurrently with an in-flight cycle.
- `Promise.allSettled` over the four requests, so one failed endpoint never
  discards the other three's successful responses.
- Polling pauses while the page is hidden (`visibilitychange`) and performs one
  immediate refresh when it becomes visible again.
- No WebSockets, SSE, long polling or full-page reload.

## Stale-data design

Each of the four sources tracks its own state. **Four** states are needed, not
three: a source that has never answered must not claim to be showing a last
successful reading it does not have.

```text
never        ->  "Loading…" placeholders, badged "Awaiting live data"
unavailable  ->  placeholders retained, badged "Unavailable — … no
                 successful reading yet"
loaded       ->  live values, badged "Live"
stale        ->  last good values retained, badged "Stale — … showing the
                 last successful reading"
```

A failed source degrades from its **previous** state through one shared
helper, which every failure path uses:

```text
never       -> unavailable
unavailable -> unavailable
loaded      -> stale
stale       -> stale
```

A successful HTTP response is not enough to be `loaded`. Each source validates
the payload against its endpoint's minimum contract shape **before** any card
is mutated; a body that is not that endpoint's response (an empty object, a
scalar, an array) is a source failure following the same transition. Presence
of keys is checked, never truthiness, so documented `null` values and zero
counters remain valid.

A failed refresh **never** erases the last valid reading and never substitutes
a fabricated zero or blank. The initial HTML asserts nothing: no card shows
`Healthy`, `Available` or `Running` until an API response has actually said so.

A refresh summary reports the most recent completed attempt, the most recent
fully successful refresh, and whether the latest cycle was complete or partial.

Individual fields that the API returns as `null`, absent or malformed render as
`Unavailable` / `Not reported` / `Not supplied` as appropriate. Zero is a valid
reading and is never treated as missing: the formatters test explicitly for
`null`/`undefined`/finite-number rather than relying on JavaScript truthiness.

## Compatibility requirements

`/dashboard` is purely additive. `/`, `/version`, `/health` and `/preview` keep
their exact current behaviour, fields, types, units and status vocabularies.
Existing contract tests are extended, not weakened or replaced. No field is
renamed or removed, and no status meaning changes.

Specifically preserved:

- `/` returns exactly `name`, `version`, `status`;
- `/version` returns exactly its five fields;
- `/health` keeps every protected top-level field;
- `/preview` still returns HTML and still contains its own controls and
  endpoint references.

## Testing requirements

- route registration on the exact production ASGI object, driven by real
  in-process dispatch (the `tests/test_app_routes.py` pattern), with no new
  test dependency;
- HTTP 200, `text/html` content type, valid document structure, `lang`,
  charset, viewport, title, and every required card heading;
- references to the four data sources and a link to `/preview`;
- proof the page never calls `/camera/preview/start` or `/camera/preview/stop`
  and never embeds `/camera/preview/stream`;
- isolation: with `collect_health`, temperature collection, subprocess
  execution, database access and preview control patched to fail if called,
  `/dashboard` still returns 200;
- repeated requests return a byte-identical shell;
- browser-source contract assertions: `textContent` only (no `innerHTML`,
  `insertAdjacentHTML` or `document.write`), a bounded refresh interval, an
  immediate first refresh, a non-overlapping loop, `Promise.allSettled`,
  GET-only requests, no external URL, a neutral fallback for unknown statuses,
  and no truthiness test that would swallow a valid zero;
- privacy: no configuration path, database path, capture path, Windows drive
  path, environment value, secret, stack trace, `.git` or Git remote appears in
  the page;
- hardware-free behaviour: the route is unaffected by absent camera tooling,
  absent temperature tooling, absent Git or unavailable monitor state, because
  it serves a static shell.

Tests run without Raspberry Pi hardware, a camera, `vcgencmd`, Git, network
access, a browser automation service, a live server, the production database,
Node.js or npm.

## Security boundaries

The dashboard is local-network functionality on a trusted LAN. It adds no
authentication, no accounts, no public exposure, no reverse proxy, no TLS, no
VPN configuration and no CORS relaxation — consistent with
`docs/Engineering-Principles.md` ("build for today's requirement").

It performs **GET requests only**. There is no browser-generated write of any
kind: no capture, no preview start/stop, no configuration change.

It displays only fields the existing API contracts deliberately publish. It
never renders environment variables, configuration contents, source paths,
database paths, capture paths, usernames, tokens, secrets, tracebacks or
command output. API-derived text reaches the DOM through `textContent` only,
and never through an HTML attribute, class name, element id or URL; unknown
status values are displayed as text with neutral styling rather than being
interpolated into a class name.

## Rollback considerations

Additive and trivially reversible. There is no schema migration, no persisted
dashboard state, no configuration change, no systemd change, no new service, no
build artefact and no change to an existing API contract.

- **Before merge** — rollback is simply returning to `main`; nothing on `main`
  is touched.
- **After a hypothetical future merge** — revert the focused Task 9 commits and
  re-run `uv run ruff check .`, `uv run mypy src` and `uv run pytest`. The
  route disappears; every other endpoint is unaffected because none of them was
  modified.

No rollback script is provided or needed.

## Raspberry Pi validation plan

**Not executed in this task.** No Raspberry Pi was accessed, modified or
deployed to. The validation procedure Matthew runs later is in
[`docs/Dashboard.md`](../Dashboard.md); it begins by checking `git status -sb`
on the Pi before any branch change, so no uncommitted Pi-side work can be
discarded, and it returns the Pi to `main` afterwards.
