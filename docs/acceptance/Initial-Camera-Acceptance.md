# Initial camera acceptance record

**Status: IN PROGRESS — immediate physical and lifecycle gates performed;
continuous soak started; 24-hour and 48-hour gates pending.**

This is a structured evidence record, not a narrative. It is completed by an
authorised Raspberry Pi acceptance run following `docs/Camera-Acceptance.md`.

Every unverified result below remains `PENDING`, `NOT PERFORMED` or
`NOT RECORDED`. Nothing here may be changed to a pass without the corresponding
evidence, and no gate may be inferred from another gate. A result recorded here
was either measured by a command whose output was read, or decided by Matthew in
his own words.

---

## 1. Record identity

| Field | Value |
| ----- | ----- |
| Record | Initial camera acceptance |
| Installation | Office window, Matt's Garden Observatory |
| Procedure | `docs/Camera-Acceptance.md` |
| Task | Task 12 |
| Created | 2026-07-30 |
| Run started (UTC) | 2026-08-05T16:56:04+00:00 |
| Run started (SAST) | 2026-08-05T18:56:04+02:00 |
| Run completed (UTC) | NOT RECORDED — soak in progress |
| Operator | Claude, for objective evidence collection only |
| Acceptance authority | Matthew |

Claude collected and described evidence. Claude decided no human gate. Every
decision in sections 7, 8, 9, 10, 11, 12, 13, 14 and 29 is Matthew's.

## 2. Scope

| Item | In scope |
| ---- | -------- |
| Physical installation and mounting | Yes |
| Camera detection | Yes |
| Field of view and feeder coverage | Yes |
| Subject pixel scale | Yes |
| Autofocus at the feeder plane | Yes |
| Exposure and colour | Yes |
| Window reflections | Yes |
| Privacy framing | Yes |
| Mechanical stability | Yes |
| Preview lifecycle and streaming | Yes |
| Still capture and archiving | Yes |
| Capture-to-preview restoration | Yes |
| Application restart and reboot recovery | Yes |
| 24-hour and 48-hour gates | Yes |
| Motion-triggered capture, events, detection | **No** — later phase |
| Region-of-interest configuration | **No** — not implemented |
| Camera tuning flags (autofocus/exposure/AWB) | **No** — defaults assessed first |

## 3. Installation under test

| Field | Value | Source |
| ----- | ----- | ------ |
| Host | mgo-core | Project definition, confirmed by `hostname` |
| Location | Office window | Project definition |
| Cameras installed | 1 | Project definition |
| Feeders in garden | 4 | Project definition |
| Window type | Fixed pane | Matthew |
| Glass cleaned before run | Yes | Matthew |
| Indoor lighting during run | NOT RECORDED | — |

## 4. Software build

| Field | Value |
| ----- | ----- |
| Branch under test | `main` |
| Production runtime SHA | `938134d4f4963256cd74b5bbf59123abe49e1d5d` |
| GitHub documentation/evidence `main` | `4340dff1efa4bd81147bf9bb2eb187d01d3b78c1` |
| Application version | `0.1.0` |
| `MGO_BUILD_COMMIT` | `null` — the `/version` endpoint reports no build commit |
| `preview.enabled` | `true` |
| `preview.auto_start` | `true` — inserted by this run |
| `preview.restore_after_capture` | `true` — inserted by this run |
| `camera.backend` | `rpicam` |
| `motion.enabled` | `false` |
| Pi test suite result | NOT PERFORMED by this run — see the attribution below |

**The two-commit difference is evidence-only and was not deployed.** Production
runs `938134d…`. GitHub `main` is two commits ahead at `4340dff…`, and those two
commits touch only the deployment-gateway document, the two Task 12 records, the
mutation register and the gateway test module. They change no application code,
no gateway implementation, no configuration parsing, no camera behaviour, no
database schema, no dependency and no migration. `4340dff…` was **not**
installed on the Pi, and nothing in this record may be read as saying it was.

**Configuration state before enablement.** The external production configuration
did not contain `preview.auto_start` or `preview.restore_after_capture` at all.
Both keys were **absent**, and both effective values were `false` only through
the application's own defaults. This run inserted the two keys and set them
`true`; it changed nothing else. The record must not be read as though the keys
had previously been present and set to `false`.

**Pi test-suite attribution.** No full Raspberry Pi suite was run by this
acceptance run. The Pi suite result on record remains the separately authorised
narrow ARM64 validation of 2026-07-31, which used the simulator backend and made
no claim about the physical camera.

## 5. Hardware identity

Every populated value carries its source. A field with no source is not
recorded, and no expected value has been promoted into a result.

