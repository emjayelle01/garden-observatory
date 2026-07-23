# MGO Engineering Principles

## 1. Purpose

Matt's Garden Observatory must be built as a maintainable, testable and recoverable software appliance rather than as a collection of unrelated scripts.

Every change should improve the system without compromising evidence, stability or future extensibility.

## 2. Core Principles

### Evidence First

Original photographs, video and audio are evidence.

- Original captures must never be overwritten.
- Derived images must be stored separately.
- AI classifications must retain confidence and model metadata.
- Human corrections must not destroy the original AI result.
- The system must distinguish observation, inference and confirmation.

### Single Responsibility

Each module should have one clear responsibility.

Examples:

- Configuration loading
- Health collection
- Camera capture
- Evidence storage
- Bird detection
- Species identification
- Notifications
- API presentation

Modules must not quietly duplicate another module's logic.

### Configuration Outside Code

Runtime values must not be hard-coded when they belong in configuration.

Examples:

- Paths
- Ports
- Health thresholds
- Camera settings
- Capture intervals
- Retention periods
- Notification settings

Default configuration belongs under `config/`.

Secrets must never be committed to Git.

### Explicit Interfaces

Components must communicate through explicit functions, models, queues or stored events.

A component should not depend on another component's internal implementation.

### Preserve Raw Evidence

Raw evidence is immutable.

Processing should produce additional artefacts rather than modifying the original:

```text
original capture
    -> detection result
    -> cropped subject
    -> identification result
    -> human confirmation
