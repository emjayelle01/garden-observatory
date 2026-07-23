"""Matt's Garden Observatory application entry point."""

import platform
import socket


def main() -> None:
    """Display the initial MGO runtime identity."""
    print("Matt's Garden Observatory")
    print(f"Host: {socket.gethostname()}")
    print(f"Python: {platform.python_version()}")
    print("Status: Foundation operational")


if __name__ == "__main__":
    main()
