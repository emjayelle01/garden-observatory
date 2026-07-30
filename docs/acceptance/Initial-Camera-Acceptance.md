# Initial camera acceptance record

**Status: PENDING — procedure implemented; physical acceptance not yet
performed.**

This is a structured evidence record, not a narrative. It is completed by an
authorised Raspberry Pi acceptance run following `docs/Camera-Acceptance.md`.

Every unverified result below is `PENDING`, `NOT PERFORMED` or `NOT RECORDED`.
Nothing here may be changed to a pass without the corresponding evidence, and no
gate may be inferred from another gate.

---

## 1. Record identity

| Field | Value |
| ----- | ----- |
| Record | Initial camera acceptance |
| Installation | Office window, Matt's Garden Observatory |
| Procedure | `docs/Camera-Acceptance.md` |
| Task | Task 12 |
| Created | 2026-07-30 |
| Run started (UTC) | NOT RECORDED |
| Run started (SAST) | NOT RECORDED |
| Run completed (UTC) | NOT RECORDED |
| Operator | NOT RECORDED |
| Acceptance authority | Matthew |

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

| Field | Value |
| ----- | ----- |
| Host | mgo-core |
| Location | Office window |
| Cameras installed | 1 |
| Feeders in garden | 4 |
| Window type | NOT RECORDED |
| Glass cleaned before run | NOT RECORDED |
| Indoor lighting during run | NOT RECORDED |

## 4. Software build

| Field | Value |
| ----- | ----- |
| Branch under test | `task-012-camera-acceptance` |
| Reviewed SHA installed on the Pi | NOT PERFORMED |
| Application version | NOT RECORDED |
| `MGO_BUILD_COMMIT` | NOT RECORDED |
| `preview.enabled` | NOT RECORDED |
| `preview.auto_start` | NOT RECORDED |
| `preview.restore_after_capture` | NOT RECORDED |
| `camera.backend` | NOT RECORDED (expected `rpicam`) |
| `motion.enabled` | NOT RECORDED |
| Pi test suite result | NOT PERFORMED |

Task 12 implementation did not access the Raspberry Pi. Every field above is
filled only by the authorised acceptance run.

## 5. Hardware identity

| Field | Value |
| ----- | ----- |
| Pi model | NOT RECORDED (expected Raspberry Pi 5) |
| RAM | NOT RECORDED |
| Operating-system version | NOT RECORDED |
| Kernel | NOT RECORDED |
| Architecture | aarch64 (recorded after the Task 11 deployment) |
| `rpicam-apps` version | NOT RECORDED |
| Detected camera index | NOT RECORDED |
| Detected sensor | Sony IMX708 / Camera Module 3 Standard (recorded after Task 11) |
| Supported sensor modes | NOT RECORDED |
| Field of view variant | Standard (not wide) |
| Ribbon-cable type | NOT RECORDED |
| Ribbon-cable length | NOT RECORDED |
| Camera mounting method | NOT RECORDED |
| Distance from lens to glass | NOT RECORDED |
| Distance to feeder 1 | NOT RECORDED |
| Distance to feeder 2 | NOT RECORDED |
| Distance to feeder 3 | NOT RECORDED |
| Distance to feeder 4 | NOT RECORDED |
| Camera orientation | NOT RECORDED |
| Protective lens film removed | NOT RECORDED |

The two pre-populated values come from the completed Task 11 deployment. They are
software-side observations (what MGO detects), not physical measurements.

## 6. Cable and connector

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
| Any hot-plugging performed | Must remain: NO |

## 7. Mounting

| Check | Result |
| ----- | ------ |
| Camera PCB mechanically supported | PENDING |
| Ribbon cable does not carry the camera's weight | PENDING |
| Lens housing not used as the mounting point | PENDING |
| Camera cannot fall against the glass | PENDING |
| Window operation does not pull the ribbon | PENDING |
| Cable bend radius reasonable | PENDING |
| No exposed conductor can contact metal | PENDING |
| Ventilation unobstructed | PENDING |
| Lens not under continuous pressure from the glass | PENDING |

## 8. Privacy

