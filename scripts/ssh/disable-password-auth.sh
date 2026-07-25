#!/usr/bin/env bash
#
# disable-password-auth.sh — disable SSH password authentication safely.
#
# Run as root on the Raspberry Pi, and ONLY after you have verified key
# authentication in a separate session:
#   sudo bash scripts/ssh/disable-password-auth.sh
#
# It writes an additive drop-in (never edits the main sshd_config), validates
# the configuration with `sshd -t`, backs up any existing drop-in, and reloads
# SSH. It refuses to run if no authorized key is present for the target user, to
# avoid locking you out. Roll back at any time with enable-password-auth.sh.

set -euo pipefail

dropin_dir="/etc/ssh/sshd_config.d"
dropin="${dropin_dir}/99-mgo-ssh-hardening.conf"

if [[ "$(id -u)" -ne 0 ]]; then
  printf 'This script must run as root. Re-run with: sudo bash %s\n' "$0" >&2
  exit 1
fi

# The account you will keep logging in as (the user invoking sudo).
target_user="${SUDO_USER:-root}"
target_home="$(getent passwd "${target_user}" | cut -d: -f6)"
auth_keys="${target_home}/.ssh/authorized_keys"

printf 'Preparing to disable password authentication.\n'
printf '  target login user : %s\n' "${target_user}"
printf '  authorized_keys   : %s\n\n' "${auth_keys}"

# --- lockout guard: require at least one key for the target user -----------
if [[ ! -f "${auth_keys}" ]] || ! grep -qvE '^\s*(#|$)' "${auth_keys}"; then
  printf 'REFUSING: "%s" has no usable authorized_keys.\n' "${target_user}" >&2
  printf 'Install and verify a key first (docs/Remote-Access.md §2-§3).\n' >&2
  exit 1
fi

# --- confirm the drop-in directory is actually included --------------------
if ! grep -qiE '^\s*Include\s+/etc/ssh/sshd_config\.d/\*\.conf' /etc/ssh/sshd_config; then
  printf 'WARNING: /etc/ssh/sshd_config does not Include %s/*.conf.\n' \
    "${dropin_dir}" >&2
  printf 'The drop-in may be ignored. Add the Include line, then re-run.\n' >&2
  exit 1
fi

mkdir -p "${dropin_dir}"

# --- back up an existing drop-in -------------------------------------------
if [[ -f "${dropin}" ]]; then
  backup="${dropin}.bak.$(date +%Y%m%dT%H%M%S)"
  cp -a "${dropin}" "${backup}"
  printf 'Backed up existing drop-in to %s\n' "${backup}"
fi

# --- write the hardening drop-in -------------------------------------------
cat > "${dropin}" <<'CONF'
# Managed by MGO scripts/ssh/disable-password-auth.sh
# Disables password/keyboard-interactive auth; requires public keys.
# Remove this file (or run enable-password-auth.sh) to roll back.
PasswordAuthentication no
KbdInteractiveAuthentication no
ChallengeResponseAuthentication no
PubkeyAuthentication yes
CONF
chmod 644 "${dropin}"
printf 'Wrote %s\n' "${dropin}"

# --- validate before applying ----------------------------------------------
if ! sshd -t; then
  printf 'sshd -t failed; removing the drop-in and aborting (no change applied).\n' >&2
  rm -f "${dropin}"
  exit 1
fi

# --- reload (does not drop existing sessions) ------------------------------
if systemctl reload ssh 2>/dev/null || systemctl reload sshd 2>/dev/null; then
  printf 'SSH configuration reloaded.\n'
else
  printf 'Could not reload the SSH service automatically; reload it manually:\n' >&2
  printf '  sudo systemctl reload ssh\n' >&2
  exit 1
fi

cat <<'NEXT'

Password authentication is now disabled via the drop-in.

VERIFY NOW, from a NEW terminal, before closing this session:
  ssh <pi-user>@<pi-host> "echo still-in"                                  # must succeed (key)
  ssh -o PreferredAuthentications=password -o PubkeyAuthentication=no \
      <pi-user>@<pi-host> "echo nope"                                      # must be refused

If key login fails, roll back immediately in THIS session:
  sudo bash scripts/ssh/enable-password-auth.sh
NEXT
