# Dashboard — the local operational overview

## Purpose

`GET /dashboard` is the browser page an operator opens to answer one question:
*is this appliance well, and what is it doing right now?*

It gathers what is otherwise spread across **four** JSON endpoints —
`/health`, `/version`, `/motion/status` and `/notifications/status` — into a
single readable overview on the local network. It is a **reporting** surface
only: it controls nothing.

Its guiding rule is that it must report **real** state. Nothing is assumed,
nothing is fabricated, and an absent or failed reading is shown as absent or
failed rather than smoothed over.

## Task 9 scope

Task 9 delivers the dashboard **shell**: the page, its cards, its refresh
behaviour and its failure behaviour. The project plan's five mandatory system
fields — hostname, uptime, CPU temperature, memory and disk — are all present,
alongside the additional cards the existing API contracts already support.

Not in this task: authentication, public exposure, TLS, reverse proxying,
capture review, event timelines, charts or history, notification transports,
motion-triggered capture, and bird or species recognition. Bird recognition
remains future work and the page says so explicitly.

## Route

```text
GET /dashboard   ->   200, text/html
```

Additive. `GET /` is unchanged — it remains the minimal three-key JSON
identity endpoint that Task 8 protects, and it is not redirected to the
dashboard. `/preview` is unchanged and remains where preview is controlled.

The route is **read-only and side-effect free**. Requesting it does not
collect health, inspect hardware, open a database connection, start a
background monitor, start or stop preview, capture an image, publish a
notification or mutate any application state. It returns a constant document.

## Rendering architecture

One self-contained HTML document, built by
[`src/mgo/api/dashboard_page.py`](../src/mgo/api/dashboard_page.py) and served
from the module-level FastAPI object in
[`src/mgo/api/app.py`](../src/mgo/api/app.py) with `HTMLResponse`.

This follows the pattern the repository already established for `/preview`: a
dedicated Python module holding the page as a constant, with inline `<style>`
and inline `<script>`. The alternatives were rejected deliberately:

- **no templating engine** — the page renders no server-side value, so there
  is nothing to template. Adding Jinja2 would add a runtime dependency and a
  wheel-packaging concern for no benefit;
- **no static-file mount** — three requests where one suffices, plus another
  packaging concern;
- **no SPA framework, npm or JavaScript build step** — disproportionate for an
  appliance overview page, and outside this task's scope.

**No new dependency** was added. `pyproject.toml` and `uv.lock` are unchanged.

There is no external stylesheet, script, font, image or CDN. The page works
with no internet access at all.

## Data sources

The browser reads four existing contracts and nothing else:

| Source | Supplies |
| ------ | -------- |
| `GET /health` | overall status, application name, hostname, uptime, CPU, temperature, memory, disk, database, camera, preview |
| `GET /version` | application, release version, build commit, Python version, architecture |
| `GET /motion/status` | enabled, status, detected, score, threshold, frame availability, evaluation time |
| `GET /notifications/status` | enabled, providers, published count, failure count, last event |

`/database/status`, `/camera/status` and `/camera/preview/status` are
deliberately **not** fetched: every field the dashboard needs is already in
`/health`, and duplicating requests would add load for nothing.

The browser never inspects `/proc`, reads thermal files, invokes `vcgencmd`,
runs a subprocess, queries SQLite, detects a camera, reads configuration, or
infers a status from an HTTP 200. **The API contracts are the only source of
truth**, so the dashboard can never disagree with the API.

## Refresh

**Interval: 10 seconds.** A single named constant, `REFRESH_MS`.

The loop is **completion-scheduled**, not interval-driven:

```text
refresh
  -> issue all four requests
  -> wait for every one to settle (Promise.allSettled)
  -> render each source that succeeded
  -> degrade each source that failed (unavailable, or stale if it has
     previously succeeded)
  -> update the refresh summary
  -> schedule the next refresh
```

`setInterval` is not used. The next cycle is queued with `setTimeout` only
after the previous cycle has fully settled, and a re-entrancy guard rejects a
manual or visibility-triggered refresh while one is already in flight — so
cycles cannot overlap or stack up, and a slow or hanging endpoint cannot
produce a request storm.

