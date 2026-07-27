# Motion Detection

Matt's Garden Observatory (MGO) includes a small, deliberately simple
**motion-detection foundation**. Its single job is to decide whether the camera
scene has *changed meaningfully since the previous analysed frame* — i.e. whether
there is recent visual **activity**. It does **not** identify the cause of the
change — recognising birds (or anything else) is future work, and `motion_detected`
never means "a bird is present".

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
4. **compare** each pixel with the **previous analysed frame** (the rolling
   reference);
5. **ignore** per-pixel changes at or below `pixel_difference_threshold` (noise);
6. **compute** the proportion of changed pixels (the *score*, 0–1);
7. **report motion** when the score exceeds `changed_pixel_ratio_threshold`.

The detector is **pure and deterministic**: the same frames and configuration
always yield the same score. Frames are normalised to the analysis resolution on
decode, so the source camera resolution never affects the result, memory stays
bounded (a single small greyscale buffer), and only the single previous frame is
retained as the reference.

## Rolling-reference behaviour

Motion is measured **frame to frame**: each analysed frame is compared with the
*previous analysed frame*, not with a fixed historically quiet image. So
`motion_detected` means the scene changed meaningfully **since the last frame** —
recent visual activity — and the live status describes current activity, never
presence.

The **reference** (the frame the next frame is compared against) is:

- **established** on the first valid frame after the monitor starts (or after
  frames became unavailable); that first cycle reports `establishing_baseline`
  because no comparison was possible yet;
- **advanced after every successful comparison** — the current frame always
  becomes the reference for the next cycle, whether the result was `no_motion`
  *or* `motion_detected`;
- **preserved** across a bad or failed frame, so a single decode error never
  corrupts the reference with invalid data;
- **reset** when frames become unavailable, so recovery re-establishes it.

Because the reference advances every frame, a **lasting** scene change does not
latch forever: a bird that lands and then stays relatively still, or a feeder
that settles into a new resting position, reads as motion only while it is
*changing* and then settles back to `no_motion` even though the subject is still
in view. Conversely, **continued** activity — a bird pecking or flapping, wind
moving leaves, several birds at once — legitimately keeps reading as
`motion_detected` frame after frame.

There is deliberately **no** time-based refresh and **no** maximum-motion
timeout: the reference advances because another frame was analysed, never because
a timer elapsed.

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
| `waiting_for_frames`    | No usable preview frame was available; the reference was reset. |
| `establishing_baseline` | The first valid frame became the rolling reference; no comparison yet. |
| `no_motion`             | The latest frame-to-frame change did not exceed the threshold. |
| `motion_detected`       | The latest frame-to-frame change exceeded the threshold.   |
| `error`                 | The analysis could not be completed truthfully (decode/detector failure). |