| Field | Value | Source |
| ----- | ----- | ------ |
| Pi model | Raspberry Pi 5 Model B Rev 1.1 | Device tree, confirmed by Matthew |
| RAM | Approximately 16 GB (17,006,182,400 bytes reported) | `/health`, confirmed by Matthew |
| Operating-system version | Raspberry Pi OS (Debian) | Observed on the host |
| Kernel | Linux 6.18.34+rpt-rpi-2712 | `uname -srm` |
| Architecture | aarch64 | `uname -m` |
| `rpicam-apps` version | v1.12.0 (12-05-2026) | `rpicam-still --version` |
| `libcamera` version | v0.7.1+rpt20260609 | `rpicam-still --version` |
| Detected camera index | 0 | Application camera status |
| Detected sensor | Sony IMX708 / Camera Module 3 Standard | Application camera status |
| Supported sensor modes | NOT RECORDED | — |
| Field of view variant | NOT RECORDED (expected: Standard, not Wide) | — |
| Ribbon-cable type | Pi 5 mini-to-standard CSI cable | Matthew |
| Ribbon-cable length | 500 mm | Matthew |
| Camera mounting method | Taped to security bar | Matthew |
| Distance from lens to glass | 5.5 cm, measured | Matthew |
| Distance to feeder 1 | Approximately 2 m, estimated | Matthew |
| Distance to feeder 2 | Approximately 2 m, estimated | Matthew |
| Distance to feeder 3 | Approximately 2 m, estimated | Matthew |
| Distance to feeder 4 | Approximately 2 m, estimated | Matthew |
| Camera orientation | Landscape | Matthew |
| Protective lens film removed | Yes | Matthew |

**`rpicam-hello --list-cameras` could not enumerate the sensor from the evidence
account, and that is a permission fact, not a camera result.** The unprivileged
`claude` account is not a member of the `video` group, so it cannot open the
camera or DMA-heap device nodes; the command reported no cameras. The service
account is in `video`, and the application's own camera status — which is
authoritative here — detected the IMX708 throughout. Supported sensor modes are
therefore `NOT RECORDED` rather than enumerated. This is recorded so a later
reader does not mistake the tool output for a camera fault.

## 6. Cable and connector

No cable work was performed or authorised during this run, so every step below
remains unperformed. The camera was already installed and connected.

| Check | Result |
| ----- | ------ |
| Pi powered down before any cable work | NOT PERFORMED |
| Power physically disconnected | NOT PERFORMED |
| Static discharged before handling | NOT PERFORMED |
| Pi 5 mini connector end verified | NOT PERFORMED |
| Camera 15-pin end verified | NOT PERFORMED |
| Both locking tabs seated | NOT PERFORMED |
| Cable orientation verified against the official guide | NOT PERFORMED |
| Power reconnected only after both ends secured | NOT PERFORMED |
| Any hot-plugging performed | NO |

## 7. Mounting

All nine answers below are Matthew's.

| Check | Result |
| ----- | ------ |
| Camera PCB mechanically supported | NO |
| Ribbon cable does not carry the camera's weight | YES |
| Lens housing not used as the mounting point | NO |
| Camera cannot fall against the glass | YES |
| Window operation does not pull the ribbon | YES |
| Cable bend radius reasonable | YES |
| No exposed conductor can contact metal | YES |
| Ventilation unobstructed | YES |
| Lens not under continuous pressure from the glass | YES |

**Matthew's mounting decision: PASS**

Two of the nine answers are `NO`: the camera PCB is not supported by the mount,
and the lens housing is being used as the mounting point. Claude raised the
contradiction between those two answers and an overall `PASS` before any reboot
was requested. Matthew reviewed it and confirmed that `PASS` stands as recorded,
that both `NO` answers are accurate, and that he accepts the residual risk for
the soak. The two `NO` answers are retained here rather than smoothed away,
because a reader of the overall decision needs to see what it was made against.

## 8. Privacy

| Check | Result |
| ----- | ------ |
| Feeders and intended garden area are the subject | YES |
| Neighbouring windows excluded where practical | YES — no neighbouring windows visible |
| Private indoor areas excluded | YES — none visible |
| Public pavement / neighbour activity not unnecessarily framed | YES — not framed |
| Framing consistent with a bird-observation purpose | YES |
| Full-resolution acceptance image reviewed | NOT RECORDED |
| Live preview reviewed | NOT RECORDED |

**Matthew's privacy decision:** PASS

**Conditions:** NONE — the decision given was `PASS`, not `CONDITIONAL PASS`.

Matthew was given the preview and dashboard addresses on his local network and
supplied the framing answers above. He did not state which view he used, so the
two review rows remain `NOT RECORDED` rather than being inferred from the fact
that he answered. Privacy cannot be auto-approved. The acceptance image is not
committed.

## 9. Feeder coverage

| Feeder | Visible | Full/partial | Position in frame | Approach direction | Obstruction | Background clutter | ID features likely visible | Decision |
| ------ | ------- | ------------ | ----------------- | ------------------ | ----------- | ------------------ | -------------------------- | -------- |
| 1 | YES | FULL | NOT RECORDED | NOT RECORDED | NOT RECORDED | LOW | YES | ACCEPT |
| 2 | YES | FULL | NOT RECORDED | NOT RECORDED | NOT RECORDED | NOT RECORDED | NOT RECORDED | NOT RECORDED |
| 3 | NOT RECORDED | NOT RECORDED | NOT RECORDED | NOT RECORDED | NOT RECORDED | NOT RECORDED | NOT RECORDED | NOT RECORDED |
| 4 | NOT RECORDED | NOT RECORDED | NOT RECORDED | NOT RECORDED | NOT RECORDED | NOT RECORDED | NOT RECORDED | NOT RECORDED |