The first refresh happens **immediately** on load, not after a full interval.

Polling pauses while the page is hidden (`visibilitychange`) and performs one
immediate refresh when the page becomes visible again, so a dashboard left
open on a background tab does not poll all day.

There is no WebSocket, server-sent-event stream or long poll. A **Refresh
now** button is provided; like everything else on the page it issues GET
requests only.

## Partial failure and stale data

`Promise.allSettled` means the four sources succeed or fail **independently**.
One failed endpoint never discards the other three's responses: `/health` can
render normally while `/motion/status` is unreachable.

Each source is in one of **four** states:

| State | Shown as | Meaning |
| ----- | -------- | ------- |
| `never` | `Awaiting live data` | no refresh attempt has completed for this source yet |
| `unavailable` | `Unavailable — latest refresh failed; no successful reading yet` | the latest request failed and this source has **never** supplied a successful reading |
| `loaded` | `Live` | the latest request succeeded and the reading is current |
| `stale` | `Stale — latest refresh failed; showing the last successful reading` | the latest request failed, but an earlier successful reading is still displayed |

### First-load failure versus later failure

The distinction matters, and the two must never be confused:

- a **first-load failure** — the endpoint has never answered — is
  `unavailable`. The card still holds its `Loading…` placeholders, so the page
  must not claim to be showing anything;
- a **later failure**, after at least one success, is `stale`. Real values are
  on screen, and saying so is the useful thing to tell an operator.

A failed source therefore degrades from its **previous** state, through one
helper (`markSourceFailure`) that every failure path uses, including the
catch-all for a wholly failed refresh:

```text
never       -> unavailable
unavailable -> unavailable
loaded      -> stale
stale       -> stale
```

A source that has never succeeded can never reach `stale`, so the phrase
"showing the last successful reading" can only ever appear when one exists.

A failed refresh **never** erases the last valid reading and never replaces it
with a fabricated zero or a blank. This is deliberate:

```text
CPU temperature: 46.2 °C
Stale — latest refresh failed; showing the last successful reading
```

is far more useful to an operator than an empty card.

### Malformed payloads

HTTP 200 with valid JSON is **not** proof of a usable payload. Because the
formatters safely turn every missing value into `Unavailable`, an empty object
would otherwise render as a card full of `Unavailable` badged `Live` — a
response the dashboard never actually understood, presented as current.

Each source therefore has a small validator that checks the payload against
its endpoint's minimum contract shape **before any card is mutated**:

| Source | Required |
| ------ | -------- |
| `/health` | a non-null, non-array object containing `status`, `application`, `hostname`, `uptime_seconds`, `cpu_percent`, `memory`, `disk`, `temperature`, `database`, `camera`, `preview`; the six nested sections must themselves be objects |
| `/version` | `application`, `version`, `commit`, `python_version`, `architecture` |
| `/motion/status` | `enabled`, `status`, `detected`, `score`, `threshold`, `frames_available`, `detail`, `evaluated_at` |
| `/notifications/status` | `enabled`, `providers`, `total_events_published`, `total_delivery_failures`, `last_event_at` |

Validation checks **key presence only** — never truthiness, never type — so
every value the API is documented to return as `null` (`temperature.celsius`,
`commit`, `preview.owner`, `preview.uptime_seconds`, `last_event_at`) remains
valid, as does a zero counter. An individually malformed *value* still renders
as `Unavailable` through the formatters; only a body that is not this
endpoint's response at all — an empty object, a scalar, an array, another
endpoint's payload — is rejected.

A rejected payload is a **source failure** and follows the same transition as
a network failure: `unavailable` with no prior success, `stale` with one. It
is never partially rendered over a good reading, because validation runs
before the renderer is called. The other three sources are unaffected. This is
also true of a renderer that throws.

No schema framework and no dependency were added; the validators are a
presence check over a list of field names.

The refresh summary at the top of the page reports:

- the most recent completed refresh **attempt**;
- the most recent **fully successful** refresh;
- whether the latest cycle was **complete**, **partial** (with the number of
  failed sources) or **failed**.

Before the first refresh the summary says so, rather than implying success.

## Cards

### Nothing is asserted before data arrives

