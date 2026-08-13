# Task 13.2 — Physical Motion-Triggered Capture Validation

**Status: physical validation performed and passed on 2026-08-12. Production
configuration was restored byte-for-byte afterwards and the feature is disabled
again. Event capture is NOT permanently enabled.**

This is a validation and evidence record. No application source changed.

## Scope

Validated one thing: **the merged Task 13.1 motion-triggered still-capture
pipeline works with the real Raspberry Pi camera**, end to end —

```text
real motion transition -> bounded EventCaptureService -> CaptureWorkflow
    -> CameraCoordinator -> physical full-resolution still -> CaptureArchive
    -> correlated immutable observation -> managed preview restoration
```

— while preserving one camera owner, one preview producer, existing preview
restoration, bounded automatic work, truthful status and safe failure semantics.

## Repository and deployment identity

| Item | Value |
| ---- | ----- |
| Merged Task 13.1 main SHA | `48bdaf39f4f903931352796c5bc159621e1f730e` |
| PR #10 | merged, strict fast-forward (`merge_commit_sha` == head) |
| Production SHA before this task | `938134d4f4963256cd74b5bbf59123abe49e1d5d` |
| Production SHA after deployment | `48bdaf39f4f903931352796c5bc159621e1f730e` |
| Deployment path | `sudo -n /usr/local/sbin/mgo-validate deploy-main` |
| Deployment exit code | **0** |
| Deployment window (UTC) | 2026-08-12T07:47:55Z → 07:48:01Z |
| Host | `mgo-core`, Debian 13 (trixie), kernel 6.18.34+rpt-rpi-2712, aarch64 |
| Boot ID | `8fe870b8-f091-413b-9371-764bab756a57` (unchanged throughout; **no reboot**) |

The gateway fast-forwarded `938134d..48bdaf3`, ran a frozen dependency sync,
restarted the service, recovered in 2 s, and observed preview already running so
issued no start request.

## Service lifecycle

| Event | MainPID | UTC |
| ----- | ------- | --- |
| Pre-task baseline | 1514 | active since 2026-08-06 12:32:18Z |
| After `deploy-main` | 73594 | 07:47:59Z |
| After enabling restart | 74540 | 07:52:18Z |
| After restart-recovery test | 76337 | 07:55:47Z |
| After configuration restore | 77010 | 07:57:59Z |

Every restart went through `mgo-validate restart-api` (exit 0 each time, 2 s
recovery). `systemctl restart` was never used.

## Configuration handling

| Item | Value |
| ---- | ----- |
| Backup directory (root-only, 0700) | `/root/mgo-task13-2-20260812T075009Z` |
| Backup file | `mgo.toml.before` |
| Pre-task config SHA256 | `012f60e3fc928cf999bc1332fa04fce6e67d88e304ce5d58521417add82984f0` |
| Backup SHA256 | `012f60e3fc928cf999bc1332fa04fce6e67d88e304ce5d58521417add82984f0` (match) |
| Temporary enabled config SHA256 | `df1c1de6fcbb883639079ca807d5cd4854c131325f46a6e7ecf12c55facf3685` |
| Restored config SHA256 | `012f60e3fc928cf999bc1332fa04fce6e67d88e304ce5d58521417add82984f0` |
| **Byte-identical restoration** | **Confirmed** |
| Owner/group/mode/size before | `root:mgo 640 3485` |
| Owner/group/mode/size after | `root:mgo 640 3485` |

All root-operated steps (approval installation, backup, edit, restoration,
approval withdrawal) were performed by Matthew. Configuration contents were
never copied into the evidence directory.

### Dependency check before enablement

Already `true` in production, unchanged by this task: `camera.enabled`,
`preview.enabled`, `preview.auto_start`, `preview.restore_after_capture`.

### The only two changes made

1. `[motion] enabled = false` → `true` (line 69).
2. `[event_capture] enabled = true` appended (the section did not exist).

