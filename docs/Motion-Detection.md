# Motion Detection

Matt's Garden Observatory (MGO) includes a small, deliberately simple
**motion-detection foundation**. Its single job is to decide whether the camera
scene has *meaningfully changed*. It does **not** identify the cause of the
change — recognising birds (or anything else) is future work.

This document describes the architecture, algorithm, configuration, runtime
behaviour, API, limitations and the production-validation procedure.

## Scope

**In scope (implemented):**

- scene-change detection over live preview frames;
- a truthful motion status held in application state and exposed via the API;
- persisted observations for material motion transitions.

**Out of scope (not implemented here):**

- bird detection, species identification or any object recognition;
- object tracking, bounding boxes or classification;
- automatic still capture triggered by motion;
- notifications of any kind;
- any heavy AI/ML framework (TensorFlow, PyTorch, YOLO, OpenCV, …).

Motion detection is **disabled by default**.

## Architecture

Motion detection lives in the `mgo.motion` package and is kept separate from
readiness detection (`mgo.core.camera`), still capture (`mgo.camera.capture`) and
preview/streaming (`mgo.camera.preview`, `mgo.camera.streaming`).

| Module                      | Responsibility                                            |
| --------------------------- | --------------------------------------------------------- |
| `mgo.motion.models`         | `MotionStatus` vocabulary and the `MotionResult` value.   |
| `mgo.motion.detector`       | `MotionDetector` protocol + `FrameDifferenceDetector`.    |
| `mgo.motion.frame_source`   | `MotionFrameSource` protocol + `BrokerFrameSource`.       |
| `mgo.motion.monitor`        | Background `run_motion_monitor` + `MotionState` holder.   |

The API exposes `GET /motion/status`, which only reads `MotionState`; it never
runs a comparison.

## Frame source (single camera owner)

The Raspberry Pi camera is a single shared hardware resource. Exactly one
component reads the preview process's MJPEG output: the streaming
`MjpegBroker`, which fans frames out to all consumers. Motion detection becomes
**another consumer** of that broker via `BrokerFrameSource` — it does **not**
start a second `rpicam` process.

Consequences:

- browser live preview and motion monitoring **coexist**;
- a browser disconnecting does not stop the frames the monitor needs;
- stopping the monitor does not stop the frames a browser needs;
- when preview is not running, the monitor simply reports `waiting_for_frames`.

Motion detection **does not** start preview automatically. To feed the monitor,
enable and start the preview (`[preview]` enabled; `POST /camera/preview/start`).

## Detection algorithm

`FrameDifferenceDetector` implements a deliberately simple, explainable
frame-difference algorithm — no background modelling, optical flow, segmentation
or machine learning:

1. **decode** the JPEG frame (via Pillow);
2. **reduce** it to a small analysis resolution (`analysis_width` ×
   `analysis_height`);
3. **convert** it to greyscale (luminance);
4. **compare** each pixel with the reference (baseline) frame;
5. **ignore** per-pixel changes at or below `pixel_difference_threshold` (noise);
6. **compute** the proportion of changed pixels (the *score*, 0–1);
7. **report motion** when the score exceeds `changed_pixel_ratio_threshold`.

The detector is **pure and deterministic**: the same frames and configuration
always yield the same score. Frames are normalised to the analysis resolution on
decode, so the source camera resolution never affects the result, memory stays
bounded (a single small greyscale buffer), and no frame history is retained.

## Baseline behaviour

The **baseline** is the reference frame the current frame is compared against.

- **Established** on the first frame after the monitor starts (or after frames
  became unavailable). That first cycle reports `establishing_baseline`.
- **Kept** while motion is detected, so a subject that enters and stays reads as
  one continuous motion event until it leaves.
- **Refreshed** during quiet periods once it is older than
  `baseline_refresh_seconds`, to absorb gradual lighting change without erasing a
  real event.
- **Reset** when frames become unavailable, so recovery re-establishes it.

