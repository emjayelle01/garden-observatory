# Physical camera acceptance

This is the authoritative procedure for accepting the physical camera
installation of Matt's Garden Observatory — initially, and again after any
change to the camera, its mounting, its window position or the room around it.

It exists because software cannot tell you whether the camera can *see* what it
needs to see. MGO can prove that a camera is detected, that a still is captured
at full resolution, that preview runs and that the pipeline survives a restart.
It cannot prove that the feeders are in frame, that a bird is sharp at the feeder
plane, that the exposure is usable, that reflections are tolerable, or that the
framing respects the neighbours. Those are decided by a person, at the machine,
looking at the picture.

## Two acceptance levels

This procedure serves two different questions, and conflating them was costing
real time. **Does the camera work well enough to build on?** is not the same
question as **is this installation ready to be left alone indefinitely?**

| Level | Question | Closes | Blocks |
| ----- | -------- | ------ | ------ |
| **1 — Functional prototype acceptance** | Does the camera work, and is the pipeline sound? | **Task 12** | Application development, until it passes |
| **2 — Production hardware hardening** | Is the installation permanent, optically characterised and proven over time? | A later hardening phase | Nothing in application development |

**Task 12 closes when Level 1 passes.** Level 2 checks may be completed later,
in any order, without blocking application development — they are a parallel
workstream, not a gate in front of the next phase.

### Level 1 — functional prototype acceptance

Every item is objective except the last two, which are Matthew's:

- the camera is available through the application;
- the physical `rpicam` backend is in use;
- preview starts automatically from configuration;
- exactly one preview producer exists;
- multiple clients share that one producer;
- a repeated start creates no duplicate;
- full-resolution physical capture succeeds;
- the capture decodes completely;
- archive metadata matches the file;
- preview is restored after a capture that began with preview running;
- a capture begun with preview stopped leaves preview stopped;
- application restart recovery succeeds;
- Raspberry Pi reboot recovery succeeds;
- the database remains healthy and the backup timer remains active;
- no unresolved functional camera fault is present;
- **Matthew accepts the privacy framing**;
- **Matthew explicitly accepts any temporary mounting limitations** for
  prototype use.

Passing all of them may be recorded as:

```text
FUNCTIONAL PROTOTYPE CAMERA ACCEPTED
```

That phrase means the pipeline works. It says nothing about the mount, the
optics or the passage of time.

### Level 2 — production hardware hardening

- independent PCB support;
- a mount that does not use the lens housing as its mounting point;
- permanent rather than temporary mounting;
- the full mechanical-stability matrix (§10);
- feeder coverage assessed for every intended feeder (§5);
- measured subject scale at each feeder plane (§6);
- the complete autofocus matrix (§7);
- representative exposure and colour testing (§8);
- reflection testing and any mitigation (§9);
- measured preview frame rate where useful (§11);
- the camera-disconnect check (§16);
- the 24-hour commissioning observation (§17);
- the 48-hour stability observation (§18).

Only a complete Level 2 result may be recorded as:

```text
PRODUCTION CAMERA INSTALLATION HARDENED
```

`CAMERA PIPELINE STABLE` stays reserved for a successful 48-hour run and is not
a synonym for either level. **Neither Level 2 phrase may be recorded while any
of its checks is outstanding**, and Level 1 never implies Level 2.

### What stays mandatory at both levels

Deferring a check is not permission to be unsafe. These are not deferrable:

- **no CSI hot-plugging** — ever, at either level;
- **power down and disconnect power before any cable work** (§2);
- **no deliberate impact testing** (§10);
- **no exposed electrical contact hazard** (§2, §3);
- **no cable or mount arrangement that leaves the camera at immediate risk of
  falling, or that pulls on the CSI connector** (§3);
- **privacy requires Matthew's explicit decision** (§4) and can never be
  auto-approved.

A temporary mount is acceptable at Level 1 only when Matthew has seen its
limitations written down and accepted them for prototype use. Recording the
limitation is what makes the acceptance meaningful; a mounting decision with its
failures edited out is worth nothing.

## The four categories, and why they never substitute for one another