| Check | Result |
| ----- | ------ |
| Feeders and intended garden area are the subject | PENDING |
| Neighbouring windows excluded where practical | PENDING |
| Private indoor areas excluded | PENDING |
| Public pavement / neighbour activity not unnecessarily framed | PENDING |
| Framing consistent with a bird-observation purpose | PENDING |
| Full-resolution acceptance image reviewed | NOT PERFORMED |
| Live preview reviewed | NOT PERFORMED |

**Matthew's privacy decision:** PENDING (`PASS` / `FAIL` / `CONDITIONAL PASS`)

**Conditions:** NOT RECORDED

Privacy cannot be auto-approved. The acceptance image is not committed.

## 9. Feeder coverage

| Feeder | Visible | Full/partial | Position in frame | Approach direction | Obstruction | Background clutter | ID features likely visible | Decision |
| ------ | ------- | ------------ | ----------------- | ------------------ | ----------- | ------------------ | -------------------------- | -------- |
| 1 | PENDING | PENDING | NOT RECORDED | NOT RECORDED | NOT RECORDED | NOT RECORDED | PENDING | PENDING |
| 2 | PENDING | PENDING | NOT RECORDED | NOT RECORDED | NOT RECORDED | NOT RECORDED | PENDING | PENDING |
| 3 | PENDING | PENDING | NOT RECORDED | NOT RECORDED | NOT RECORDED | NOT RECORDED | PENDING | PENDING |
| 4 | PENDING | PENDING | NOT RECORDED | NOT RECORDED | NOT RECORDED | NOT RECORDED | PENDING | PENDING |

**Gate:** all four feeders visible, or a written rationale accepted by Matthew for
each exclusion. — PENDING

**Exclusion rationale (if any):** NOT RECORDED

## 10. Subject pixel scale

Capture resolution used for measurement: NOT RECORDED (expected `4608 x 2592`).

| Feeder | Subject | Width (px) | Height (px) | % of image width | % of image height | Head | Bill | Eye | Wing | Body markings | Useful 100% crop |
| ------ | ------- | ---------- | ----------- | ---------------- | ----------------- | ---- | ---- | --- | ---- | ------------- | ---------------- |
| 1 | NOT RECORDED | NOT RECORDED | NOT RECORDED | NOT RECORDED | NOT RECORDED | PENDING | PENDING | PENDING | PENDING | PENDING | PENDING |
| 2 | NOT RECORDED | NOT RECORDED | NOT RECORDED | NOT RECORDED | NOT RECORDED | PENDING | PENDING | PENDING | PENDING | PENDING | PENDING |
| 3 | NOT RECORDED | NOT RECORDED | NOT RECORDED | NOT RECORDED | NOT RECORDED | PENDING | PENDING | PENDING | PENDING | PENDING | PENDING |
| 4 | NOT RECORDED | NOT RECORDED | NOT RECORDED | NOT RECORDED | NOT RECORDED | PENDING | PENDING | PENDING | PENDING | PENDING | PENDING |

**Decision:** the recorded pixel scale is sufficient for Matthew to distinguish
useful identification features in a 100% crop. — PENDING

No universal minimum pixel threshold is asserted. The measurements exist so a
later model-selection task can derive thresholds from evidence.

## 11. Autofocus

Installed command capability check (`--version` / `--help` output is
authoritative):

| Command | Version | Autofocus options observed |
| ------- | ------- | -------------------------- |
| `rpicam-still` | NOT RECORDED | NOT RECORDED |
| `rpicam-vid` | NOT RECORDED | NOT RECORDED |

MGO production commands carry no autofocus argument in Task 12; this run assesses
default behaviour.