Because motion is reported while the scene differs from the last *quiet*
baseline, a subject that enters and remains reads as motion for the duration of
its visit, and the status returns to `no_motion` once the scene matches the
baseline again.

## Monitor lifecycle

`run_motion_monitor` is an application-managed asyncio task, mirroring the
health and camera monitors:

- it **starts** during application startup **only when motion is enabled**;
- when **disabled**, it records a truthful `disabled` state and consumes no
  frames;
- it analyses **one frame per `analysis_interval_seconds`** (far slower than the
  preview frame rate), never every frame, and never busy-loops;
- frame reads and pixel comparison run in **worker threads**, so slow analysis
  never blocks the event loop or other frame consumers;
- it **recovers** from transient frame unavailability and detector errors;
- it **stops cleanly** on shutdown, releasing its broker subscription — no
  orphaned task, subscription or camera process remains.

## API state meanings

`GET /motion/status` returns a typed, read-only projection of the latest result:

| Field              | Meaning                                                       |
| ------------------ | ------------------------------------------------------------- |
| `enabled`          | Whether motion detection is on (false only when `disabled`).  |
| `status`           | One of the statuses below.                                    |
| `detected`         | `true` only alongside `motion_detected`.                      |
| `score`            | Changed-pixel ratio (0–1); `0.0` when no comparison ran.      |
| `threshold`        | The configured `changed_pixel_ratio_threshold`.               |
| `frames_available` | Whether a usable frame backed this evaluation.                |
| `detail`           | A concise, bounded human-readable explanation.                |
| `evaluated_at`     | UTC timestamp of the evaluation.                              |

| Status                  | Meaning                                                    |
| ----------------------- | ---------------------------------------------------------- |
| `disabled`              | Motion detection is off by configuration.                  |
| `waiting_for_frames`    | Enabled, but no preview frame is available yet.            |
| `establishing_baseline` | The first frame is being adopted as the reference.         |
| `no_motion`             | A comparison ran; change stayed within threshold.          |
| `motion_detected`       | A comparison ran; change exceeded the threshold.           |
| `error`                 | A frame could not be decoded or the detector failed.       |

The endpoint always returns `200` when the application is healthy (including the
`disabled` and `waiting_for_frames` states), and requesting it triggers no
hardware activity.

## Observations

Motion transitions are recorded in the existing immutable observation model
(kind `motion_status`, source `mgo-motion`) — there is no separate event
database. A `motion_status` observation is persisted only on a **material
transition** of status; a continuous or repeated event does not flood the
timeline:

- persistence happens on a change of status (e.g.
  `no_motion → motion_detected`, `motion_detected → no_motion`);
- re-entry into `motion_detected` within `cooldown_seconds` of the last recorded
  motion is **suppressed**, so a flickering event is recorded once;
- the eventual return to `no_motion` after a *recorded* motion is **always**
  persisted — cooldown never hides it.

The **live status** (`GET /motion/status`) continues to update every cycle even
while observation persistence is suppressed. Observation payloads are compact
(`status`, `detected`, `score`, `threshold`, `frames_available`, `detail`,
`evaluated_at`); **frame bytes are never persisted**.

## Configuration

```toml
[motion]
enabled = false
analysis_interval_seconds = 1.0
analysis_width = 160
analysis_height = 90
pixel_difference_threshold = 20
changed_pixel_ratio_threshold = 0.02
baseline_refresh_seconds = 30.0
cooldown_seconds = 5.0
```

| Key                             | Type  | Notes                                             |
| ------------------------------- | ----- | ------------------------------------------------- |
| `enabled`                       | bool  | Off by default.                                   |
| `analysis_interval_seconds`     | float | Must be positive.                                 |
| `analysis_width` / `_height`    | int   | Positive and bounded (≤ 1920 × 1080).             |
| `pixel_difference_threshold`    | int   | 0–255.                                            |
| `changed_pixel_ratio_threshold` | float | Within `(0, 1]`.                                  |
| `baseline_refresh_seconds`      | float | Must be positive.                                 |
| `cooldown_seconds`              | float | Cannot be negative.                               |