| Category | Who decides | Evidence |
| -------- | ----------- | -------- |
| 1. Automated software checks | The test suite | Test output, exit codes |
| 2. Operator-observed physical checks | The operator at the machine | Command output, process state, measurements |
| 3. Matthew's visual acceptance | Matthew | His written decision |
| 4. Time-based gates | Elapsed continuous runtime | Checkpoint records, journal history |

**A pass in one category never implies a pass in another.** A green test suite
says nothing about focus. A sharp image says nothing about 48-hour stability. A
24-hour run is not a 48-hour run.

Record every result in `docs/acceptance/Initial-Camera-Acceptance.md` (or a new
dated record for a re-acceptance). Anything not actually performed stays
`NOT PERFORMED`; anything not actually measured stays `NOT RECORDED`. Do not
pre-fill a pass, and do not infer a value you did not read.

## 0. Evidence commands must fail closed

Every command in this procedure is read-only unless the gate it belongs to
explicitly performs an authorised start, stop, capture, restart or reboot.

Two shell helpers are used throughout. Define them once in the acceptance shell
on the Pi; they exist so a broken check cannot be mistaken for a passing one.

```bash
mgo_get() { curl --noproxy '*' -fsS "http://127.0.0.1:8080$1"; }
mgo_preview_count() { n=$(pgrep -c -x rpicam-vid || true); n=${n:-0}; if [ "$n" -eq 1 ]; then echo "PASS exactly one rpicam-vid"; else echo "FAIL expected exactly 1 rpicam-vid, found $n"; return 1; fi; }
```

Why each part matters:

- `--noproxy '*'` — a proxy variable in the environment would otherwise send a
  "local" check somewhere else entirely, and the reply would look like the Pi's.
- `-f` — without it curl prints an HTTP 404 or 500 body and exits **0**. A
  response body is not a passing endpoint check; the HTTP status is. With `-f`
  a non-2xx status is a non-zero exit.
- `-sS` — quiet, but errors are still shown.
- literal `127.0.0.1` rather than `localhost` — no DNS or `/etc/hosts` lookup
  stands between the check and the loopback interface.
- `pgrep -c -x` with an explicit `-eq 1` — "at least one" is not the gate. Zero
  processes and two processes are both failures, and both must be reported as
  such rather than summarised as "preview is running".

If a helper is not available, write the full form inline — never a bare
`curl -s localhost:...`.

---

## 1. Hardware identification

### Expected initial installation

```text
Raspberry Pi 5
Camera Module 3 Standard
Sony IMX708
standard field of view
powered autofocus
office-window installation
one fixed camera
four garden feeders
```

### Record these facts

Record each item, or `Not recorded`. Never guess a physical fact.

- Pi model
- RAM
- Operating-system version
- Kernel
- Architecture
- `rpicam-apps` version
- Detected camera index
- Detected sensor
- Supported sensor modes
- Ribbon-cable type and length
- Camera mounting method
- Distance from lens to glass
- Approximate distance to each of the four feeders
- Camera orientation
- Whether the protective lens film has been removed

Useful commands (read-only):

```bash
cat /proc/device-tree/model; uname -srm; rpicam-hello --version; rpicam-hello --list-cameras
```

---

## 2. Electrical and cable safety

The CSI ribbon is **not** hot-pluggable. Before reseating a camera cable:

1. shut the Pi down cleanly;
2. disconnect power;
3. discharge static (touch an earthed surface before handling the board);
4. avoid touching exposed contacts;
5. verify the Pi 5 **mini** camera connector end (the Pi 5 uses the narrower
   connector; a Camera Module 3 ships with a standard 15-pin cable and needs the
   mini-to-standard cable supplied for the Pi 5);
6. verify the camera's 15-pin end;
7. verify both connector locking tabs are fully seated;
8. verify cable orientation against the official Raspberry Pi camera hardware
   guide (contacts face the correct way at each end — the two ends differ);
9. reconnect power only once both ends are secured.

Do not hot-plug. Software cannot validate cable orientation mechanically: a
mis-seated cable presents as "no cameras available", which is indistinguishable
in software from an absent camera.

---

## 3. Mounting