The static shell contains only neutral placeholders — `Loading…`,
`Awaiting live data`, `Not yet loaded`. No card shows `Healthy`, `Available`
or `Running` until an API response has actually said so. This is asserted by a
test over the served markup.

### Overall health

Displays the exact `/health.status` value — `healthy`, `warning`, `critical`
or `unknown` — as text. Non-healthy values are not translated into `offline`
or `failed`, and an unexpected future value is displayed as-is with neutral
styling rather than breaking the page.

### Application identity

From `/version`: application, release version, build commit, Python version
and architecture. A `null` commit is normal and reads `Not supplied`, not an
error. A `version` of `unknown` is displayed truthfully.

### Hostname

`/health.hostname`, exactly as returned. It is not calculated in JavaScript,
not derived from the browser URL, and no hostname is hard-coded.

### System uptime

`/health.uptime_seconds`, rendered as a duration: `42 seconds`, `8 minutes`,
`3 hours 12 minutes`, `3 days 4 hours 12 minutes`. The conversion is
deterministic. This is **machine** uptime — not application uptime and not
preview uptime, which the preview card reports separately.

Zero is a valid reading (`0 seconds`). Negative, `null`, absent and
non-numeric values render as `Unavailable`.

### CPU utilisation

`/health.cpu_percent` to one decimal place. Zero is a valid reading. No new
CPU sampling is introduced, and no health is inferred from the number itself —
`/health` supplies no CPU status field, so none is invented.

### CPU temperature

`/health.temperature.celsius` and `.status`. Celsius is the unit the API
established.

Where Raspberry Pi thermal tooling is absent — every Windows and CI run — the
API returns `null` and the card reads `Not reported`. It never shows `0 °C`
unless the API actually returned numeric zero.

### Memory

`/health.memory`: used percentage, available versus total, and status.

The API supplies `total_bytes`, `available_bytes`, `used_percent` and
`status` — there is **no** `used_bytes` field and none is invented. Used bytes
are *derived* for display only (`total - available`), and only when both
inputs are valid numbers and the result is non-negative; otherwise the derived
line reads `Unavailable`. The API's own values remain authoritative.

### Disk

`/health.disk`: used percentage, free versus total, and status, with used
bytes derived under the same rule.

### Byte units

Sizes use **binary** units — KiB, MiB, GiB, TiB, PiB (1 KiB = 1024 bytes) —
consistently across memory and disk. Binary units were chosen because that is
how the underlying `psutil` and `shutil` readings are counted, so the
displayed figures match `free`, `df -h` and similar tools on the Pi rather
than differing by a few percent.

### Database

The compact projection embedded in `/health.database`: status, reachability,
schema version, expected schema version, migration status and integrity.

The dashboard never queries SQLite, runs a migration, runs an integrity check
or repairs anything, and never infers health from an HTTP 200. The distinction
between `healthy`, `degraded` and `unhealthy` is preserved: a degraded
database is usable but behind or misconfigured, which is not the same as an
unreachable one. The database **path** is not displayed.

### Camera (placeholder)

The WEB-01 placeholder camera card, reporting the real state from
`/health.camera`: enabled, readiness status, availability, backend, the safe
detail text and the last check time.

It shows **no image**. Opening the dashboard starts no camera process,
performs no capture and changes no camera ownership. There are no camera
controls; the card links to `/preview` for viewing. The card states plainly:

```text
Bird recognition is not yet implemented.
```

### Preview boundary

`/health.preview`: enabled, state, camera owner and — when preview is actually
running — how long for.

The dashboard **reports** preview and never controls it. It does not start or
stop preview, does not embed the MJPEG stream, does not reconnect it, does not
own the camera and does not duplicate the preview page's controls. The strings
`/camera/preview/start`, `/camera/preview/stop` and `/camera/preview/stream`
do not appear anywhere in the page, which is asserted by a test.

A stopped preview reads `stopped`, not `offline`. A disabled preview is not
labelled unhealthy.

### Motion detection

`/motion/status`: enabled, status, whether a change was detected, frame
availability, score, threshold, evaluation time and detail.

Score is shown as a measurement only when a frame was actually available;
otherwise it reads `Not measured` rather than presenting a meaningless `0`.

