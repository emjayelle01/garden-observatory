# ADR-001: Establish MGO Engineering Standards

## Status

Accepted

## Context

Matt's Garden Observatory is expected to evolve beyond a simple camera script into a permanent wildlife monitoring platform.

The planned system includes camera capture, detection, species identification, audio recognition, storage, health monitoring, notifications, dashboards and integration with Matt's Viewings.

Without common engineering standards, these components could become tightly coupled, difficult to test and hard to rebuild.

## Decision

MGO will be developed as a modular Python application with:

- Git-based change control
- Semantic versioning
- External configuration
- Explicit component boundaries
- Immutable original evidence
- Ruff formatting and linting
- mypy static type checking
- pytest automated testing
- Architecture Decision Records for significant decisions

Hardware-dependent code will be isolated so most tests can run without connected hardware.

## Consequences

### Positive

- Easier maintenance
- Safer changes
- Repeatable deployments
- Better fault isolation
- Clear architectural history
- Easier future expansion

### Negative

- Some initial development takes longer.
- Small features may require tests and documentation.
- Architectural discipline must be maintained consistently.

## Alternatives Considered

### Collection of independent scripts

Rejected because it would be quick initially but difficult to operate and extend reliably.

### Container-first architecture

Deferred because containers would complicate access to Raspberry Pi camera and system libraries during the initial development phase.

### Multiple independent services immediately

Deferred. MGO will begin as a modular application and split into separate processes only when operational requirements justify that complexity.
