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


def _temperature_status(
    temperature: float | None,
    config: MGOConfig,
) -> str:
    """Classify the current temperature."""
    if temperature is None:
        return "unknown"

    if temperature >= config.health.temperature_critical_c:
        return "critical"

    if temperature >= config.health.temperature_warning_c:
        return "warning"

    return "healthy"


def collect_health(config: MGOConfig) -> dict[str, Any]:
    """Collect current system-health information."""
    disk = shutil.disk_usage(Path("/"))
    memory = psutil.virtual_memory()
    temperature = _temperature_c()
    disk_used_percent = round((disk.used / disk.total) * 100, 1)

    overall_status = "healthy"

    if temperature is not None:
        if temperature >= config.health.temperature_critical_c:
            overall_status = "critical"
        elif temperature >= config.health.temperature_warning_c:
            overall_status = "warning"

    if disk_used_percent >= config.health.disk_critical_percent:
        overall_status = "critical"
    elif (
        disk_used_percent >= config.health.disk_warning_percent
        and overall_status == "healthy"
    ):
        overall_status = "warning"

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
        },
        "disk": {
            "total_bytes": disk.total,
            "free_bytes": disk.free,
            "used_percent": disk_used_percent,
        },
        "temperature": {
            "celsius": temperature,
            "status": _temperature_status(temperature, config),
        },
        "camera": {
            "enabled": config.camera.enabled,
            "status": "waiting_for_hardware"
            if not config.camera.enabled
            else "not_tested",
        },
    }