The card is explicit that this is **scene change**, not recognition: "It is
not object recognition, not species identification and not confirmed wildlife
activity."

### Notifications

`/notifications/status`: enabled state, configured providers, events
published, delivery failures and the last event time.

An empty provider list reads `None configured` — not a failure. Disabled
notifications are not unhealthy, and no provider configured is never presented
as a delivery failure. `No event yet` is shown for a `null` last event.

## Health-state presentation

Known status values are mapped through a **fixed internal whitelist** to a
styling class. Unknown values fall back to a neutral class and remain visible
as text — an API value is never inserted into a class name, attribute,
selector, element id or URL.

| Vocabulary | Neutral | Healthy | Warning | Critical | Active |
| ---------- | ------- | ------- | ------- | -------- | ------ |
| health | `unknown` | `healthy` | `warning` | `critical` | — |
| database | — | `healthy` | `degraded` | `unhealthy` | — |
| camera | `disabled` | `available` | `waiting_for_hardware` | `error` | — |
| preview | `stopped`, `starting`, `stopping` | `running` | — | `failed` | — |
| motion | `disabled`, `waiting_for_frames`, `establishing_baseline` | `no_motion` | — | `error` | `motion_detected` |

Status is **never** communicated by colour alone: every status pill carries a
textual glyph and the status word itself, and has a visible border.

## Disabled and unavailable states

The page keeps these distinct, because conflating them would be untruthful:

| Condition | Shown as | Not shown as |
| --------- | -------- | ------------ |
| feature disabled by configuration | `Disabled` | failed, unhealthy |
| preview stopped | `stopped` | offline |
| no notification provider | `None configured` | delivery failure |
| API field `null` or absent | `Unavailable` | `0`, blank |
| temperature not collectable | `Not reported` | `0 °C` |
| commit not supplied | `Not supplied` | an error |
| no event yet | `No event yet` | `null` |
| motion score not measurable | `Not measured` | `0` |

A valid **zero** is a reading, not a missing value. The formatters check
explicitly for `null`, `undefined` and finite numbers; no truthiness-based
fallback (`value || "Unavailable"`) exists anywhere in the page, which is
asserted by a test.

## JavaScript-disabled behaviour

With JavaScript disabled the page remains a valid, readable document with its
title, every card heading and a working link to `/preview`. A `<noscript>`
message explains that live values require JavaScript, states that **no status
is shown or implied**, and points at the JSON endpoints, which can be read
directly.

No health value is fabricated, and no card claims a state. Server-rendering
live values was deliberately rejected: it would force the dashboard route to
collect health and duplicate the API's composition, which is exactly what
makes the route inert today.

## Responsive behaviour

A single CSS grid (`repeat(auto-fit, minmax(16rem, 1fr))`) reflows from a
multi-column desktop layout to a single column on a phone. No CSS framework is
used. The page respects `prefers-color-scheme` for light and dark.

## Accessibility decisions

- semantic HTML — `header`, `main`, `section`, `h1`/`h2`, `dl`/`dt`/`dd`;
- each card is labelled by its heading via `aria-labelledby`;
- status is conveyed by **text plus a glyph**, never by colour alone;
- a visible `:focus-visible` outline on every link and button;
- readable system fonts and sufficient contrast in both colour schemes;
- no animation, no auto-scrolling and no full-page reload, so reading is never
  interrupted by a refresh.

## Privacy and security boundaries

The dashboard is local-network functionality on a trusted LAN. Consistent with
[`docs/Engineering-Principles.md`](Engineering-Principles.md), it adds **no**
authentication, accounts, public exposure, reverse proxy, TLS, VPN
configuration or CORS relaxation — those are revisited when the appliance's
exposure actually changes.

It performs **GET requests only**. There is no browser-generated write of any
kind: no capture, no preview control, no configuration change.

It displays only fields the existing API contracts deliberately publish. It
never renders environment variables, configuration contents, source paths,
database paths, capture paths, usernames, tokens, secrets, stack traces, Git
remotes or command output — and it hard-codes no hostname, IP address or port.