| Test | AF state | Lens position | Focus metric | Feeder plane visibly sharp | Successful cycles | Failed cycles | Result |
| ---- | -------- | ------------- | ------------ | -------------------------- | ----------------- | ------------- | ------ |
| Preview focus after cold start | NOT RECORDED | NOT RECORDED | NOT RECORDED | PENDING | NOT RECORDED | NOT RECORDED | PENDING |
| Focus after scene movement | NOT RECORDED | NOT RECORDED | NOT RECORDED | PENDING | NOT RECORDED | NOT RECORDED | PENDING |
| Focus as a subject enters each feeder plane | NOT RECORDED | NOT RECORDED | NOT RECORDED | PENDING | NOT RECORDED | NOT RECORDED | PENDING |
| Capture-time autofocus | NOT RECORDED | NOT RECORDED | NOT RECORDED | PENDING | NOT RECORDED | NOT RECORDED | PENDING |
| Repeated stills, same subject | NOT RECORDED | NOT RECORDED | NOT RECORDED | PENDING | NOT RECORDED | NOT RECORDED | PENDING |
| Centre vs edge feeder sharpness | NOT RECORDED | NOT RECORDED | NOT RECORDED | PENDING | NOT RECORDED | NOT RECORDED | PENDING |
| Focus with reflections visible | NOT RECORDED | NOT RECORDED | NOT RECORDED | PENDING | NOT RECORDED | NOT RECORDED | PENDING |
| Recovery after a closer object is removed | NOT RECORDED | NOT RECORDED | NOT RECORDED | PENDING | NOT RECORDED | NOT RECORDED | PENDING |

**Decision:** repeatable, useful focus on the feeder plane — PENDING

## 12. Exposure and colour

| Condition | Clipped highlights | Crushed shadows | Dark-feather detail | Light-feather detail | Colour cast | WB stability | Motion blur | Noise | Exposure pumping | Feeder detail useful |
| --------- | ------------------ | --------------- | ------------------- | -------------------- | ----------- | ------------ | ----------- | ----- | ---------------- | -------------------- |
| Normal daylight | PENDING | PENDING | PENDING | PENDING | PENDING | PENDING | PENDING | PENDING | PENDING | PENDING |
| Bright sky behind/near feeders | PENDING | PENDING | PENDING | PENDING | PENDING | PENDING | PENDING | PENDING | PENDING | PENDING |
| Darker bird-sized object | PENDING | PENDING | PENDING | PENDING | PENDING | PENDING | PENDING | PENDING | PENDING | PENDING |
| Mixed sun and shade | PENDING | PENDING | PENDING | PENDING | PENDING | PENDING | PENDING | PENDING | PENDING | PENDING |
| Office lights off | PENDING | PENDING | PENDING | PENDING | PENDING | PENDING | PENDING | PENDING | PENDING | PENDING |
| Office lights on | PENDING | PENDING | PENDING | PENDING | PENDING | PENDING | PENDING | PENDING | PENDING | PENDING |
| Late afternoon / low light | PENDING | PENDING | PENDING | PENDING | PENDING | PENDING | PENDING | PENDING | PENDING | PENDING |

**Decision:** the image is useful for evidence and future detection under
representative garden conditions — PENDING

## 13. Reflections

| Condition | Severity | Affected feeders | Time of day | Obscures useful bird detail | Mitigation tested |
| --------- | -------- | ---------------- | ----------- | --------------------------- | ----------------- |
| Lens close to glass | PENDING | NOT RECORDED | NOT RECORDED | PENDING | NOT RECORDED |
| Office lights off | PENDING | NOT RECORDED | NOT RECORDED | PENDING | NOT RECORDED |
| Office lights on | PENDING | NOT RECORDED | NOT RECORDED | PENDING | NOT RECORDED |
| Bright indoor objects behind camera | PENDING | NOT RECORDED | NOT RECORDED | PENDING | NOT RECORDED |
| Daylight, multiple angles | PENDING | NOT RECORDED | NOT RECORDED | PENDING | NOT RECORDED |
| Temporary dark hood / mask | PENDING | NOT RECORDED | NOT RECORDED | PENDING | NOT RECORDED |

**Final mitigation selected:** NOT RECORDED

Region-of-interest exclusion of reflective frame edges is **not implemented** and
must not be recorded as a mitigation in place.

## 14. Mechanical stability

