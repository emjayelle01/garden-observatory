#!/usr/bin/env bash
#
# enable-password-auth.sh — roll back the MGO SSH hardening drop-in.
#
# Run as root on the Raspberry Pi:
#   sudo bash scripts/ssh/enable-password-auth.sh
#
# Removes the drop-in written by disable-password-auth.sh and reloads SSH,
# restoring the system default (password authentication enabled unless disabled
# elsewhere). It never edits the main sshd_config.

set -euo pipefail

dropin="/etc/ssh/sshd_config.d/99-mgo-ssh-hardening.conf"

if [[ "$(id -u)" -ne 0 ]]; then
  printf 'This script must run as root. Re-run with: sudo bash %s\n' "$0" >&2
  exit 1
fi

if [[ -f "${dropin}" ]]; then
  rm -f "${dropin}"
  printf 'Removed %s\n' "${dropin}"
else
  printf 'Nothing to roll back: %s is not present.\n' "${dropin}"
fi

if ! sshd -t; then
  printf 'WARNING: sshd -t reports a problem in the remaining configuration.\n' >&2
  printf 'Inspect /etc/ssh/sshd_config and /etc/ssh/sshd_config.d/ before reloading.\n' >&2
  exit 1
fi

if systemctl reload ssh 2>/dev/null || systemctl reload sshd 2>/dev/null; then
  printf 'SSH configuration reloaded. Password authentication is restored.\n'
else
  printf 'Reload the SSH service manually: sudo systemctl reload ssh\n' >&2
  exit 1
fi