**Gate:** all four feeders visible, or a written rationale accepted by Matthew for
each exclusion. — PENDING

**Matthew's statement, verbatim:** "All feeders re visible and are all in a line
so background clutter is similar"

Matthew states that all four feeders are visible. Per-feeder decisions were given
for feeder 1 only, and the position and approach-direction questions were
answered "not sure how to answer" — recorded as `NOT RECORDED`, not as a pass.
The gate stays `PENDING` until per-feeder decisions exist for feeders 2, 3 and 4.

## 10. Subject pixel scale

Capture resolution used for measurement: `4608 x 2592`.

| Feeder | Subject | Width (px) | Height (px) | % of image width | % of image height | Head | Bill | Eye | Wing | Body markings | Useful 100% crop |
| ------ | ------- | ---------- | ----------- | ---------------- | ----------------- | ---- | ---- | --- | ---- | ------------- | ---------------- |
| 1 | NOT RECORDED | NOT RECORDED | NOT RECORDED | NOT RECORDED | NOT RECORDED | PENDING | PENDING | PENDING | PENDING | PENDING | PENDING |
| 2 | NOT RECORDED | NOT RECORDED | NOT RECORDED | NOT RECORDED | NOT RECORDED | PENDING | PENDING | PENDING | PENDING | PENDING | PENDING |
| 3 | NOT RECORDED | NOT RECORDED | NOT RECORDED | NOT RECORDED | NOT RECORDED | PENDING | PENDING | PENDING | PENDING | PENDING | PENDING |
| 4 | NOT RECORDED | NOT RECORDED | NOT RECORDED | NOT RECORDED | NOT RECORDED | PENDING | PENDING | PENDING | PENDING | PENDING | PENDING |

**Matthew's subject-scale decision:** PENDING — no calibration object or bird was
measured during this session.

No universal minimum pixel threshold is asserted. The measurements exist so a
later model-selection task can derive thresholds from evidence.

## 11. Autofocus

Installed command capability check (`--version` / `--help` output is
authoritative):

| Command | Version | Autofocus options observed |
| ------- | ------- | -------------------------- |
| `rpicam-still` | v1.12.0 | `--autofocus-mode`, `--autofocus-range`, `--autofocus-speed`, `--autofocus-window`, `--lens-position` |
| `rpicam-vid` | v1.12.0 | `--autofocus-mode`, `--autofocus-range`, `--autofocus-speed`, `--autofocus-window`, `--lens-position` |

MGO production commands carry no autofocus argument in Task 12; this run assesses
default behaviour. The observed option names are recorded as *capability*, not as
configuration: none was added to any production command.

| Test | AF state | Lens position | Focus metric | Feeder plane visibly sharp | Successful cycles | Failed cycles | Result |
| ---- | -------- | ------------- | ------------ | -------------------------- | ----------------- | ------------- | ------ |
| Preview focus after cold start | NOT RECORDED | NOT RECORDED | NOT RECORDED | NOT RECORDED | NOT RECORDED | NOT RECORDED | GOOD (Matthew) |
| Focus after scene movement | NOT RECORDED | NOT RECORDED | NOT RECORDED | NOT RECORDED | NOT RECORDED | NOT RECORDED | NOT ASSESSED |
| Focus as a subject enters each feeder plane | NOT RECORDED | NOT RECORDED | NOT RECORDED | NOT RECORDED | NOT RECORDED | NOT RECORDED | NOT ASSESSED |
| Capture-time autofocus | NOT RECORDED | NOT RECORDED | NOT RECORDED | NOT RECORDED | NOT RECORDED | NOT RECORDED | NOT ASSESSED |
| Repeated stills, same subject | NOT RECORDED | NOT RECORDED | NOT RECORDED | NOT RECORDED | NOT RECORDED | NOT RECORDED | NOT ASSESSED |
| Centre vs edge feeder sharpness | NOT RECORDED | NOT RECORDED | NOT RECORDED | NOT RECORDED | NOT RECORDED | NOT RECORDED | NOT ASSESSED |
| Focus with reflections visible | NOT RECORDED | NOT RECORDED | NOT RECORDED | NOT RECORDED | NOT RECORDED | NOT RECORDED | NOT ASSESSED |
| Recovery after a closer object is removed | NOT RECORDED | NOT RECORDED | NOT RECORDED | NOT RECORDED | NOT RECORDED | NOT RECORDED | NOT ASSESSED |