| Disturbance | Aim change | Rotation | Slip | Focus loss | Connection loss | Preview interruption | Result |
| ----------- | ---------- | -------- | ---- | ---------- | --------------- | -------------------- | ------ |
| Office door opened/closed | PENDING | PENDING | PENDING | PENDING | PENDING | PENDING | PENDING |
| Normal desk contact | PENDING | PENDING | PENDING | PENDING | PENDING | PENDING | PENDING |
| Normal window-frame contact | PENDING | PENDING | PENDING | PENDING | PENDING | PENDING | PENDING |
| Cable movement | PENDING | PENDING | PENDING | PENDING | PENDING | PENDING | PENDING |
| Minor vibration | PENDING | PENDING | PENDING | PENDING | PENDING | PENDING | PENDING |
| Ordinary cleaning access | PENDING | PENDING | PENDING | PENDING | PENDING | PENDING | PENDING |

No deliberate impact testing was or will be performed.

## 15. Preview

| Check | Expected | Observed | Result |
| ----- | -------- | -------- | ------ |
| Physical backend | `rpicam-vid` | NOT RECORDED | PENDING |
| Preview processes | exactly 1 | NOT RECORDED | PENDING |
| Resolution | `1280x720` | NOT RECORDED | PENDING |
| Configured frame rate | `15` | NOT RECORDED | PENDING |
| Measured frame rate | — | NOT RECORDED | NOT PERFORMED |
| Multiple browser consumers share one producer | yes | NOT RECORDED | PENDING |
| Motion and browser preview coexist | yes | NOT RECORDED | PENDING |
| Second start creates no duplicate | yes | NOT RECORDED | PENDING |
| Stop is clean | yes | NOT RECORDED | PENDING |
| Start after stop works | yes | NOT RECORDED | PENDING |
| Malformed frames observed | none | NOT RECORDED | PENDING |
| Unexpected process exits | none | NOT RECORDED | PENDING |
| Preview status truthful throughout | yes | NOT RECORDED | PENDING |

## 16. Capture

| Check | Expected | Observed | Result |
| ----- | -------- | -------- | ------ |
| Physical backend | `rpicam-still` | NOT RECORDED | PENDING |
| JPEGs created per request | 1 | NOT RECORDED | PENDING |
| Reported resolution | `4608x2592` | NOT RECORDED | PENDING |
| File non-empty | yes | NOT RECORDED | PENDING |
| Pillow decodes it fully | yes | NOT RECORDED | PENDING |
| Archive metadata matches the file | yes | NOT RECORDED | PENDING |
| `GET /captures/{id}` resolves | yes | NOT RECORDED | PENDING |
| No partial file after a controlled failure | yes | NOT RECORDED | PENDING |

## 17. Capture-to-preview restoration

| Check | Expected | Observed | Result |
| ----- | -------- | -------- | ------ |
| Capture while preview running releases preview | yes | NOT RECORDED | PENDING |
| With `restore_after_capture = true`, preview returns to `running` | yes | NOT RECORDED | PENDING |
| Restored preview has exactly one process | yes | NOT RECORDED | PENDING |
| Capture begun with preview stopped leaves it stopped | yes | NOT RECORDED | PENDING |
| A restoration failure (if any) left the capture successful | n/a | NOT RECORDED | NOT PERFORMED |

## 18. Application restart

| Check | Expected | Observed | Result |
| ----- | -------- | -------- | ------ |
| Service active after `systemctl restart mgo.service` | yes | NOT RECORDED | PENDING |
| Camera readiness | `available` | NOT RECORDED | PENDING |
| Preview state without an API start request | `running` | NOT RECORDED | PENDING |
| `rpicam-vid` process count | 1 | NOT RECORDED | PENDING |
| Motion beyond `waiting_for_frames` (when enabled) | yes | NOT RECORDED | PENDING |
| Human preview-start action required | no | NOT RECORDED | PENDING |

## 19. Reboot recovery

| Check | Expected | Observed | Result |
| ----- | -------- | -------- | ------ |
| Pi returns to the network | yes | NOT RECORDED | PENDING |
| Time correct | yes | NOT RECORDED | PENDING |
| Service active | yes | NOT RECORDED | PENDING |
| Database healthy | yes | NOT RECORDED | PENDING |
| Camera detected | yes | NOT RECORDED | PENDING |
| Preview auto-started | yes | NOT RECORDED | PENDING |
| Preview processes | 1 | NOT RECORDED | PENDING |
| Stale preview process | none | NOT RECORDED | PENDING |
| Still capture succeeds | yes | NOT RECORDED | PENDING |
| Preview restored after capture | yes | NOT RECORDED | PENDING |
| Database and prior captures present | yes | NOT RECORDED | PENDING |
| Backup timer active | yes | NOT RECORDED | PENDING |
| Production configuration unchanged | yes | NOT RECORDED | PENDING |

