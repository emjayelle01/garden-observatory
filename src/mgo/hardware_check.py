"""Basic hardware and runtime checks for Matt's Garden Observatory."""

from __future__ import annotations

import platform
import shutil
import socket
import subprocess


def command_output(command: list[str]) -> str:
    """Run a command and return concise output."""
    try:
        result = subprocess.run(
            command,
            check=True,
            capture_output=True,
            text=True,
        )
        return result.stdout.strip()
    except (subprocess.CalledProcessError, FileNotFoundError) as exc:
        return f"Unavailable: {exc}"


def module_status(module_name: str) -> str:
    """Report whether a Python module is importable."""
    try:
        __import__(module_name)
        return "OK"
    except Exception as exc:
        return f"FAILED: {exc}"


def main() -> None:
    """Display the current MGO hardware-readiness state."""
    print("Matt's Garden Observatory — Hardware Check")
    print(f"Hostname: {socket.gethostname()}")
    print(f"Architecture: {platform.machine()}")
    print(f"Python: {platform.python_version()}")
    print(f"Picamera2: {module_status('picamera2')}")
    print(f"libcamera: {module_status('libcamera')}")
    print(f"rpicam-hello: {'Present' if shutil.which('rpicam-hello') else 'Missing'}")
    print(f"Temperature: {command_output(['vcgencmd', 'measure_temp'])}")


if __name__ == "__main__":
    main()
