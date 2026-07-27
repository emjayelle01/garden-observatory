# Task 8 — API foundation, version endpoint and health contract review

## Status

Defined. Implementation follows in a separate commit on
`task-008-api-version-foundation`.

## Authoritative definition

> **API — Create `/health` and `/version` endpoints.**

## Starting point

| Item | Value |
| ---- | ----- |
| Branch | `main` |
| HEAD / `origin/main` | `d381b6d00aa1deff2303e1890f2fcfea22ab48cd` |
| Baseline `ruff check .` | passed |
| Baseline `mypy src` | passed (37 source files) |
| Baseline `pytest` | 452 passed |

## Current repository reality

The repository has moved well past the point the plan assumed. Task 8 does
**not** start from an application without an API.

Already present before this task:

- `src/mgo/api/app.py` — the single module-level FastAPI object production
  serves (`uvicorn mgo.api.app:app`); there is no router or application
  factory, and every route is registered directly on it;
- `GET /` — an application identity endpoint returning `name`, `version` and
  `status`;
- `GET /health` — system health **plus** database, camera readiness and
  preview, composed at the API layer from cached monitor state;
- specialised status endpoints: `/database/status`, `/camera/status`,
  `/motion/status`, `/notifications/status`;
- capture, preview, streaming, captures-catalogue and observations endpoints;
- a background monitor architecture (health, database, camera, motion) whose
  results the status endpoints read without performing work per request.

**`GET /version` does not exist.** That is the only endpoint gap.

Task 8 therefore **reviews and formalises** the existing `/health` contract and
**adds** `/version`. It does not recreate `/health`, and it does not replace the
API foundation.

### The real defect this task closes

The application release version is a **hard-coded string literal repeated in
five places** in `src/mgo/api/app.py`:

| Line | Use |
| ---- | --- |
| `FastAPI(version="0.1.0")` | OpenAPI document `info.version` |
| `root()` → `"version": "0.1.0"` | the `GET /` response |
| `_system_event(...)` payload | start/stop notification events |
| lifespan `application_start` observation payload | persisted timeline |
| lifespan `application_stop` observation payload | persisted timeline |

Meanwhile `pyproject.toml` declares `version = "0.1.0"` for the
`garden-observatory` distribution. There are therefore **two** version
authorities that agree only by coincidence, and six edits are required to
release a new version. There is no `__version__`, no `importlib.metadata` use
and no version accessor anywhere in `src/`.

`src/mgo/__init__.py` is empty.

## Scope

### In scope

- a single central application identity/version accessor;
- `GET /version`, returning a small, stable, side-effect-free contract;
- replacing all five hard-coded `"0.1.0"` literals with that accessor;
- an explicit, optional, validated deployment build-commit input;
- formalising the `/health` contract with contract tests;
- documentation of `/`, `/health` and `/version`;
- comprehensive deterministic tests, including fallback behaviour.

### Explicit non-goals

- recreating, replacing or restructuring `/health`;
- changing any existing field name, meaning, unit, status value or threshold;
- adding a version to `/health` (see the decision below);
- an API version prefix such as `/v1`;
- semantic-release automation, release pipelines or package publishing;
- Docker/build-image metadata, update checking or diagnostic bundles;
- OpenAPI custom branding;
- authentication, public exposure or remote-access changes;
- the Task 9 dashboard shell, or any Task 10–12 work;
- deployment to, or any access of, the Raspberry Pi.

## Compatibility requirements

Every existing API contract is preserved unless a proven defect requires
correction. Specifically, `/health` keeps every one of these top-level fields
with its current name, type, unit and meaning:

```text
status  application  hostname  architecture  python_version  uptime_seconds
cpu_percent  memory  disk  temperature  database  camera  preview
```

`/health` must continue to perform **no** database I/O, **no** hardware
detection, **no** migration and **no** state mutation per request; it reads
cached monitor state only. The cached-monitor architecture stays intact.

`GET /` keeps its exact three keys — `name`, `version`, `status` — and their
values. Only the *source* of `version` changes, from a literal to the central
accessor. The resolved value is identical (`0.1.0`).

## Version-source decision

**The package metadata of the `garden-observatory` distribution is the single
authoritative release version.** `pyproject.toml` `[project].version` is already
the package-version authority; it becomes the *only* authority.