Autofocus metadata is not exposed by the application, and the evidence account
cannot query the sensor directly, so the per-test metadata columns stay
`NOT RECORDED`. Matthew's one observation — focus after the restart — is recorded
as his, and visible lens movement was not treated as acceptance.

**Matthew's autofocus decision:** PENDING

## 12. Exposure and colour

Matthew returned this section unanswered, so nothing in it is recorded. Current
lighting condition: `NOT RECORDED`.

| Condition | Clipped highlights | Crushed shadows | Dark-feather detail | Light-feather detail | Colour cast | WB stability | Motion blur | Noise | Exposure pumping | Feeder detail useful |
| --------- | ------------------ | --------------- | ------------------- | -------------------- | ----------- | ------------ | ----------- | ----- | ---------------- | -------------------- |
| Normal daylight | PENDING | PENDING | PENDING | PENDING | PENDING | PENDING | PENDING | PENDING | PENDING | PENDING |
| Bright sky behind/near feeders | PENDING | PENDING | PENDING | PENDING | PENDING | PENDING | PENDING | PENDING | PENDING | PENDING |
| Darker bird-sized object | PENDING | PENDING | PENDING | PENDING | PENDING | PENDING | PENDING | PENDING | PENDING | PENDING |
| Mixed sun and shade | PENDING | PENDING | PENDING | PENDING | PENDING | PENDING | PENDING | PENDING | PENDING | PENDING |
| Office lights off | PENDING | PENDING | PENDING | PENDING | PENDING | PENDING | PENDING | PENDING | PENDING | PENDING |
| Office lights on | PENDING | PENDING | PENDING | PENDING | PENDING | PENDING | PENDING | PENDING | PENDING | PENDING |
| Late afternoon / low light | PENDING | PENDING | PENDING | PENDING | PENDING | PENDING | PENDING | PENDING | PENDING | PENDING |

**Matthew's exposure and colour decision:** PENDING

## 13. Reflections

Matthew returned this section unanswered.

| Condition | Severity | Affected feeders | Time of day | Obscures useful bird detail | Mitigation tested |
| --------- | -------- | ---------------- | ----------- | --------------------------- | ----------------- |
| Lens close to glass | PENDING | NOT RECORDED | NOT RECORDED | PENDING | NOT RECORDED |
| Office lights off | PENDING | NOT RECORDED | NOT RECORDED | PENDING | NOT RECORDED |
| Office lights on | PENDING | NOT RECORDED | NOT RECORDED | PENDING | NOT RECORDED |
| Bright indoor objects behind camera | PENDING | NOT RECORDED | NOT RECORDED | PENDING | NOT RECORDED |
| Daylight, multiple angles | PENDING | NOT RECORDED | NOT RECORDED | PENDING | NOT RECORDED |
| Temporary dark hood / mask | PENDING | NOT RECORDED | NOT RECORDED | PENDING | NOT RECORDED |

**Final mitigation selected:** NOT RECORDED

**Matthew's reflection decision:** PENDING

Region-of-interest exclusion of reflective frame edges is **not implemented** and
must not be recorded as a mitigation in place.

## 14. Mechanical stability

| Disturbance | Aim change | Rotation | Slip | Focus loss | Connection loss | Preview interruption | Result |
| ----------- | ---------- | -------- | ---- | ---------- | --------------- | -------------------- | ------ |
| Office door opened/closed | NOT RECORDED | NOT RECORDED | NOT RECORDED | NOT RECORDED | NOT RECORDED | NOT RECORDED | NOT PERFORMED |
| Normal desk contact | NOT RECORDED | NOT RECORDED | NOT RECORDED | NOT RECORDED | NOT RECORDED | NOT RECORDED | NOT PERFORMED |
| Normal window-frame contact | NOT RECORDED | NOT RECORDED | NOT RECORDED | NOT RECORDED | NOT RECORDED | NOT RECORDED | NOT PERFORMED |
| Cable movement | NOT RECORDED | NOT RECORDED | NOT RECORDED | NOT RECORDED | NOT RECORDED | NOT RECORDED | NOT PERFORMED |
| Minor vibration | NOT RECORDED | NOT RECORDED | NOT RECORDED | NOT RECORDED | NOT RECORDED | NOT RECORDED | NOT PERFORMED |
| Ordinary cleaning access | NOT RECORDED | NOT RECORDED | NOT RECORDED | NOT RECORDED | NOT RECORDED | NOT RECORDED | NOT PERFORMED |

**Matthew's mechanical-stability decision:** PENDING

Claude asked whether to perform this section before the soak began, since it is
one of the two gates that guard an unattended run. Matthew chose to record it
`NOT PERFORMED` and proceed. It is not recorded as passed, and it must be
completed before final sign-off.

No deliberate impact testing was or will be performed.

## 15. Preview