Size moved 3485 → 3516 bytes, which reconciles exactly: −1 byte for
`false`→`true`, +32 for the appended section. Motion thresholds
(`analysis_interval_seconds = 1.0`, `160×90`, `pixel_difference_threshold = 20`,
`changed_pixel_ratio_threshold = 0.08`, `cooldown_seconds = 5.0`), preview
policies and dimensions, camera backend, capture directory, database path,
notifications and health thresholds were all verified unchanged after the edit.

## Merged code runs safely with the feature off

Between deployment and enablement, with the pre-task configuration still in
place:

- `GET /event-capture/status` → **HTTP 200**, `enabled=false`, `state=disabled`,
  every counter `0`, every timestamp `null`;
- health `healthy`; database `healthy`, schema 2 = expected, `current`, WAL,
  foreign keys on, integrity ok;
- camera `available` (imx708); preview `running`, auto-started by the
  application 1 s after restart;
- exactly **1** `rpicam-vid`, **0** `libcamera-vid`;
- motion still `disabled`; capture catalogue unchanged at 11.

## Validation window

| Item | Value |
| ---- | ----- |
| Enabled from | 2026-08-12T07:52:18Z |
| Disabled (restore restart) | 2026-08-12T07:57:59Z |
| Elapsed | **5 min 41 s** of a 15-minute allowance |
| Successful automatic captures | **17** against a 10-capture cap — **budget exceeded, see Deviations** |
| Failed captures | **0** |
| Dropped triggers | **0** |
| `pending_triggers` observed | never above 1 |

## Capture evidence

Catalogue moved **11 → 28**; media files **8 → 25**; `event_capture`
observations **0 → 17**. All 17 observations have status `captured` and a
correlation ID.

Every capture: `4608×2592`, backend `rpicam-still`, ~2.42–2.55 MB.

The 17 recorded JPEG sizes sum to **42 660 151 bytes**. Over the 341-second
enabled window that is approximately **3.0 captures per minute** and
approximately **7.5 MB per minute** of JPEG payload (decimal). Those are
measurements of this short window on this scene under the light and activity of
that morning — they are not long-term averages and must not be quoted as one.

### First automatic capture

| Field | Value |
| ----- | ----- |
| `capture_id` | `626ccf72-74c7-426e-b4b8-bf6fd620fe36` |
| Timestamp | 2026-08-12T07:52:28.174805+00:00 |
| Filename | `2026-08-12T07-52-28.174805Z.jpg` |
| Dimensions / size | 4608×2592 / 2 424 351 B |
| Backend | `rpicam-still` |
| SHA256 | `78526e8b9edc71c146f8bfba0fd6763bd63a752bee2ed8d2f51e41ebf0f44cdf` |
| JPEG magic | `ffd8` |
| `origin` | `motion` |
| `motion_status` | `motion_detected` |
| `motion_score` | `0.22069444444444444` |
| `motion_threshold` | `0.08` |
| `motion_evaluated_at` | 2026-08-12T07:52:28.130867+00:00 |

Observation: `kind=event_capture`, `source=mgo-event-capture`,
`status=captured`, `summary=Motion-triggered still captured`,
`correlation_id=626ccf72-74c7-426e-b4b8-bf6fd620fe36` — exactly the capture
UUID. Payload carried `capture_id`, `filename`, `motion_score`,
`motion_threshold`, `motion_evaluated_at` and nothing else. Verified **absent**:
`absolute_path`, `capture_directory`, `database_path`, configuration path, raw
exception, traceback, command line; and no `/var/lib` or `/etc/` substring
anywhere in the observation.

### Second automatic capture

| Field | Value |
| ----- | ----- |
| `capture_id` | `ce6a550d-5f70-4b39-8687-c7b42e315eb7` |
| Timestamp | 2026-08-12T07:53:23.890348+00:00 |
| Size | 2 532 555 B |
| SHA256 | `e72d67bf9e592e3f9def4fff058d7e02f2fc2ff40626e5ea89537b03981214d5` |
| `motion_score` / threshold | `0.18541666666666667` / `0.08` |
| Correlated observation | yes, `correlation_id` == capture UUID |

### Post-restart automatic capture