## 20. Camera-disconnect test

**Status: NOT PERFORMED.**

Requires separate authorisation from Matthew and a planned hardware window. It
was not performed during Task 12 implementation and must not be recorded as
passed unless it is actually carried out.

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

| Item | Start | 1 h | 6 h | 12 h | 24 h |
| ---- | ----- | --- | --- | ---- | ---- |
| UTC time | NOT RECORDED | NOT RECORDED | NOT RECORDED | NOT RECORDED | NOT RECORDED |
| SAST time | NOT RECORDED | NOT RECORDED | NOT RECORDED | NOT RECORDED | NOT RECORDED |
| Service state | NOT RECORDED | NOT RECORDED | NOT RECORDED | NOT RECORDED | NOT RECORDED |
| `MainPID` | NOT RECORDED | NOT RECORDED | NOT RECORDED | NOT RECORDED | NOT RECORDED |
| `NRestarts` | NOT RECORDED | NOT RECORDED | NOT RECORDED | NOT RECORDED | NOT RECORDED |
| Camera readiness | NOT RECORDED | NOT RECORDED | NOT RECORDED | NOT RECORDED | NOT RECORDED |
| Preview state | NOT RECORDED | NOT RECORDED | NOT RECORDED | NOT RECORDED | NOT RECORDED |
| Preview `started_at` | NOT RECORDED | NOT RECORDED | NOT RECORDED | NOT RECORDED | NOT RECORDED |
| Preview uptime | NOT RECORDED | NOT RECORDED | NOT RECORDED | NOT RECORDED | NOT RECORDED |
| Preview PID | NOT RECORDED | NOT RECORDED | NOT RECORDED | NOT RECORDED | NOT RECORDED |
| Database health | NOT RECORDED | NOT RECORDED | NOT RECORDED | NOT RECORDED | NOT RECORDED |
| Temperature | NOT RECORDED | NOT RECORDED | NOT RECORDED | NOT RECORDED | NOT RECORDED |
| Memory use | NOT RECORDED | NOT RECORDED | NOT RECORDED | NOT RECORDED | NOT RECORDED |
| Disk use | NOT RECORDED | NOT RECORDED | NOT RECORDED | NOT RECORDED | NOT RECORDED |
| Journal errors | NOT RECORDED | NOT RECORDED | NOT RECORDED | NOT RECORDED | NOT RECORDED |
| Malformed-frame / stream errors | NOT RECORDED | NOT RECORDED | NOT RECORDED | NOT RECORDED | NOT RECORDED |
| Unexpected preview restarts | NOT RECORDED | NOT RECORDED | NOT RECORDED | NOT RECORDED | NOT RECORDED |
| Capture count | NOT RECORDED | NOT RECORDED | NOT RECORDED | NOT RECORDED | NOT RECORDED |
| Scheduled controlled still | NOT PERFORMED | NOT PERFORMED | NOT PERFORMED | NOT PERFORMED | NOT PERFORMED |
| Preview restored after that capture | NOT RECORDED | NOT RECORDED | NOT RECORDED | NOT RECORDED | NOT RECORDED |

**Continuity evidence (service uptime, preview uptime, `NRestarts`, journal
history):** NOT RECORDED

**24-hour gate:** PENDING — `CAMERA BRING-UP PASSED` may be recorded only when
this gate actually passes.

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

**48-hour stability gate:** PENDING — `CAMERA PIPELINE STABLE` must not be
claimed from the 24-hour result.

## 23. Temperature and resources