| Check | Expected | Observed | Result |
| ----- | -------- | -------- | ------ |
| Physical backend | `rpicam-vid` | `rpicam-vid` | PASS |
| Preview processes | exactly 1 | 1 at every check | PASS |
| Resolution | `1280x720` | `1280x720` | PASS |
| Configured frame rate | `15` | `15` | PASS |
| Measured frame rate | — | NOT RECORDED | NOT PERFORMED |
| Multiple browser consumers share one producer | yes | 2 consumers, 2,052,096 bytes each, 1 producer throughout | PASS |
| Motion and browser preview coexist | yes | NOT APPLICABLE — motion is disabled | NOT APPLICABLE |
| Second start creates no duplicate | yes | 2 starts, same producer PID, count stayed 1 | PASS |
| Stop is clean | yes | state `stopped`, 0 producers | PASS |
| Start after stop works | yes | state `running`, 1 producer | PASS |
| Malformed frames observed | none | 0 matching journal entries since boot | PASS |
| Unexpected process exits | none | 0 unexpected preview restarts in the journal | PASS |
| Preview status truthful throughout | yes | state, owner, `started_at` and `last_error` consistent with process state at every check | PASS |

Motion coexistence is **not** proven by this run: `motion.enabled` is `false`, so
there was no motion consumer to share the stream with. It is recorded as not
applicable, never as a pass.

## 16. Capture

| Check | Expected | Observed | Result |
| ----- | -------- | -------- | ------ |
| Physical backend | `rpicam-still` | `rpicam-still` | PASS |
| JPEGs created per request | 1 | 1 per request, 3 requests | PASS |
| Reported resolution | `4608x2592` | `4608x2592` | PASS |
| File non-empty | yes | 1,015,301 / 1,015,035 / 2,341,659 bytes | PASS |
| Pillow decodes it fully | yes | full decode, correct format and size, all three | PASS |
| Archive metadata matches the file | yes | ID, filename, dimensions, size and backend matched | PASS |
| `GET /captures/{id}` resolves | yes | resolved for all three | PASS |
| No partial file after a controlled failure | yes | NOT PERFORMED | NOT PERFORMED |

**No partial file after a controlled failure: NOT PERFORMED.** No safe production
failure mechanism was authorised; automated tests cover the failure-cleanup path.
It is not described as passed.

## 17. Capture-to-preview restoration

| Check | Expected | Observed | Result |
| ----- | -------- | -------- | ------ |
| Capture while preview running releases preview | yes | producer replaced, new `started_at` after the capture | PASS |
| With `restore_after_capture = true`, preview returns to `running` | yes | `running` after both the immediate and the post-reboot capture | PASS |
| Restored preview has exactly one process | yes | 1 producer after each restoration | PASS |
| Capture begun with preview stopped leaves it stopped | yes | `stopped`, 0 producers, after a successful capture | PASS |
| A restoration failure (if any) left the capture successful | n/a | NOT RECORDED | NOT PERFORMED |

Restoration is a genuine restart, not a survival: the restored preview carries a
new producer process and a new `started_at`. That is the designed behaviour and
is recorded as such rather than as an uninterrupted stream.

## 18. Application restart

The restart was performed once, through the installed deployment gateway's
`restart-api` action, at the production runtime SHA. No direct `systemctl`
restart was issued.

| Check | Expected | Observed | Result |
| ----- | -------- | -------- | ------ |
| Service active after the gateway `restart-api` | yes | active, exit 0, recovered in 2 s | PASS |
| `MainPID` changed | yes | 17702 → 21330 | PASS |
| `NRestarts` unchanged | yes | 0 before and after | PASS |
| Camera readiness | `available` | `available` | PASS |
| Preview state without an API start request | `running` | `running` 1 s after activation | PASS |
| `rpicam-vid` process count | 1 | 1 | PASS |
| Motion beyond `waiting_for_frames` (when enabled) | yes | NOT APPLICABLE — motion is disabled | NOT APPLICABLE |
| Human preview-start action required | no | no start request was issued before the check | PASS |

## 19. Reboot recovery

One controlled reboot was performed, by Matthew, from his own administrative
account.

| Check | Expected | Observed | Result |
| ----- | -------- | -------- | ------ |
| Pi returns to the network | yes | reconnected on the first bounded retry | PASS |
| Boot identifier changed | yes | changed | PASS |
| Time correct | yes | `NTPSynchronized=yes` | PASS |
| Service active | yes | active | PASS |
| Database healthy | yes | healthy, schema 2, integrity ok | PASS |
| Camera detected | yes | `available`, IMX708 | PASS |
| Preview auto-started | yes | `running`, with no API start request | PASS |
| Preview processes | 1 | 1 | PASS |
| Stale preview process | none | 0 `libcamera-vid`, no orphan `rpicam-vid` | PASS |
| Still capture succeeds | yes | succeeded, decoded, archive consistent | PASS |
| Preview restored after capture | yes | `running`, 1 producer | PASS |
| Database and prior captures present | yes | 10 records and 7 files present before the post-reboot still | PASS |
| Backup timer active | yes | active | PASS |
| Production configuration unchanged | yes | checksum unchanged since enablement | PASS |
| Installed gateway unchanged | yes | checksum unchanged | PASS |
| Approval file empty | yes | empty | PASS |