Verified in this environment: `importlib.metadata.version("garden-observatory")`
returns `0.1.0` under the editable `uv` install. The import name (`mgo`) differs
from the distribution name (`garden-observatory`), and
`importlib.metadata.packages_distributions()` does **not** map `mgo` back to the
distribution under an editable `.pth` install — so the distribution name must be
named explicitly.

Resolution is cached, so it happens once per process rather than per request. A
`PackageNotFoundError` (or any metadata failure) must never crash import or
startup; the truthful fallback is the literal `unknown`. No second hard-coded
version constant is introduced, and **no** version value is added to
`mgo.toml` — a release version is build identity, not machine configuration.

## Commit/build identity decision

`commit` **is** included in `/version`, sourced **only** from an explicit,
optional, validated environment variable:

```text
MGO_BUILD_COMMIT
```

The concrete deployment need is real: the package version (`0.1.0`) does not
change between commits, so it alone cannot verify *which* build is deployed —
which is exactly what the Pi validation plan has to confirm.

The review nevertheless rejects every Git-derived source:

- **no subprocess**, per request or at startup — `git` is not guaranteed to be
  installed for the runtime account, and the service runs under
  `NoNewPrivileges=yes` with an empty capability bounding set;
- **no `.git` parsing.** The production checkout *is* a clone (see
  `scripts/deploy/update-main.sh`), but `.git/HEAD` may name a loose ref, a
  packed ref in `.git/packed-refs`, or a detached SHA. Handling all three
  correctly, under `ProtectSystem=strict`, for a value that is only ever
  advisory, is disproportionate complexity for a fragile result. It is rejected.

When `MGO_BUILD_COMMIT` is unset, blank or malformed, `commit` is `null` — a
truthful "not supplied", never a fabricated SHA and never an application error.
The value is validated as 7–40 lowercase hexadecimal characters before being
reported, so no arbitrary environment text can reach a client.

Nothing is added to the systemd unit template. The unit is rendered once at
install time while the deployed commit changes on every pull, so baking a SHA
into it would be actively misleading. The variable stays an optional,
documented hook.

## `/health` version decision

`/health` gains **nothing** in this task.

Version identity lives in `/version` (complete) and `/` (minimal, pre-existing).
`/health` answers "is this machine and its subsystems well?", not "what is
deployed?". Adding a version field would enlarge the health contract and touch
`mgo.core.health` for no demonstrated need. Task 8 formalises `/health` with
contract tests instead.

## Identity relationship across the three endpoints

There is one internal source and three deliberately different projections:

| Endpoint | Purpose | Identity fields |
| -------- | ------- | --------------- |
| `GET /` | minimal liveness/identity | `name`, `version`, `status` |
| `GET /version` | deployment/build verification | `application`, `version`, `commit`, `python_version`, `architecture` |
| `GET /health` | operational health | `application` (plus health data) |

`/` is not made a duplicate of `/version`: its existing three-key contract is
preserved for compatibility. All three take the application name from
`config.application.name` and — where they report it — the version from the
central accessor, so the three can never disagree.

## Testing requirements

- version resolution with metadata present, absent and failing;
- build-commit resolution: absent, blank, malformed, valid, mixed case;
- determinism — repeated resolution returns an identical result and does not
  re-discover package metadata;
- `/version` via real in-process HTTP dispatch through the production ASGI app
  (the `tests/test_app_routes.py` pattern), asserting status, exact schema and
  values;
- `/version` proven independent of the database, the camera, the health
  monitors and Git;
- `/version` proven free of paths, secrets and environment leakage;
- `/` contract compatibility, and agreement with `/version`;
- `/health` contract: every required field present, correct types and units,
  database/camera/preview sections unchanged, no per-request side effects;
- platform behaviour: Windows, CI, absent Pi tooling, absent `.git`.

Tests must not depend on the current checkout's real Git SHA.

## Rollback considerations

The change is additive and reversible. `/version` is a new route with no
persistent state, no schema change and no configuration change; reverting the
branch removes it and restores the literals. Because `/` keeps its exact keys
and values, and `/health` is untouched, no client, script or dashboard needs to
change in either direction. The only observable difference if package metadata
is ever unavailable is `unknown` in place of `0.1.0` — degraded but truthful,
and never a startup failure.

## Raspberry Pi validation plan

Not executed in this task. See "Raspberry Pi validation" in
[`docs/API.md`](../API.md).