**Level 1** requires that the mount is electrically and mechanically safe, and
that any point below which is not satisfied is written down and explicitly
accepted by Matthew for prototype use. **Level 2** requires every point to be
satisfied outright, with independent PCB support and a mount that does not bear
on the lens housing.

Confirm every point before powering on:

- the camera PCB is mechanically supported by the mount;
- the ribbon cable does **not** carry the camera's weight;
- the lens housing is not used as the mounting point;
- the camera cannot fall against the glass;
- opening or touching the window does not pull the ribbon;
- the cable's bend radius is reasonable (no sharp folds at the connector);
- no exposed conductive part can contact metal (window frame, radiator, blinds);
- ventilation around the Pi is not obstructed;
- the lens is not under continuous mechanical pressure from the window glass.

A lens positioned very close to the glass is acceptable — it is the best
reflection mitigation available — **provided the mount, not the lens, carries any
contact load.** Do not press the lens barrel against the glass to hold the
camera in place.

The two points most often unmet by a temporary mount are independent PCB support
and not using the lens housing as the mounting point. Neither may be silently
downgraded: record the actual answer, and if it is `NO`, record Matthew's
explicit acceptance of it beside the answer rather than in place of it.

---

## 4. Privacy gate

Take one full-resolution acceptance capture and review the live preview, then
confirm:

- the feeders and the intended garden area are the subject of the frame;
- neighbouring windows are excluded where practical;
- private indoor areas are excluded;
- public pavement or neighbour activity is not unnecessarily framed;
- the camera points nowhere inconsistent with a bird-observation purpose.

Record Matthew's explicit decision:

```text
PASS | FAIL | CONDITIONAL PASS
```

with conditions written out if conditional. **Privacy cannot be auto-approved.**
Do not commit the acceptance image to the repository — reference it by archive ID
and filename.

---

## 5. Field of view and feeder coverage

**Level 1** requires only that the framing is useful and that Matthew has
accepted it. **Level 2** requires a separate, complete assessment for every
intended feeder; a partial assessment is recorded per feeder, never averaged
into a single verdict.

Assess each feeder **separately**. For feeders 1–4 record:

| Field | Values |
| ----- | ------ |
| Visible | yes / no |
| Fully or partially visible | full / partial |
| Position in frame | quadrant or approximate x,y |
| Likely bird approach direction | free text |
| Obstruction | none / describe |
| Background clutter | none / low / high, describe |
| Identification features likely visible | yes / no / marginal |
| Decision | accept / reject / conditional |

**Gate:** all four feeders are visible, **or** every excluded feeder has a
deliberate written rationale that Matthew has accepted.

A detected camera is not evidence of feeder coverage. Do not record coverage
from the camera being available.

---

## 6. Bird-sized subject scale

**Level 2 — deferred hardening.** Measured subject scale is not required for
functional prototype acceptance. It is required before any claim about what the
camera can resolve at a feeder plane.

Place a bird-sized calibration object (roughly 12–18 cm, e.g. a printed target or
a tennis-ball-sized object with markings) at each practical feeder plane, or
observe a real bird there. For each feeder record:

- full capture resolution (expected `4608 x 2592`);
- subject bounding width in pixels;
- subject bounding height in pixels;
- approximate percentage of image width and height;
- whether head, bill, eye, wing and body markings are distinguishable;
- whether a useful crop can be made without excessive enlargement.

**Do not invent a universal minimum pixel threshold.** The detector and species
models have not been selected, so any number written now would be a guess that a
later task would inherit as if it were evidence. The acceptance decision is:

> The recorded pixel scale is sufficient for Matthew to distinguish useful
> identification features in a 100% crop.

Record the actual measurements so a later model-selection task can set numerical
thresholds from evidence.

---

## 7. Autofocus

**Level 2 — deferred hardening.** The complete focus matrix below is not
required for functional prototype acceptance.

The Camera Module 3 has powered autofocus. **Check the installed command's own
help and version before relying on any option**, and treat that output as
authoritative if it differs from documentation:

```bash
rpicam-still --version; rpicam-still --help; rpicam-vid --version; rpicam-vid --help
```

Current official autofocus concepts to look for: default behaviour, continuous
autofocus, one-shot/auto autofocus, autofocus-on-capture, autofocus range,
autofocus speed, autofocus window, fixed lens position, focus metric, lens
position, and autofocus state.

