#!/usr/bin/env bash
#
# verify-key-auth.sh — non-destructive SSH key-authentication readiness check.
#
# Run as your normal login user on the Raspberry Pi:
#   bash scripts/ssh/verify-key-auth.sh
#
# It only reads state and prints findings; it changes nothing. Use it before and
# after installing your public key, and before disabling password auth.

set -euo pipefail

ssh_dir="${HOME}/.ssh"
auth_keys="${ssh_dir}/authorized_keys"
problems=0

note() { printf '  - %s\n' "$1"; }
fail() { printf '  ! %s\n' "$1"; problems=$((problems + 1)); }

printf 'MGO SSH key-authentication check for user "%s" on "%s"\n\n' \
  "$(whoami)" "$(hostname)"

# --- authorized_keys presence and permissions ------------------------------
printf 'authorized_keys:\n'
if [[ -d "${ssh_dir}" ]]; then
  dir_perm="$(stat -c '%a' "${ssh_dir}")"
  if [[ "${dir_perm}" == "700" ]]; then
    note "~/.ssh permissions are 700"
  else
    fail "~/.ssh should be 700 but is ${dir_perm} (run: chmod 700 ~/.ssh)"
  fi
else
  fail "~/.ssh does not exist yet (install a public key first)"
fi

if [[ -f "${auth_keys}" ]]; then
  key_perm="$(stat -c '%a' "${auth_keys}")"
  if [[ "${key_perm}" == "600" ]]; then
    note "authorized_keys permissions are 600"
  else
    fail "authorized_keys should be 600 but is ${key_perm} (run: chmod 600 ${auth_keys})"
  fi
  key_count="$(grep -cvE '^\s*(#|$)' "${auth_keys}" || true)"
  if [[ "${key_count}" -gt 0 ]]; then
    note "${key_count} authorized key(s) installed"
  else
    fail "authorized_keys is empty"
  fi
else
  fail "no authorized_keys file (install your public key — see docs/Remote-Access.md §2)"
fi

# --- current effective password-auth setting -------------------------------
printf '\npassword authentication (effective):\n'
if command -v sshd >/dev/null 2>&1 && [[ "$(id -u)" -eq 0 ]]; then
  pw="$(sshd -T 2>/dev/null | awk '/^passwordauthentication/ {print $2}')"
  note "PasswordAuthentication ${pw:-unknown} (from sshd -T)"
else
  note "re-run with sudo to read the effective sshd setting (sudo sshd -T)"
  if ls /etc/ssh/sshd_config.d/*.conf >/dev/null 2>&1; then
    note "drop-ins present in /etc/ssh/sshd_config.d/:"
    grep -HiEn 'passwordauthentication' /etc/ssh/sshd_config.d/*.conf 2>/dev/null \
      | sed 's/^/    /' || note "  (no PasswordAuthentication lines found in drop-ins)"
  fi
fi

printf '\n'
if [[ "${problems}" -eq 0 ]]; then
  printf 'OK: key-authentication prerequisites look correct.\n'
  printf 'Verify from your workstation before disabling passwords:\n'
  printf '  ssh -o PreferredAuthentications=publickey -o PasswordAuthentication=no <pi-user>@<pi-host> "echo ok"\n'
else
  printf 'Found %d issue(s) above. Fix them before disabling password auth.\n' \
    "${problems}"
  exit 1
fi