| Metric | Start | 24 h | 48 h | Trend | Acceptable |
| ------ | ----- | ---- | ---- | ----- | ---------- |
| CPU temperature | NOT RECORDED | NOT RECORDED | NOT RECORDED | NOT RECORDED | PENDING |
| Memory used | NOT RECORDED | NOT RECORDED | NOT RECORDED | NOT RECORDED | PENDING |
| Disk used | NOT RECORDED | NOT RECORDED | NOT RECORDED | NOT RECORDED | PENDING |
| Capture directory size | NOT RECORDED | NOT RECORDED | NOT RECORDED | NOT RECORDED | PENDING |

## 24. Journal review

| Item | Value |
| ---- | ----- |
| Window reviewed | NOT RECORDED |
| Error-level entries | NOT RECORDED |
| Warning-level entries of note | NOT RECORDED |
| Preview process exits | NOT RECORDED |
| Capture failures | NOT RECORDED |
| Restoration failures | NOT RECORDED |
| Unexplained entries | NOT RECORDED |

## 25. Evidence references

Captures are referenced by archive ID and filename. No image bytes are committed.

| Purpose | Capture ID | Filename | UTC | SAST | Dimensions | Bytes | Backend | SHA-256 |
| ------- | ---------- | -------- | --- | ---- | ---------- | ----- | ------- | ------- |
| Privacy / framing reference | NOT RECORDED | NOT RECORDED | NOT RECORDED | NOT RECORDED | NOT RECORDED | NOT RECORDED | NOT RECORDED | NOT RECORDED |
| Feeder coverage reference | NOT RECORDED | NOT RECORDED | NOT RECORDED | NOT RECORDED | NOT RECORDED | NOT RECORDED | NOT RECORDED | NOT RECORDED |
| Subject-scale reference | NOT RECORDED | NOT RECORDED | NOT RECORDED | NOT RECORDED | NOT RECORDED | NOT RECORDED | NOT RECORDED | NOT RECORDED |
| Post-soak capture | NOT RECORDED | NOT RECORDED | NOT RECORDED | NOT RECORDED | NOT RECORDED | NOT RECORDED | NOT RECORDED | NOT RECORDED |

## 26. Deviations

| Deviation | Reason | Accepted by |
| --------- | ------ | ----------- |
| NOT RECORDED | — | — |

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
- Passing every gate here does not imply readiness for motion-triggered capture,
  event capture or species identification.

## 28. Outstanding actions

| Action | Owner | Status |
| ------ | ----- | ------ |
| Install the reviewed Task 12 SHA on the Pi | NOT ASSIGNED | NOT PERFORMED |
| Validate the branch on ARM64 | NOT ASSIGNED | NOT PERFORMED |
| Enable managed preview in the external production configuration | NOT ASSIGNED | NOT PERFORMED |
| Execute the physical acceptance checklist | NOT ASSIGNED | NOT PERFORMED |
| Run the 24-hour gate | NOT ASSIGNED | NOT PERFORMED |
| Run the 48-hour gate | NOT ASSIGNED | NOT PERFORMED |
| Collect Matthew's decisions | NOT ASSIGNED | NOT PERFORMED |

## 29. Matthew's decision

```text
Accepted by:        NOT RECORDED
Acceptance date:    NOT RECORDED
Decision:           PENDING
Conditions:         NOT RECORDED
Outstanding actions: see section 28
```

This section is completed by Matthew. It must not be filled in on his behalf.

## 30. Final gate status

| Gate | Status |
| ---- | ------ |
| Automated software checks (hardware-free) | Passed on the development machine; NOT PERFORMED on the Pi |
| Operator-observed physical checks | NOT PERFORMED |
| Privacy | PENDING |
| Feeder coverage | PENDING |
| Subject scale | PENDING |
| Autofocus | PENDING |
| Exposure and colour | PENDING |
| Reflections | PENDING |
| Mechanical stability | PENDING |
| Preview | PENDING |
| Capture | PENDING |
| Capture-to-preview restoration | PENDING |
| Application restart | PENDING |
| Reboot recovery | PENDING |
| Camera disconnect | NOT PERFORMED |
| 24-hour camera bring-up | PENDING |
| 48-hour camera pipeline stability | PENDING |
| Matthew's sign-off | NOT GIVEN |

**Overall: PENDING — procedure implemented; physical acceptance not yet
performed.**