The section is optional (files without it load with motion disabled). Invalid
values are rejected at startup with a clear error. Machine-specific production
configuration stays **external** to Git (see the main README).

## Tuning

- **Too sensitive** (motion on tiny changes): raise
  `changed_pixel_ratio_threshold`, or raise `pixel_difference_threshold` to
  reject more per-pixel noise.
- **Not sensitive enough**: lower those thresholds.
- **Missing brief events**: lower `analysis_interval_seconds` (more frequent
  analysis, more CPU).
- **False motion from slow lighting change**: lower `baseline_refresh_seconds` so
  the baseline adapts faster during quiet periods.

## Troubleshooting

| Symptom                                   | Likely cause / action                             |
| ----------------------------------------- | ------------------------------------------------- |
| Status stuck at `waiting_for_frames`      | Preview is not enabled/running. Enable `[preview]` and start it. |
| Status stuck at `disabled`                | `[motion] enabled = false`. Enable it and restart the service.   |
| Frequent `error` status                   | Malformed frames from the encoder; check preview health and logs. |
| Motion never clears                       | A persistent scene change; the baseline refreshes during quiet periods. |
| Too many / too few motion observations    | Tune the thresholds and `cooldown_seconds` (see Tuning).         |

## Limitations

- It detects **scene change only** — it cannot tell *what* changed; a swaying
  branch, a shadow or a person triggers it just as a bird would.
- A subject that enters and stays reads as motion for its whole visit.
- Sudden lighting changes can register as motion until the baseline refreshes.
- There is no incident grouping or visit-session modelling.

## Off-Pi / Windows behaviour

Motion detection is fully hardware-safe. On Windows and in CI there is no
Raspberry Pi camera tooling, so with the defaults the monitor is not started and
`GET /motion/status` reports `disabled`. If enabled without a running preview, it
reports `waiting_for_frames`. The application always starts and serves normally,
and the automated tests never require hardware or run `rpicam-*` commands.

## Production-validation procedure (Raspberry Pi)

Perform this only after the feature branch has been reviewed and approved. Do
**not** merge before validation.

1. **Back up** the existing production configuration
   (`sudo cp /etc/mgo/mgo.toml /etc/mgo/mgo.toml.bak`).
2. Align the Pi to the feature branch (`task-004-motion-detection-foundation`).
3. Add a temporary `[motion]` section to `/etc/mgo/mgo.toml` with
   `enabled = true`, and ensure `[preview]` is enabled.
4. Start the service.
5. Confirm `GET /health` is healthy.
6. Confirm `GET /camera/status` reports `available`.
7. Open `GET /preview` in a browser and confirm **live preview still works**.
8. Confirm `GET /motion/status` begins in a truthful `waiting_for_frames` or
   `establishing_baseline`/`no_motion` state (after preview is running).
9. Introduce controlled movement in view; confirm the status transitions to
   `motion_detected`.
10. Remove the movement; confirm the status returns to `no_motion`.
11. Inspect `GET /observations?kind=motion_status` and confirm duplicate
    suppression (a continuous event is not recorded repeatedly).
12. Confirm `POST /camera/capture` still works while motion monitoring is enabled
    (capture takes camera priority; preview is released and can be restarted).
13. Confirm browser preview and the motion monitor **coexist**.
14. Stop the service; confirm **no orphaned `rpicam-vid` process** remains
    (`pgrep -a rpicam-vid` returns nothing).
15. If validation fails, **restore** the backed-up configuration
    (`sudo mv /etc/mgo/mgo.toml.bak /etc/mgo/mgo.toml`) and restart.

Production enablement of motion detection happens only during this explicit
validation step; the merged default keeps motion **disabled**.