## 20. Camera-disconnect test

**Status: NOT PERFORMED.**

Requires separate authorisation from Matthew and a planned hardware window. It
was not performed during Task 12 implementation, was not performed during this
acceptance run, and must not be recorded as passed unless it is actually carried
out.

| Step | Result |
| ---- | ------ |
| Preview stopped | NOT PERFORMED |
| Shut down and power removed | NOT PERFORMED |
| Camera disconnected / left unavailable | NOT PERFORMED |
| Booted | NOT PERFORMED |
| API remained diagnosable | NOT PERFORMED |
| Readiness `unavailable` | NOT PERFORMED |
| Auto-start failure truthful | NOT PERFORMED |
| Service remained active | NOT PERFORMED |
| Powered down before reconnecting | NOT PERFORMED |
| Reconnected safely | NOT PERFORMED |
| Recovery confirmed after boot | NOT PERFORMED |

## 21. Twenty-four-hour checkpoints

The continuous clock started after the reboot-recovery gate and the post-reboot
capture had both passed.

| Item | Start | 1 h | 6 h | 12 h | 24 h |
| ---- | ----- | --- | --- | ---- | ---- |
| UTC time | 2026-08-06T06:35:36+00:00 | NOT RECORDED | NOT RECORDED | NOT RECORDED | NOT RECORDED |
| SAST time | 2026-08-06T08:35:36+02:00 | NOT RECORDED | NOT RECORDED | NOT RECORDED | NOT RECORDED |
| Service state | active | NOT RECORDED | NOT RECORDED | NOT RECORDED | NOT RECORDED |
| `MainPID` | 1511 | NOT RECORDED | NOT RECORDED | NOT RECORDED | NOT RECORDED |
| `NRestarts` | 0 | NOT RECORDED | NOT RECORDED | NOT RECORDED | NOT RECORDED |
| Camera readiness | available | NOT RECORDED | NOT RECORDED | NOT RECORDED | NOT RECORDED |
| Preview state | running | NOT RECORDED | NOT RECORDED | NOT RECORDED | NOT RECORDED |
| Preview `started_at` | 2026-08-06T06:34:37.865663+00:00 | NOT RECORDED | NOT RECORDED | NOT RECORDED | NOT RECORDED |
| Preview uptime | 59.1 s | NOT RECORDED | NOT RECORDED | NOT RECORDED | NOT RECORDED |
| Preview PID | 1952 | NOT RECORDED | NOT RECORDED | NOT RECORDED | NOT RECORDED |
| Database health | healthy | NOT RECORDED | NOT RECORDED | NOT RECORDED | NOT RECORDED |
| Temperature | 47.7 °C | NOT RECORDED | NOT RECORDED | NOT RECORDED | NOT RECORDED |
| Memory use | 4.2 % | NOT RECORDED | NOT RECORDED | NOT RECORDED | NOT RECORDED |
| Disk use | 14.4 % | NOT RECORDED | NOT RECORDED | NOT RECORDED | NOT RECORDED |
| Journal errors | 0 | NOT RECORDED | NOT RECORDED | NOT RECORDED | NOT RECORDED |
| Malformed-frame / stream errors | 0 | NOT RECORDED | NOT RECORDED | NOT RECORDED | NOT RECORDED |
| Unexpected preview restarts | 0 | NOT RECORDED | NOT RECORDED | NOT RECORDED | NOT RECORDED |
| Capture count | 11 records / 8 files | NOT RECORDED | NOT RECORDED | NOT RECORDED | NOT RECORDED |
| Scheduled controlled still | PERFORMED | NOT PERFORMED | NOT PERFORMED | NOT PERFORMED | NOT PERFORMED |
| Preview restored after that capture | yes | NOT RECORDED | NOT RECORDED | NOT RECORDED | NOT RECORDED |

**Checkpoint times due:**

| Checkpoint | UTC | SAST |
| ---------- | --- | ---- |
| 1 h | 2026-08-06T07:35:36+00:00 | 2026-08-06T09:35:36+02:00 |
| 6 h | 2026-08-06T12:35:36+00:00 | 2026-08-06T14:35:36+02:00 |
| 12 h | 2026-08-06T18:35:36+00:00 | 2026-08-06T20:35:36+02:00 |
| 24 h | 2026-08-07T06:35:36+00:00 | 2026-08-07T08:35:36+02:00 |
| 48 h | 2026-08-08T06:35:36+00:00 | 2026-08-08T08:35:36+02:00 |

**Continuity evidence (service uptime, preview uptime, `NRestarts`, journal
history):** NOT RECORDED — only the zero-hour point exists, and one point is not
continuity.

**24-hour gate:** IN PROGRESS — `CAMERA BRING-UP PASSED` may be recorded only
when this gate actually passes.

## 22. Forty-eight-hour checkpoint