API-derived text reaches the DOM through `textContent` only. `innerHTML`,
`insertAdjacentHTML`, `outerHTML`, `document.write` and `eval` appear nowhere
in the page.

## Testing approach

[`tests/test_dashboard.py`](../tests/test_dashboard.py) drives the **production**
ASGI object (`mgo.api.app:app`) with real in-process HTTP dispatch, following
the pattern in `tests/test_app_routes.py` and `tests/test_version_api.py`. No
test dependency was added — `httpx`, and therefore FastAPI's `TestClient`, is
not installed.

Coverage:

- route registration on the exact served application object; status code,
  content type, document structure, `lang`, charset, viewport, title and every
  required card heading;
- references to the four data sources and the `/preview` link;
- proof the camera-control and stream endpoints appear nowhere in the page;
- **isolation** — with health collection, temperature reading, subprocess
  execution, database access, migrations, observation recording, camera,
  preview, capture and notification helpers all replaced by functions that
  fail if called, `/dashboard` still returns 200;
- **no state mutation** — application state is identical before and after a
  request;
- **hardware-free** — with every monitor state holder removed from the
  application, the route is still complete and available;
- **browser contract** — safe DOM writes only, a bounded 10-second interval, an
  immediate first refresh, a non-overlapping completion-scheduled loop,
  `Promise.allSettled`, the four-state failure transition (a never-loaded
  source degrades to `unavailable`, never to `stale`) and its single shared
  helper, the source-specific payload validators and their ordering before any
  DOM mutation, GET-only requests, no external
  URL, no hardware or database work in the browser, a neutral fallback for
  unknown statuses, and no truthiness fallback that could swallow a valid zero;
- **formatting** — the presence and shape of every formatter, the binary byte
  units, derived-used-bytes safety, and truthful temperature absence;
- **privacy** — no configuration path, database path, capture path, Windows
  drive path, environment variable name, secret, traceback, `.git` or Git
  remote appears in the page;
- **compatibility** — `/`, `/version`, `/health` and `/preview` all still
  return exactly what they returned before, and the dashboard and preview
  pages remain separate.

**The browser assertions above are deterministic checks over the served
JavaScript source, not runtime execution of it.** They pin the structure —
that the transition helper exists and is what every failure path calls, that
validation precedes rendering, that the required contract fields are declared
— but they cannot prove the code behaves correctly when run. Runtime
behaviour of the browser code is established by the manual browser validation
below, and by that alone. Treat the two as complementary: neither substitutes
for the other, and a change to the page's JavaScript needs the browser check
repeating.

Formatting behaviour that exists only in browser JavaScript is validated the
same way; it is
deliberately not duplicated in Python solely to make it unit-testable, and no
JavaScript runtime or test framework was added.

Tests run without Raspberry Pi hardware, a camera, `vcgencmd`, Git, network
access, browser automation, a live server, the production database, Node.js or
npm.

## Local validation procedure

```bash
uv sync
uv run ruff check .
uv run mypy src
uv run pytest
```

Then run the application locally:

```bash
uv run uvicorn mgo.api.app:app --host 127.0.0.1 --port 8000
```

Confirm the routes:

```bash
curl -i http://127.0.0.1:8000/dashboard
curl -i http://127.0.0.1:8000/
curl -i http://127.0.0.1:8000/version
curl -i http://127.0.0.1:8000/health
curl -i http://127.0.0.1:8000/motion/status
curl -i http://127.0.0.1:8000/notifications/status
curl -i http://127.0.0.1:8000/preview
```

`/dashboard` must return `text/html`; the others must be unchanged.

Then open `http://127.0.0.1:8000/dashboard` in a browser and confirm the page
loads, all cards appear, values refresh, the layout works at a narrow
viewport, keyboard focus is visible, no external request is made, and no
preview process starts.

To exercise failure handling without touching production code, block one
source from the browser's developer tools — a request-blocking rule, an
offline override, or a console override of `window.fetch` for that one URL.

Check both failure shapes, because they are different states:

- **after** at least one successful refresh, the affected card must keep its
  last reading and gain a **Stale** badge, while the other cards keep
  updating and the summary reports a **partial** refresh;