**MGO adds none of these to its production commands in Task 12.** The acceptance
run assesses the *current default* behaviour first; only evidence from this run
justifies adding a tuning flag later.

### Focus tests

Perform and record each:

1. preview focus after a cold application start;
2. focus after camera scene movement;
3. focus after a subject enters each feeder plane;
4. capture-time autofocus;
5. repeated stills of the same subject;
6. centre versus edge feeder sharpness;
7. focus with indoor reflections visible;
8. focus recovery after deliberately presenting a closer temporary object and
   removing it.

For each, record: autofocus state (where metadata provides it), lens position
(where metadata provides it), focus metric (where metadata provides it), whether
the feeder plane is visibly sharp, the number of successful focus cycles, and the
number of failed or obviously incorrect cycles.

**Acceptance requires repeatable, useful focus on the feeder plane** — not merely
that the lens motor moves.

---

## 8. Exposure and colour

**Level 2 — deferred hardening.** The condition matrix below spans times of day
and weather that no single session can cover, and it is not required for
functional prototype acceptance.

Test at minimum: normal daylight; bright sky behind or near the feeders; a darker
bird-sized object; mixed sun and shade; office lights off; office lights on; and
late-afternoon or lower-light conditions when practical.

Record for each condition: clipped highlights; crushed shadows; visible
dark-feather detail; visible light-feather detail; colour cast; white-balance
stability; motion blur; digital noise; exposure pumping; and whether feeder
detail remains useful.

A perfect image in every lighting condition is not required. Acceptance means the
image is **useful for evidence and future detection under representative garden
conditions**. Do not introduce custom camera tuning files in Task 12.

---

## 9. Window reflections

**Level 2 — deferred hardening.** Not required for functional prototype
acceptance.

Test: camera close to the glass; office lights off; office lights on; bright
indoor objects behind the camera; daylight at multiple angles; and a temporary
dark hood or mask where needed.

Record: reflection severity; affected feeders; time of day; whether reflections
obscure useful bird detail; mitigation tested; final mitigation selected.

Possible physical mitigations:

- moving the lens closer to the glass (without mechanically loading it — see §3);
- a dark, non-reflective hood around the lens;
- blocking stray light around the lens;
- changing the camera angle;
- turning off nearby indoor lights;
- excluding reflective frame edges later through region-of-interest
  configuration — **not implemented in Task 12**, and not to be assumed.

---

## 10. Mechanical stability

**Level 2 — deferred hardening.** Not required for functional prototype
acceptance. The prohibition on impact testing below is **not** deferred: it
applies whenever this section is performed, at either level.

With preview running, gently and safely test: opening and closing the office
door; normal desk contact; normal window-frame contact; cable movement; minor
vibration; and ordinary cleaning access.

Record whether the camera changes aim, rotates, slips, loses focus, loses
connection, or causes a preview interruption.

**Do not deliberately strike the camera, the glass, the Pi or the ribbon cable.**

Level 2 acceptance requires framing to remain materially unchanged under
ordinary disturbance. Until this section is performed, the mount's behaviour
under ordinary disturbance is simply unknown, and must be recorded as unknown
rather than assumed from the fact that nothing has moved so far.

---

## 11. Preview

Prove and record:

- the physical backend is `rpicam-vid`;
- exactly one preview process exists;
- resolution is `1280x720`;
- the configured frame rate is `15`;
- multiple browser consumers share one producer;
- motion detection and browser preview coexist;
- starting preview twice creates no duplicate process;
- stopping preview is clean;
- starting after a stop works;
- no malformed frame is observed;
- no unexpected process exit occurs;
- preview status remains truthful throughout.

```bash
pgrep -a -x rpicam-vid; mgo_preview_count; mgo_get /camera/preview/status
```

Do not claim frame-rate precision without measuring it. `15` is what MGO
*requests*; report a measured rate only if you measured one.

---

## 12. Capture

Prove and record:

- the physical backend is `rpicam-still`;
- one full-resolution JPEG is created per request;
- the reported resolution is `4608x2592`;
- the file is non-empty;
- Pillow can fully decode it;
- archive metadata matches the actual file;
- the capture ID resolves through `GET /captures/{id}`;
- no partial file remains after a controlled failure;
- a capture while preview is running temporarily releases preview;
- with `restore_after_capture = true`, preview returns to `running`;
- the restored preview has exactly one physical process;
- a capture begun while preview is stopped does **not** start preview.

This is the only authorised write in the software checks — a capture is a
deliberate act of the gate, not an incidental side effect of a status probe:

```bash
capture=$(curl --noproxy '*' -fsS -X POST http://127.0.0.1:8080/camera/capture) && echo "$capture" && mgo_get "/captures/$(echo "$capture" | python3 -c 'import json,sys; print(json.load(sys.stdin)["capture_id"])')" && mgo_preview_count
```

`-f` matters here more than anywhere else: without it a 503 (camera
unavailable) or 504 (capture timeout) would print an error body and exit 0,
and the gate would record a capture that never happened.

Acceptance captures are real evidence and may remain in the production archive.
**Do not commit them to Git.**

---

## 13. Managed preview configuration under test

The restart and reboot gates below assume the production configuration at
`/etc/garden-observatory/mgo.toml` explicitly sets:

```toml
[preview]
enabled = true
auto_start = true
restore_after_capture = true
```

Both keys default to `false`, and both tracked repository configuration files
leave them `false`. Enabling them is a deliberate, recorded edit to the external
production configuration, made under separate authorisation.

---

## 14. Application restart gate

After `systemctl restart mgo.service`, verify:

- the service becomes `active (running)`;
- camera readiness is `available`;
- preview becomes `running` **with no API start request**;
- exactly one `rpicam-vid` process exists;
- motion progresses beyond `waiting_for_frames` when motion is enabled;
- no human preview-start action was required.

```bash
systemctl is-active mgo.service; mgo_preview_count; mgo_get /camera/preview/status; mgo_get /motion/status
```

Each of those exits non-zero if its check fails, so the sequence stops being
"output appeared" and starts being "the gate passed".

---

## 15. Reboot recovery gate

After an authorised reboot, verify:

- the Pi returns to the network;
- the time is correct;
- the service is active;
- the database is healthy;
- the camera is detected;
- preview auto-started;
- exactly one preview process exists;
- no stale preview process exists;
- a still capture succeeds;
- preview is restored after that capture;
- the existing database and prior captures are still present;
- the backup timer is still active;
- no production configuration was replaced.

```bash
systemctl is-active mgo.service; mgo_preview_count; mgo_get /health; mgo_get /camera/status; mgo_get /database/status; systemctl is-active mgo-backup.timer
```

`mgo_preview_count` is what proves "exactly one preview process and no stale
one" — a stale process from before the reboot would make the count two, which
it reports as a failure rather than as "preview is running".

---

## 16. Camera-disconnect failure check

**Level 2 — deferred hardening.** Not required for functional prototype
acceptance.

Separately authorised, and only in a planned hardware window:

1. stop preview;
2. shut down and remove power;
3. disconnect the camera, or deliberately leave it unavailable;
4. boot;
5. confirm the API remains diagnosable;
6. confirm readiness is `unavailable`;
7. confirm the auto-start failure is truthful (`failed` with a real
   `last_error`, never `running`, never silently `stopped`);
8. confirm the service remains active;
9. power down again before reconnecting;
10. reconnect safely (see §2);
11. boot and confirm recovery.

**Never hot-unplug the CSI cable.** If Matthew does not authorise physical
disconnection, record this gate as `NOT PERFORMED` — never as passed.

---

## 17. Twenty-four-hour commissioning observation

**Level 2 — recommended commissioning evidence. Not required for functional
prototype completion.** It remains required before the 24-hour bring-up result
itself may be claimed: the gate is deferred, not weakened.

The camera bring-up minimum is **at least 24 continuous hours**. Record
checkpoints at approximately: start, 1 hour, 6 hours, 12 hours, 24 hours.

At each checkpoint record:

| Item | Source |
| ---- | ------ |
| UTC and SAST time | `date -u`, `date` |
| Service state | `systemctl is-active mgo.service` |
| `MainPID` | `systemctl show -p MainPID mgo.service` |
| `NRestarts` | `systemctl show -p NRestarts mgo.service` |
| Camera readiness | `mgo_get /camera/status` |
| Preview state | `mgo_get /camera/preview/status` |
| Preview `started_at` | `mgo_get /camera/preview/status` |
| Preview uptime | `mgo_get /camera/preview/status` |
| Physical preview PID | `pgrep -x rpicam-vid` (count checked with `mgo_preview_count`) |
| Database health | `mgo_get /database/status` |
| Pi temperature | `mgo_get /health` |
| Memory use | `mgo_get /health` |
| Disk use | `mgo_get /health` |
| Journal errors | `journalctl -u mgo.service -p err --since ...` |
| Malformed-frame or stream errors | journal |
| Unexpected preview restarts | preview `started_at` + journal |
| Capture count | `mgo_get /captures` |
| One controlled still where scheduled | `curl --noproxy '*' -fsS -X POST http://127.0.0.1:8080/camera/capture` |
| Whether preview restored after that capture | `mgo_get /camera/preview/status` |

**Do not claim continuous monitoring from a handful of snapshots.** Continuity is
supported by service uptime, preview process uptime, `NRestarts` and journal
history — cite those, not the fact that each snapshot happened to look fine.

Passing this gate may be recorded as:

```text
CAMERA BRING-UP PASSED
```

A run that was started and then left unobserved has not passed it. **Do not
reconstruct or back-fill a checkpoint that was missed** — an absent checkpoint
is recorded as absent, and the gate stays unclaimed.

---

## 18. Forty-eight-hour stability observation

**Level 2 — production stability and hardening gate. Not required before
continuing prototype application development.** Only a completed run may support
`CAMERA PIPELINE STABLE`.

The project test standard requires **at least 48 continuous hours** before
declaring the camera pipeline stable. The record must include:

- every 24-hour result;
- an additional 48-hour checkpoint;
- no unexplained service restart;
- no unexplained preview process restart;
- no camera disappearance;
- no unresolved malformed-frame error;
- no capture failure;
- no preview-restoration failure;
- acceptable temperatures;
- no meaningful memory growth;
- no excessive disk growth;
- a successful post-soak capture;
- Matthew's visual sign-off.

Only after this gate passes may the record state:

```text
CAMERA PIPELINE STABLE
```

A 24-hour result must never be extrapolated into this claim.

---

## 19. Human sign-off

Matthew is the final acceptance authority for: field of view; feeder coverage;
privacy; subject scale; focus; exposure; reflections; mechanical stability; and
representative garden usefulness.

The record must contain:

```text
Accepted by:
Acceptance date:
Decision:
Conditions:
Outstanding actions:
```

Evidence may be collected and described on his behalf. **The decision may not be
signed on his behalf.**

---

## 20. Evidence handling

**May be committed** to the acceptance record: capture UUID; filename; UTC
timestamp; SAST timestamp; dimensions; file size; backend; SHA-256; camera index;
sensor; metadata values; service and PID state; test outcome; Matthew's written
decision.

**Must never be committed:** JPEG bytes; thumbnails; screenshots; raw metadata
containing unnecessary paths; neighbouring private imagery; production database
copies; configuration contents; credentials; support bundles.

Avoid absolute filesystem paths in the committed record. Refer to captures by
archive ID and filename.

```bash
sha256sum /var/lib/garden-observatory/media/captures/<filename>
```

Record the digest; not the file.

---

## 21. What this procedure does not cover

Motion-triggered capture, event lifecycle, pre/post-event buffers, media
retention, regions of interest, bird detection, species identification and
notification transports are all **out of scope**. They belong to the
event-capture phase.

**That phase may begin once functional prototype acceptance (Level 1) is
recorded as passed** — including Matthew's privacy decision and his explicit
acceptance of any temporary mounting limitations. It does not wait for the
24-hour or 48-hour observation, nor for the rest of Level 2, which remain an
open parallel workstream.

What Level 1 does *not* license is a claim about the installation. Building the
next phase on a prototype camera is a project decision; describing that camera
as hardened, stable or optically characterised would be a false one.
