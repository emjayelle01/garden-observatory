"""System health collection for Matt's Garden Observatory."""

from __future__ import annotations

import platform
import shutil
import socket
import subprocess
import time
from pathlib import Path
from typing import Any

import psutil

from mgo.core.config import MGOConfig


def _temperature_c() -> float | None:
    """Read Raspberry Pi CPU temperature."""
    try:
        result = subprocess.run(
            ["vcgencmd", "measure_temp"],
            check=True,
            capture_output=True,
            text=True,
        )
        value = result.stdout.strip().replace("temp=", "").replace("'C", "")
        return float(value)
    except (FileNotFoundError, ValueError, subprocess.CalledProcessError):
        return None


def _temperature_status(temperature: float | None, config: MGOConfig) -> str:
    """Classify the current temperature."""
    if temperature is None:
        return "unknown"
    if temperature >= config.health.temperature_critical_celsius:
        return "critical"
    if temperature >= config.health.temperature_warning_celsius:
        return "warning"
    return "healthy"


def _usage_status(value: float, warning: float, critical: float) -> str:
    """Classify a percentage-based resource measurement."""
    if value >= critical:
        return "critical"
    if value >= warning:
        return "warning"
    return "healthy"


def _worst_status(*statuses: str) -> str:
    """Return the most severe known status."""
    severity = {"unknown": 0, "healthy": 1, "warning": 2, "critical": 3}
    return max(statuses, key=lambda status: severity.get(status, 0))


def collect_health(config: MGOConfig) -> dict[str, Any]:
    """Collect current system-health information."""
    disk = shutil.disk_usage(Path("/"))
    memory = psutil.virtual_memory()
    temperature = _temperature_c()
    disk_used_percent = round((disk.used / disk.total) * 100, 1)

    temperature_status = _temperature_status(temperature, config)
    disk_status = _usage_status(
        disk_used_percent,
        config.health.disk_warning_percent,
        config.health.disk_critical_percent,
    )
    memory_status = _usage_status(
        float(memory.percent),
        config.health.memory_warning_percent,
        config.health.memory_critical_percent,
    )
    overall_status = _worst_status(
        temperature_status,
        disk_status,
        memory_status,
    )

    return {
        "status": overall_status,
        "application": config.application.name,
        "hostname": socket.gethostname(),
        "architecture": platform.machine(),
        "python_version": platform.python_version(),
        "uptime_seconds": round(time.time() - psutil.boot_time()),
        "cpu_percent": psutil.cpu_percent(interval=0.1),
        "memory": {
            "total_bytes": memory.total,
            "available_bytes": memory.available,
            "used_percent": memory.percent,
            "status": memory_status,
        },
        "disk": {
            "total_bytes": disk.total,
            "free_bytes": disk.free,
            "used_percent": disk_used_percent,
            "status": disk_status,
        },
        "temperature": {
            "celsius": temperature,
            "status": temperature_status,
        },
        "camera": {
            "enabled": config.camera.enabled,
            "status": (
                "waiting_for_hardware" if not config.camera.enabled else "not_tested"
            ),
        },
    }