| Item | 48 h |
| ---- | ---- |
| UTC time | NOT RECORDED |
| SAST time | NOT RECORDED |
| Service state | NOT RECORDED |
| `MainPID` | NOT RECORDED |
| `NRestarts` | NOT RECORDED |
| Camera readiness | NOT RECORDED |
| Preview state / uptime / PID | NOT RECORDED |
| Database health | NOT RECORDED |
| Temperature | NOT RECORDED |
| Memory use | NOT RECORDED |
| Disk use | NOT RECORDED |
| Unexplained service restarts | NOT RECORDED |
| Unexplained preview restarts | NOT RECORDED |
| Camera disappearance | NOT RECORDED |
| Unresolved malformed-frame errors | NOT RECORDED |
| Capture failures | NOT RECORDED |
| Preview-restoration failures | NOT RECORDED |
| Post-soak capture | NOT PERFORMED |

**48-hour stability gate:** IN PROGRESS — `CAMERA PIPELINE STABLE` must not be
claimed from the 24-hour result.

## 23. Temperature and resources

| Metric | Start | 24 h | 48 h | Trend | Acceptable |
| ------ | ----- | ---- | ---- | ----- | ---------- |
| CPU temperature | 47.7 °C | NOT RECORDED | NOT RECORDED | NOT RECORDED | PENDING |
| Memory used | 4.2 % | NOT RECORDED | NOT RECORDED | NOT RECORDED | PENDING |
| Disk used | 14.4 % | NOT RECORDED | NOT RECORDED | NOT RECORDED | PENDING |
| Capture directory size | NOT RECORDED | NOT RECORDED | NOT RECORDED | NOT RECORDED | PENDING |

A single reading is not a trend. Every trend cell stays unrecorded until a later
checkpoint gives it something to compare against.

## 24. Journal review

| Item | Value |
| ---- | ----- |
| Window reviewed | Since the acceptance reboot, to the zero-hour checkpoint |
| Error-level entries | 0 |
| Warning-level entries of note | NOT RECORDED |
| Preview process exits | 0 unexpected |
| Capture failures | 0 |
| Restoration failures | 0 |
| Unexplained entries | 0 |

## 25. Evidence references

Captures are referenced by archive ID and filename. No image bytes are committed,
and no filesystem path is recorded.

| Purpose | Capture ID | Filename | UTC | SAST | Dimensions | Bytes | Backend | SHA-256 |
| ------- | ---------- | -------- | --- | ---- | ---------- | ----- | ------- | ------- |
| Capture with preview running | `0e2d9a3e-81ba-49a9-a9bc-923f0d6b2b0f` | `2026-08-05T17-54-30.483486Z.jpg` | 2026-08-05T17:54:30.483486+00:00 | 2026-08-05T19:54:30+02:00 | 4608x2592 | 1015301 | `rpicam-still` | `d349190f73124b2f04fcb06cdd8b80a6a93457d2df7088542121b01f0512e816` |
| Capture with preview stopped | `54cd5d9e-29c0-444b-883a-20462b41934b` | `2026-08-05T17-55-16.391721Z.jpg` | 2026-08-05T17:55:16.391721+00:00 | 2026-08-05T19:55:16+02:00 | 4608x2592 | 1015035 | `rpicam-still` | `37965424aee79dc64ddae6fc27238f78e07fd87ccb81f582dc6244b9635b9fee` |
| Post-reboot recovery capture | `35a1ddd9-b718-4f88-bc55-2df4bbfab32f` | `2026-08-06T06-34-34.755758Z.jpg` | 2026-08-06T06:34:34.755758+00:00 | 2026-08-06T08:34:34+02:00 | 4608x2592 | 2341659 | `rpicam-still` | `57a9db921fd6f18f826885359e82c876b03f0207aa00e00f44b6ebfb34e8146c` |
| Privacy / framing reference | NOT RECORDED | NOT RECORDED | NOT RECORDED | NOT RECORDED | NOT RECORDED | NOT RECORDED | NOT RECORDED | NOT RECORDED |
| Feeder coverage reference | NOT RECORDED | NOT RECORDED | NOT RECORDED | NOT RECORDED | NOT RECORDED | NOT RECORDED | NOT RECORDED | NOT RECORDED |
| Subject-scale reference | NOT RECORDED | NOT RECORDED | NOT RECORDED | NOT RECORDED | NOT RECORDED | NOT RECORDED | NOT RECORDED | NOT RECORDED |
| Post-soak capture | NOT RECORDED | NOT RECORDED | NOT RECORDED | NOT RECORDED | NOT RECORDED | NOT RECORDED | NOT RECORDED | NOT RECORDED |

The three recorded captures are the acceptance transactions themselves. They were
not reviewed as privacy, framing or subject-scale references, so those rows stay
unrecorded rather than borrowing a capture that was taken for another purpose.

## 26. Deviations