| Field | Value |
| ----- | ----- |
| `capture_id` | `83e9339f-230c-48d7-ab40-6b8a2445131c` |
| Timestamp | 2026-08-12T07:56:23.900585+00:00 |
| Size | 2 527 677 B |
| SHA256 | `8e67de402f268c5eb82199d2defa6c1230a1a1f942025e2d839b3abf21fb3155` |
| `motion_score` / threshold | `0.1371527777777778` / `0.08` |
| Correlated observation | yes, `correlation_id` == capture UUID |

### All 17 validation capture IDs

```text
fa66dedf-93bd-414f-9528-648d80b6a4c2  07:57:39.949961Z  2515737
965eb199-3dfc-45ee-8cd6-a7918f94af85  07:56:45.223307Z  2525702
24bb3117-e3a3-43bc-886b-f4d63c9f8700  07:56:29.989295Z  2441924
83e9339f-230c-48d7-ab40-6b8a2445131c  07:56:23.900585Z  2527677
0e5d263f-db23-4885-80a5-364921affd85  07:56:18.837873Z  2496033
0c55dc17-03eb-45bb-93f8-e57f7a65817e  07:56:13.775651Z  2547133
3a2e8a17-4dbc-4783-a4a0-ebc30ac1e412  07:55:40.698956Z  2518837
62957c5f-cb1a-451c-a2a1-24100886c0dd  07:55:35.642387Z  2539693
80b05dc9-cd4b-4203-8a41-58ddcb719085  07:55:26.519540Z  2506207
edc12f78-b8d3-4fe0-b9da-6da2d907396c  07:55:09.303702Z  2489042
0f5a9e70-5bad-439c-b1b8-4976152d9998  07:55:00.151191Z  2497899
4b7f65d3-60e4-4295-b698-26eee906cdbf  07:54:34.818003Z  2506774
29b0e450-29ec-411d-90a0-86465c9d86df  07:54:27.731611Z  2539175
fb444c15-945d-4e67-b952-8ef445aa72a4  07:54:06.446639Z  2545537
8d336293-dbb5-459b-a99b-e4742502d38c  07:53:39.103486Z  2505875
ce6a550d-5f70-4b39-8687-c7b42e315eb7  07:53:23.890348Z  2532555
626ccf72-74c7-426e-b4b8-bf6fd620fe36  07:52:28.174805Z  2424351
```

**No capture was deleted.** All 17 JPEGs and all 17 catalogue records remain.

## Single camera owner — the decisive evidence

A bounded sampler recorded `rpicam-vid` count, `libcamera-vid` count and
event-capture state every 0.5 s for 30 s, and happened to span a complete
capture transaction:

| UTC | `rpicam-vid` | `libcamera-vid` | event-capture |
| --- | --- | --- | --- |
| 07:53:47 → 07:54:05 | 1 | 0 | `idle` (3 succeeded) |
| **07:54:06 → 07:54:08** | **0** | 0 | **`capturing`** |
| 07:54:09.2 | 1 | 0 | `capturing` |
| 07:54:09.8 → 07:54:20 | 1 | 0 | `idle` (4 succeeded) |

Aggregates: `max_rpicam = 1`, `max_libcamera = 0`, `min_rpicam = 0`.

That is the expected physical sequence — preview producer released so the still
owns the camera exclusively, then exactly one producer restored. **Never two
producers. Never a `libcamera-vid`.** This is polling evidence: it proves what
it sampled and does not claim to have observed every instant.

## Preview restoration and motion recovery

Preview reported `running` after every automatic capture, with a new
`started_at` following the capture timestamp (the producer PID legitimately
changes because capture releases preview and restoration starts a new one).
Motion transitioned to `waiting_for_frames` while a capture owned the camera and
returned to live analysis (`no_motion` / `motion_detected`, `frames_available =
true`) afterwards — it never stuck in `waiting_for_frames`.

## Service restart recovery

`restart-api` at 07:55:47Z, exit 0, recovered in 2 s, MainPID 74540 → 76337.
After it: service active, health `healthy`, database `healthy`, camera
`available`, preview `running` (auto-started), exactly 1 `rpicam-vid`, 0
`libcamera-vid`, event capture `enabled=true` `state=idle` with counters
correctly reset to zero (they are process-lifetime), motion live. A further
automatic capture then succeeded and correlated — the pipeline survives an
ordinary service restart.

