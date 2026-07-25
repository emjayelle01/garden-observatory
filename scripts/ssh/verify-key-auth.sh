#!/usr/bin/env bash
#
# verify-key-auth.sh — check that SSH key-authentication prerequisites are in
# place on the Raspberry Pi. Read-only: it changes nothing.
#
# Run as your normal login user on the Pi:
#   bash scripts/ssh/verify-key-auth.sh
#
# Success means key-based login is ready to use. Password authentication is a
# deliberate fallback on this trusted LAN, so its state is shown for information
# only and is never treated as a problem.

set -euo pipefail

ssh_dir="${HOME}/.ssh"
auth_keys="${ssh_dir}/authorized_keys"
problems=0

ok()   { printf '  - %s\n' "$1"; }
bad()  { printf '  ! %s\n' "$1"; problems=$((problems + 1)); }
info() { printf '  · %s\n' "$1"; }

printf 'MGO SSH key-authentication check for user "%s" on "%s"\n\n' \
  "$(whoami)" "$(hostname)"

# --- ~/.ssh directory ------------------------------------------------------
printf 'Key prerequisites:\n'
if [[ -d "${ssh_dir}" ]]; then
  dir_perm="$(stat -c '%a' "${ssh_dir}")"
  if [[ "${dir_perm}" == "700" ]]; then
    ok "~/.ssh exists with permissions 700"
  else
    bad "~/.ssh should be 700 but is ${dir_perm} (fix: chmod 700 ~/.ssh)"
  fi
else
  bad "~/.ssh does not exist yet (install your public key — see docs/Remote-Access.md)"
fi

# --- authorized_keys presence, permissions and content ---------------------
if [[ -f "${auth_keys}" ]]; then
  key_perm="$(stat -c '%a' "${auth_keys}")"
  if [[ "${key_perm}" == "600" ]]; then
    ok "authorized_keys exists with permissions 600"
  else
    bad "authorized_keys should be 600 but is ${key_perm} (fix: chmod 600 ${auth_keys})"
  fi
  key_count="$(grep -cvE '^\s*(#|$)' "${auth_keys}" || true)"
  if [[ "${key_count}" -gt 0 ]]; then
    ok "${key_count} usable public key(s) installed"
  else
    bad "authorized_keys is empty (install your public key)"
  fi
else
  bad "no authorized_keys file (install your public key — see docs/Remote-Access.md)"
fi

# --- informational: current password-auth setting -------------------------
# Password authentication is intentionally kept enabled as a fallback on the
# trusted LAN. This is shown for information only; it is never a failure.
printf '\nPassword authentication (informational only — a deliberate fallback):\n'
if [[ "$(id -u)" -eq 0 ]] && command -v sshd >/dev/null 2>&1; then
  pw="$(sshd -T 2>/dev/null | awk '/^passwordauthentication/ {print $2}')"
  info "PasswordAuthentication ${pw:-unknown} (from sshd -T)"
else
  info "run 'sudo sshd -T | grep -i passwordauthentication' to see the effective value"
fi

printf '\n'
if [[ "${problems}" -eq 0 ]]; then
  printf 'OK: key-based login prerequisites are valid.\n'
  printf 'Prove key-only login from your workstation (no password fallback):\n'
  printf '  ssh -o PreferredAuthentications=publickey -o PasswordAuthentication=no <pi-user>@<pi-host> "echo key auth OK"\n'
else
  printf 'Found %d issue(s) above with the key setup. Password login still works meanwhile.\n' \
    "${problems}"
  exit 1
fi
