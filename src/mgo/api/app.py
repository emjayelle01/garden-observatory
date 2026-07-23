"""FastAPI application for Matt's Garden Observatory."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Query

from mgo.core.config import load_config
from mgo.core.database import apply_migrations
from mgo.core.health import collect_health
from mgo.core.observations import list_observations, record_observation

config = load_config()


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    """Initialise persistent MGO services during application startup."""
    applied_versions = apply_migrations(config.storage.database_path)

    if applied_versions:
        record_observation(
            config.storage.database_path,
            kind="database_migration",
            source="mgo-api",
            status="success",
            summary="Database migrations applied",
            payload={"versions": applied_versions},
        )

    record_observation(
        config.storage.database_path,
        kind="application_start",
        source="mgo-api",
        status="success",
        summary="MGO API started",
        payload={"version": "0.1.0"},
    )

    yield

    record_observation(
        config.storage.database_path,
        kind="application_stop",
        source="mgo-api",
        status="success",
        summary="MGO API stopped",
        payload={"version": "0.1.0"},
    )


app = FastAPI(
    title=config.application.name,
    version="0.1.0",
    description="API for Matt's Garden Observatory.",
    lifespan=lifespan,
)


@app.get("/")
def root() -> dict[str, str]:
    """Return the application identity."""
    return {
        "name": config.application.name,
        "version": "0.1.0",
        "status": "operational",
    }


@app.get("/health")
def health() -> dict[str, Any]:
    """Return live system-health information."""
    return collect_health(config)


@app.get("/observations")
def observations(
    limit: int = Query(default=100, ge=1, le=1000),
    kind: str | None = None,
) -> list[dict[str, Any]]:
    """Return the latest observatory timeline entries."""
    records = list_observations(
        config.storage.database_path,
        limit=limit,
        kind=kind,
    )

    return [
        {
            "id": record.id,
            "observed_at": record.observed_at,
            "kind": record.kind,
            "source": record.source,
            "status": record.status,
            "summary": record.summary,
            "payload": record.payload,
            "correlation_id": record.correlation_id,
            "created_at": record.created_at,
        }
        for record in records
    ]
