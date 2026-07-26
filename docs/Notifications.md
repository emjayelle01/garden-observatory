# Notifications

MGO includes a **notification framework foundation**: event-driven
infrastructure that lets any part of the application announce that something
happened without knowing — or caring — how that announcement is delivered.

This task is infrastructure only. There is **no bird recognition**, **no
notification policy** (no quiet hours, throttling or routing rules), and **no
real transport** (no Telegram, email or SMS). Those belong to future tasks;
this framework is what they will plug into.

## Architecture

```
producer (lifespan / camera monitor / motion monitor / future features)
    │
    ▼
NotificationEvent          typed, immutable, structured payload
    │
    ▼
NotificationManager        registration, fan-out, failure isolation, counters
    │
    ▼
NotificationProvider       abstract send(event) -> DeliveryResult
    │
    ▼
delivery transport         today: the application log, or nothing
```

The pieces live in `src/mgo/notifications/`:

| Module         | Responsibility                                              |
| -------------- | ----------------------------------------------------------- |
| `models.py`    | `NotificationEvent`, `EventType`, `EventSeverity`, `create_event` |
| `providers.py` | `NotificationProvider` ABC, `LoggingProvider`, `NullProvider`, `build_provider` |
| `manager.py`   | `NotificationManager`, `build_notification_manager`, status snapshot |

## Why business logic never talks to providers

A producer that called Telegram (or any transport) directly would couple every
feature to every delivery mechanism: adding a transport would mean touching the
camera monitor, the motion monitor and every future producer; a transport
outage could break the feature that tried to announce it; and testing a
feature would require faking a transport.

Instead, producers know exactly one thing: *publish this typed event to the
manager*. The manager decides which providers receive it and absorbs every
delivery problem. Adding Telegram later means writing one new provider class
and registering it — **zero producer code changes**.

## Event flow

1. A producer builds a `NotificationEvent` with `create_event(...)`, which
   stamps a UUID `event_id` and a timezone-aware UTC `timestamp` (both
   injectable for deterministic tests).
2. The producer calls `NotificationManager.publish(event)`.
3. The manager fans the event out to every registered provider, in
   registration order.
4. Each provider returns a `DeliveryResult` (success/failure with detail).
   A provider that **raises** is caught, logged and counted as a failure —
   one provider failure never prevents delivery to the others, and `publish`
   itself never raises into the producer.
5. The manager updates its counters (events published, delivery failures,
   last event timestamp), which `GET /notifications/status` reports.

When notifications are **disabled** (the default), the manager is a truthful
no-op: `publish` accepts nothing, delivers nothing and counts nothing, so
producers always publish unconditionally without checking configuration.

## Event model

Each event carries:

| Field            | Meaning                                                        |
| ---------------- | -------------------------------------------------------------- |
| `event_id`       | Unique identifier (UUID4 by default).                          |
| `timestamp`      | Timezone-aware UTC instant the event was created.              |
| `event_type`     | One of the `EventType` enum values below.                      |
| `severity`       | `info` / `warning` / `error` / `critical` — advisory metadata for providers. |
| `source`         | The producing subsystem (`mgo-api`, `mgo-camera`, `mgo-motion`). |
| `title`          | Short human-readable headline (plain text).                    |
| `summary`        | One or two sentences of context (plain text).                  |
| `payload`        | **Structured** JSON-compatible facts — never a pre-formatted message. |
| `correlation_id` | Optional link to a related record (e.g. an observation id).    |

The payload stays structured because formatting is a **per-transport**
concern: a future Telegram provider will render an event as Markdown, an email
provider as HTML, the logging provider as a log line — all from the same
structured facts.

### Event types

`EventType` is deliberately generic infrastructure vocabulary:

| Type                   | Emitted today by                                     |
| ---------------------- | ---------------------------------------------------- |
| `system_start`         | Application startup (lifespan).                      |
| `system_stop`          | Application shutdown (lifespan).                     |
| `camera_available`     | Camera monitor, when readiness becomes available.    |
| `camera_unavailable`   | Camera monitor, on any material change to a non-available state. |
| `motion_state_changed` | Motion monitor, on every material motion transition. |
| `new_observation`      | Reserved — no producer yet.                          |
| `error`                | Reserved — no producer yet.                          |