| Deviation | Reason | Accepted by |
| --------- | ------ | ----------- |
| The authored enablement block could not run | It required `preview.auto_start` and `preview.restore_after_capture` to exist as explicit `false` assignments. Both keys were absent from the external production configuration, so the block aborted on its own precondition and its line rewriter could never insert a missing key. The run stopped before any mutation and reported it. | Matthew |
| A corrected enablement block was used | It inserts the two keys instead of rewriting them, and additionally proves that no other table changed and that `[preview]` gained exactly those two keys. Its logic was dry-run unprivileged against the live file first, writing only to a temporary directory, and the predicted result checksum matched the operator run exactly. | Matthew |
| Motion is disabled in production | `motion.enabled = false`, so motion progression and motion/browser coexistence could not be exercised. Both are recorded as not applicable, never as passed. | Recorded fact |
| Mechanical stability not performed | Matthew was asked whether to perform it before the soak and chose to record it `NOT PERFORMED` and proceed. | Matthew |
| Mounting decision carries two `NO` answers | The PCB is not supported by the mount and the lens housing is the mounting point. The contradiction with the overall `PASS` was raised before the reboot; Matthew confirmed `PASS` stands and accepted the residual risk. | Matthew |
| Data baseline file written during the resumed preflight | The original preflight stopped at the configuration check before it wrote the capture and backup baseline file. The values were re-read from scratch when the run resumed, and the file was written then. | Recorded fact |
| `rpicam-hello --list-cameras` unavailable to the evidence account | The `claude` account is not in the `video` group and cannot open the camera device nodes. Sensor identity came from the application's own status endpoint instead. | Recorded fact |
| No pull request and no merge | The evidence branch stays open for later additive checkpoint commits. | Task instruction |

## 27. Known limitations

- This record covers one fixed camera at one office window. It says nothing
  about any other position, room or camera.
- No numerical subject-scale threshold is asserted; measurements are recorded so
  a later model-selection task can derive one from evidence.
- Measured preview frame rate is only recorded if it was actually measured; the
  configured `15` is a request, not a measurement.
- The camera-disconnect gate is optional and requires Matthew's authorisation.
- Reflection mitigation by region-of-interest exclusion is not available: ROI is
  not implemented.
- Motion coexistence is unproven because motion is disabled; enabling it is a
  separate authorised change.
- The mechanical-stability gate has not been performed, so the mount's behaviour
  under ordinary disturbance is unknown for the duration of the soak.
- Feeder coverage is decided for feeder 1 only; subject scale, autofocus,
  exposure and reflections are decided for none.
- Passing every gate here does not imply readiness for motion-triggered capture,
  event capture or species identification.

## 28. Outstanding actions

| Action | Owner | Status |
| ------ | ----- | ------ |
| Install the reviewed Task 12 SHA on the Pi | Matthew | PERFORMED |
| Validate the branch on ARM64 | Claude | PERFORMED — narrow validation, 2026-07-31 |
| Enable managed preview in the external production configuration | Matthew | PERFORMED |
| Execute the immediate physical acceptance checklist | Claude | PERFORMED |
| Record which view Matthew used for the privacy and framing decisions | Matthew | NOT PERFORMED |
| Complete feeder coverage for feeders 2, 3 and 4 | Matthew | NOT PERFORMED |
| Measure subject pixel scale at each feeder plane | Matthew | NOT PERFORMED |
| Complete the autofocus assessment | Matthew | NOT PERFORMED |
| Complete exposure, colour and reflection assessment | Matthew | NOT PERFORMED |
| Perform the mechanical-stability checks | Matthew | NOT PERFORMED |
| Run the 24-hour gate | Claude | IN PROGRESS |
| Run the 48-hour gate | Claude | IN PROGRESS |
| Collect Matthew's final sign-off | Matthew | NOT PERFORMED |

## 29. Matthew's decision

```text
Accepted by:        NOT RECORDED
Acceptance date:    NOT RECORDED
Decision:           PENDING
Conditions:         NOT RECORDED
Outstanding actions: see section 28
```

This section is completed by Matthew. It must not be filled in on his behalf.
Individual sections above carry his interim decisions; this final one stays
pending until the 48-hour gate is complete.

## 30. Final gate status

| Gate | Status |
| ---- | ------ |
| Automated software checks (hardware-free) | Passed on the development machine; narrow ARM64 validation passed 2026-07-31 |
| Operator-observed physical checks | PARTIAL — mounting and privacy decided; stability not performed |
| Privacy | PASS |
| Feeder coverage | PENDING |
| Subject scale | PENDING |
| Autofocus | PENDING |
| Exposure and colour | PENDING |
| Reflections | PENDING |
| Mechanical stability | PENDING |
| Preview | PASS |
| Capture | PASS |
| Capture-to-preview restoration | PASS |
| Application restart | PASS |
| Reboot recovery | PASS |
| Camera disconnect | NOT PERFORMED |
| 24-hour camera bring-up | IN PROGRESS |
| 48-hour camera pipeline stability | IN PROGRESS |
| Matthew's sign-off | NOT GIVEN |

**Overall: IN PROGRESS — zero-hour checkpoint recorded; awaiting timed
checkpoints and final Matthew sign-off.**