- **before** any successful refresh — block the source, then reload the page —
  the card must read **Unavailable — … no successful reading yet**, keep its
  `Loading…` placeholders, and must **not** claim to be showing a last
  successful reading. With all four blocked from the start, every card must
  say unavailable and the summary must read `Failed — no source answered`.

A first-load failure needs the block in place before the page's first refresh.
Serving the same page from a throwaway local harness whose endpoints can be
failed on demand is a convenient way to do that; the page bytes must be the
unmodified production output.

Also confirm a structurally invalid but successful response is rejected:
override one endpoint to return HTTP 200 with `{}` or a JSON scalar. That
source must **not** be badged `Live` — it becomes `unavailable` with no prior
success, or `stale` with one — and no part of the malformed body may be
rendered over a good reading.

## Raspberry Pi validation procedure

Run this **after** the branch has been reviewed. It is read-only apart from
the checkout change and the service restart.

**First**, confirm nothing on the Pi would be lost:

```bash
cd /opt/garden-observatory
git status -sb
```

Only continue if the working tree is clean. Then:

```bash
git fetch --prune origin
git checkout task-009-dashboard-shell
git pull --ff-only origin task-009-dashboard-shell

uv sync
uv run ruff check .
uv run mypy src
uv run pytest

sudo systemctl restart mgo.service
sudo systemctl status mgo.service --no-pager
```

Verify locally on the Pi:

```bash
curl -i http://127.0.0.1:8080/dashboard
curl -s http://127.0.0.1:8080/
curl -s http://127.0.0.1:8080/version
curl -s http://127.0.0.1:8080/health
curl -s http://127.0.0.1:8080/motion/status
curl -s http://127.0.0.1:8080/notifications/status
```

Then open the dashboard from the workstation browser using the Pi's local
hostname or LAN address, for example `http://mgo-core:8080/dashboard`, and
confirm:

- the real hostname is shown;
- system uptime is realistic;
- a live CPU percentage is shown;
- CPU temperature is shown, or truthfully unavailable;
- real memory and disk usage are shown;
- database, camera, preview, motion and notification statuses are correct;
- the page refreshes automatically and refreshes never overlap;
- partial refresh failure is visible, stale data is identified, and a source
  that has never answered reads unavailable rather than stale;
- the `/preview` link works;
- **no preview process starts merely by opening the dashboard**, and no
  capture occurs;
- the dashboard survives a service restart.

Finally review the journal:

```bash
sudo journalctl -u mgo.service --since "10 minutes ago" --no-pager
```

Return the Pi to `main` afterwards unless separately instructed to deploy the
feature branch:

```bash
git status -sb
git checkout main
uv sync
sudo systemctl restart mgo.service
```

## Rollback

The change is additive and trivially reversible. There is **no** schema
migration, persisted dashboard state, configuration change, systemd change,
new service, browser-generated write, automatic camera action or frontend
build artefact.

- **Before merge** — rollback is returning to `main`. Nothing on `main` was
  touched.
- **After a hypothetical future merge** — revert the focused Task 9 commits
  and re-run `uv run ruff check .`, `uv run mypy src` and `uv run pytest`. The
  route disappears and every other endpoint is unaffected, because none of
  them was modified.

No rollback script exists or is needed.

## Known limitations

- **No history.** Every card is a point-in-time reading; there are no charts,
  trends or sparklines, and nothing is persisted by the page.
- **No preview image.** The camera card is a status placeholder; viewing the
  camera stays on `/preview`.
- **No authentication.** Anyone who can reach the port can read the dashboard.
  That is the current, deliberate trust model for the private LAN.
- **Browser-side formatting is validated by source contract plus manual
  browser checking**, not by an automated JavaScript test runtime — a
  deliberate trade against adding Node.js and a JS toolchain to this project.
- **Timestamps render in the viewer's locale and timezone**, while the API
  reports UTC ISO strings.
- **A hidden tab does not poll**, so a dashboard brought back to the
  foreground shows values from its immediate refresh, not from the moment it
  was hidden.

## Deferred future work

Capture and event review, motion event history, charts and trends, an embedded
preview thumbnail, log inspection, service controls, alert acknowledgement,
authentication, and anything to do with bird detection or species
identification. None of it belongs in a Task 9 shell.