No species-specific event exists; bird events belong to a future task and will
be added to this enum when that task defines them.

## Provider model

`NotificationProvider` is an abstract base class:

```python
class NotificationProvider(ABC):
    @property
    def name(self) -> str: ...                            # short stable id
    def send(self, event: NotificationEvent) -> DeliveryResult: ...
```

The contract: return a `DeliveryResult(success, detail)` cleanly rather than
raising. The manager tolerates exceptions regardless, so a faulty provider can
never crash publication.

Two providers exist, both transport-free by design:

- **`LoggingProvider`** (`provider = "log"`) — writes each event to the
  application log at a level matching its severity. It exists to validate the
  framework end to end: enable notifications with this provider and every
  published event becomes an observable log line.
- **`NullProvider`** (`provider = "null"`) — accepts and discards every event.
  It exists for tests and for configurations where notifications are wired but
  intentionally deliver nowhere.

## The manager

`NotificationManager` is responsible for:

- **registration** — `register_provider(provider)`; duplicate names are
  rejected so status and fan-out stay unambiguous;
- **publication** — `publish(event)` fans out to all providers and returns a
  `PublishResult` with per-provider outcomes;
- **failure isolation** — each provider is attempted independently; raising or
  failing providers are logged and counted, never propagated;
- **structured logging** — registrations, delivery failures and dropped
  (disabled) events are logged with event type and id;
- **status** — `status()` returns a read-only snapshot for the API.

The application builds one manager at startup with
`build_notification_manager(config.notifications)` and attaches it to
`app.state`.

## Integration points (this task only)

| Producer            | Trigger                                    | Event                              |
| ------------------- | ------------------------------------------ | ---------------------------------- |
| Application lifespan| startup / shutdown                         | `system_start` / `system_stop`     |
| Camera monitor      | material readiness change                  | `camera_available` / `camera_unavailable` |
| Motion monitor      | material motion transition (post-cooldown) | `motion_state_changed`             |

The monitors stay transport-agnostic: they accept an optional callback
(`on_material_change` on the camera monitor, `transition_listener` on the
motion observer) and the application wires those callbacks to the manager. A
callback failure is isolated inside the monitor, so notifications can never
break a readiness check or an analysis cycle. Notification fan-out therefore
piggybacks on the existing *material change* semantics — the same transitions
that create observations — so notifications add no new noise.

No other integrations exist yet.

## Configuration

```toml
[notifications]
enabled = false     # notifications are off unless explicitly enabled
provider = "log"    # "log" (application log) or "null" (discard)
```

Defaults are safe: notifications are **disabled**, no provider is constructed,
and every published event is dropped. The `[notifications]` section is
optional — configuration files without it load unchanged. An unsupported
provider name is rejected at startup with a clear error (even when disabled,
so misconfiguration fails fast).

Future transports extend this section with their own settings; **no Telegram
tokens or SMTP details belong here yet**.

## `GET /notifications/status`

Read-only and truthful: it reflects the manager's live counters and never
publishes anything itself.

```json
{
  "enabled": true,
  "providers": ["log"],
  "total_events_published": 4,
  "total_delivery_failures": 0,
  "last_event_at": "2026-07-26T12:00:00+00:00"
}
```

There is deliberately **no event history endpoint** and **no persistence** of
notifications: this task only delivers events. (Material camera/motion
transitions are still persisted as *observations*, exactly as before —
independent of notifications.)

## Future Telegram / email integration

When a real transport task arrives, it will:

1. add a new `NotificationProvider` subclass (e.g. `TelegramProvider`) whose
   `send` renders the structured event for that transport;
2. add the provider's name to `SUPPORTED_NOTIFICATION_PROVIDERS` and the
   factory in `providers.py`, plus its settings to `[notifications]`;
3. register it via `build_notification_manager` (extending it to multiple
   simultaneous providers if required).

Producers, the event model, the manager and the API are unchanged by that
step — which is the point of this foundation.