`no_motion` describes the pixels, not the garden: it means the scene did not
change much between the two most recent frames. It does **not** mean no bird is
present, the feeder is empty, or nothing is moving. Equally, `motion_detected`
does not mean a bird caused the change — wind, leaves or shadows read the same.

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
changed_pixel_ratio_threshold = 0.08
cooldown_seconds = 5.0
```

| Key                             | Type  | Notes                                             |
| ------------------------------- | ----- | ------------------------------------------------- |
| `enabled`                       | bool  | Off by default.                                   |
| `analysis_interval_seconds`     | float | Must be positive.                                 |
| `analysis_width` / `_height`    | int   | Positive and bounded (≤ 1920 × 1080).             |
| `pixel_difference_threshold`    | int   | 0–255.                                            |
| `changed_pixel_ratio_threshold` | float | Within `(0, 1]`. Default **0.08**.                |
| `cooldown_seconds`              | float | Cannot be negative.                               |

The `changed_pixel_ratio_threshold` default of **0.08** is based on real IMX708
production measurements: quiet-scene frame-to-frame variation sat around
0.003–0.016, while clear controlled motion measured ~0.12–0.61, so 0.08
separates the two with margin. Per-site tuning may still be necessary.

The section is optional (files without it load with motion disabled). Invalid
values are rejected at startup with a clear error. Any unknown key is ignored, so
a pre-refinement config that still carries `baseline_refresh_seconds` continues
to load (that field was removed when motion became a rolling-reference detector —
a time-based refresh has no role once the reference advances every frame).
Machine-specific production configuration stays **external** to Git (see the main
README).

## Tuning

- **Too sensitive** (motion on tiny changes): raise
  `changed_pixel_ratio_threshold`, or raise `pixel_difference_threshold` to
  reject more per-pixel noise.
- **Not sensitive enough**: lower those thresholds.
- **Missing brief events**: lower `analysis_interval_seconds` (more frequent
  analysis, more CPU).
- **Slow lighting change**: the rolling reference absorbs it automatically —
  each frame is compared only with the one before it, so a gradual drift stays
  below the threshold without any refresh setting.

## Troubleshooting

| Symptom                                   | Likely cause / action                             |
| ----------------------------------------- | ------------------------------------------------- |
| Status stuck at `waiting_for_frames`      | Preview is not enabled/running. Enable `[preview]` and start it. |
| Status stuck at `disabled`                | `[motion] enabled = false`. Enable it and restart the service.   |
| Frequent `error` status                   | Malformed frames from the encoder; check preview health and logs. |
| Motion never clears                       | The scene is still *changing* every frame (wind, continuous activity). A lasting-but-static change settles to `no_motion` within a cycle or two; if it does not, raise the thresholds. |
| Too many / too few motion observations    | Tune the thresholds and `cooldown_seconds` (see Tuning).         |

## Deployment context (open garden)

The production camera watches **four bird feeders attached to a tree in an open
garden**. That scene is rarely still: birds arrive, feed, peck and leave; leaves
and branches move in wind; feeders sway; shadows shift; daylight and
auto-exposure change through the day. The detector is designed for exactly this —
it asks only *"has the scene changed since the previous frame?"*, not *"does the
scene still match some quiet reference?"*.

Consequences to keep in mind:

- ordinary garden movement (**wind, leaves, shadows, birds**) may legitimately
  produce `motion_detected` — this is activity detection, not presence detection;
- **prolonged real movement** (a bird feeding actively, sustained wind) may
  legitimately keep the status at `motion_detected` for as long as it continues;
- a bird that **lands and becomes still** may settle to `no_motion` even though
  it is still on the feeder — the pixels stopped changing;
- `no_motion` does **not** mean no bird is present, and `motion_detected` does
  **not** mean a bird caused the movement;
- the default threshold is based on initial IMX708 production measurements;
  per-site tuning may still be necessary;
- the detector does **not** filter wind, and does **not** distinguish birds from
  branches — object recognition and region-of-interest work are outside Task 4.

## Limitations

- It detects **frame-to-frame scene change only** — it cannot tell *what*
  changed; a swaying branch, a shadow or a person triggers it just as a bird
  would.
- It reports **activity, not presence**: a subject that enters and then holds
  still settles to `no_motion` while still in view, and continuous movement keeps
  reading as motion.
- It does not distinguish causes (wind vs bird vs light) and applies no
  region-of-interest masking.
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

1. **Back up** the existing production configuration (`sudo cp
   /etc/garden-observatory/mgo.toml /etc/garden-observatory/mgo.toml.bak`).
2. Align the Pi to the feature branch (`task-004-motion-detection-foundation`).
3. Add a temporary `[motion]` section to `/etc/garden-observatory/mgo.toml` with
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
15. If validation fails, **restore** the backed-up configuration (`sudo mv
    /etc/garden-observatory/mgo.toml.bak /etc/garden-observatory/mgo.toml`) and
    restart.

Production enablement of motion detection happens only during this explicit
validation step; the merged default keeps motion **disabled**.