## Failure and queue counters

Across the whole window: `total_captures_failed = 0`, `last_error = null`,
`total_triggers_dropped = 0`, and `pending_triggers` never sampled above 1.

No drop was observed, and none was induced. The bounded-queue guarantee remains
established by the automated tests and the mutation register, not by this
exercise.

## Disk

| Point | Free bytes | Used % |
| ----- | ---------- | ------ |
| Baseline | 49 402 179 584 | 16 (health 14.7) |
| While enabled (final) | 49 360 941 056 | 16 (health 14.8) |
| After restoration | 49 358 385 152 | 16 (health 14.8) |

Filesystem free space fell by **43 794 432 bytes** (~43.8 MB decimal) between
baseline and post-restoration. That is **1 134 281 bytes** more than the
**42 660 151-byte** JPEG payload. The free-space measurement includes all
filesystem writes and allocation effects occurring during the interval, so the
difference is **not attributed to any specific component** — no component-level
size measurement was taken during this exercise — and the two figures are not
interchangeable. Free space never approached the 2 GiB floor and utilisation
never approached 90 %.

## Final production state

| Item | Value |
| ---- | ----- |
| Production HEAD | `48bdaf39f4f903931352796c5bc159621e1f730e` |
| Branch | `main` |
| Working tree | clean, including untracked |
| Stash | empty |
| Service | `active`, MainPID 77010 |
| Health / database | `healthy` / `healthy`, schema 2, current, WAL, integrity ok |
| Camera | `available` |
| Preview | `running`, 1 `rpicam-vid`, 0 `libcamera-vid` |
| Motion | `disabled` (pre-task state) |
| Event capture | `enabled=false`, `state=disabled` |
| Configuration | byte-identical to pre-task (`012f60e3…984f0`) |
| Deployment approval | **withdrawn** — 0 bytes, gateway `show-approval` exits 64 |
| Captures | retained; nothing deleted |
| Reboot | none; boot ID unchanged |

**The merged software stays deployed. Only the temporary operating state was
reverted.** That distinction is the point: production runs Task 13.1 code with
the feature off, exactly as `main` intends.

## Evidence location

`/home/claude/mgo-task13-2-evidence/20260812T061858Z` on the Pi, mode 0700.
Contains command output, HTTP JSON snapshots, process counts, checksums, capture
and observation metadata, disk statistics and timestamps. It contains no
configuration contents, no secrets, no keys and no copied JPEGs.

## Deviations

**The capture budget was exceeded: 17 successful automatic captures against a
cap of 10.** Elapsed time was well inside limits (5 min 41 s of 15 minutes), and
no other budget threshold — disk free, utilisation, space delta, preview, health,
database, producer count — was approached.

Cause: ambient garden motion produced approximately **3.0 captures per minute**
across the 341-second enabled window. At that short-window rate the procedural
10-capture limit would be reached in roughly 3.3 minutes, were the rate to
persist — and nothing here shows that it would; this is one short-window
measurement, not a long-term average or a projection. The budget itself defined
no assumed capture rate: it defined procedural stopping thresholds — 10
successful automatic captures, or 15 minutes, whichever came first. The cap is
procedural, checked by the operator between verification steps, and each
verification round trip took 20–40 s, during which one or two further captures
completed. By the time the count was read after the post-restart capture it had
already passed 10. The enabled window was stopped once the overrun was observed.

Consequence, computed from the recorded JPEG sizes:

| Item | Bytes |
| ---- | ----- |
| All 17 validation captures | 42 660 151 |
| First 10 chronological captures (within the cap) | 25 087 108 |
| **Seven captures beyond the cap** | **17 573 043** |

The excess attributable specifically to exceeding the cap is therefore
**17 573 043 bytes — approximately 17.6 MB decimal, approximately 16.8 MiB** of
additional retained evidence, and nothing else. Every capture succeeded, none
failed or was dropped, disk utilisation moved 14.7 % → 14.8 %, and all captures
are retained.

Lesson for any future enabled window on this scene: **the capture cap, not the
clock, is the binding constraint.** A future exercise needs either an
application-side limit or an enabled window measured in tens of seconds, not
minutes.

### Reviewer disposition

**CAPTURE-BUDGET OVERRUN ACCEPTED AS A NON-BLOCKING VALIDATION DEVIATION.**

The reviewer's recorded reasoning: 17 successful captures occurred against the
procedural cap of 10; the excess was caused by autonomous ambient garden motion
while verification operations were in progress; the enabled validation window was
stopped once the overrun was observed; elapsed time remained well below 15
minutes; storage remained safe; health remained healthy; the database remained
healthy; preview restoration remained successful; producer ownership remained
correct; no capture failed; no trigger was dropped; production configuration was
restored byte-identically; approval was withdrawn; and repeating the physical
test would produce additional captures without adding useful validation evidence.

**The absence of separately induced operator motion is likewise accepted.**
Natural garden motion provided repeated real physical triggers, and deliberately
introducing additional motion once the autonomous capture rate was known would
have consumed more of the validation budget without strengthening the pipeline
evidence.

No capture in this record is attributed to a bird or to any other particular
subject.

Two smaller notes:

- **Controlled operator motion was not separately required.** Ambient garden
  activity produced continuous material transitions from the moment the feature
  came up, including the first capture 10 s after the enabling restart. Adding
  deliberate movement would have consumed more of an already-overrun capture
  budget without strengthening the evidence. No capture in this record is
  attributed to any specific cause — see the honesty note below.
- **`mgo-core` name resolution failed** from the workstation; the verified
  `192.168.68.79` fallback was used. SSH also works only from PowerShell, not
  Git Bash. Neither affects the validation.

## What this record does and does not claim

**Proven:** real motion transitions reached the event-capture worker; real
physical full-resolution stills were captured through `CameraCoordinator`; they
were archived with `origin = motion` and their actual motion facts; each produced
exactly one immutable observation whose `correlation_id` equals the capture UUID;
observations leak no path or raw exception; preview was released and restored
every time; exactly one camera producer existed outside the capture window and
zero during it; no `libcamera-vid` ever existed; motion resumed analysing after
each capture; the pipeline recovered across a service restart and captured again;
production configuration was restored byte-for-byte.

**Not proven, and not claimed:** bird detection or species identification (not
implemented); that any captured subject was a bird — `motion_detected` means the
scene changed, and wind, leaves, shadows and daylight all qualify; wind
filtering; visit or session detection; burst capture; retention (none exists);
queue-overflow or dropped-trigger behaviour under load (none observed, none
induced); deployment rollback (not exercised); long-term unattended operation;
camera hardware hardening (still deferred from Task 12); and permanent
event-capture enablement, which is **not authorised**.

## Recommended next steps

1. Review this record.
2. Treat **retention/deletion policy** as the blocker for any persistent
   enablement — across this validation window the scene produced approximately
   7.5 MB per minute of JPEG payload with nothing to reclaim it. Long-term
   unattended behaviour remains unproven; that figure is a short-window
   measurement, not a projection.
3. Permanent enablement remains a separate, explicitly authorised operating
   decision.

## Correction note — evidence accounting

The first evidence commit (`1c72d71`, *Record Task 13.2 physical validation*)
carried two rough figures in its narrative and commit message: an approximate
"~26 MB extra" consequence for the capture-budget overrun, and a rough
"~3.4 captures/minute" ambient rate. **Both were wrong.**

The ~26 MB figure did not correspond to the seven excess captures at all; the
~25 MB it approximated is the total for the *first ten* captures — the ones
inside the cap. Recomputed from the recorded JPEG sizes, the seven excess
captures total **17 573 043 bytes** (~17.6 MB decimal, ~16.8 MiB). The rate,
recomputed from the recorded enabled-window timestamps
(2026-08-12T07:52:18Z → 07:57:59Z, **341 s**), is **approximately 3.0 captures
per minute**, and the JPEG payload rate is **approximately 7.5 MB per minute**.

Repository history is immutable and `1c72d71` was **not** amended. This document,
as corrected, is the authoritative accounting; where it and the earlier commit
message disagree, this document is correct.
