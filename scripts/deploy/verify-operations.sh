#!/usr/bin/env bash
#
# verify-operations.sh — READ-ONLY check of the MGO operations provisioning.
#
# Confirms the backup directory, the backup service and timer, and the log
# rotation policy are installed as the deployment expects. It changes nothing:
# it takes no backup, rotates no log, starts no unit, enables nothing and
# repairs nothing.
#
# This is the operations counterpart to verify-service-identity.sh, which
# remains the authority on the runtime account, the filesystem layout and
# mgo.service. Run both:
#
#   bash scripts/deploy/verify-service-identity.sh
#   bash scripts/deploy/verify-operations.sh
#
# Some probes need root; without it they are reported as SKIP rather than
# failing. Exits non-zero if any check fails.

set -uo pipefail

service_user="mgo"
service_group="mgo"
service_unit="mgo.service"
backup_unit="mgo-backup.service"
backup_timer="mgo-backup.timer"

config_path="/etc/garden-observatory/mgo.toml"
state_dir="/var/lib/garden-observatory"
database_dir="${state_dir}/db"
log_dir="/var/log/garden-observatory"
backup_dir="/var/backups/garden-observatory"

backup_unit_path="/etc/systemd/system/${backup_unit}"
backup_timer_path="/etc/systemd/system/${backup_timer}"
logrotate_path="/etc/logrotate.d/garden-observatory"

failures=0

pass() { printf '  PASS  %s\n' "$*"; }
skip() { printf '  SKIP  %s\n' "$*"; }
fail() { printf '  FAIL  %s\n' "$*"; failures=$((failures + 1)); }
step() { printf '\n== %s\n' "$*"; }

if [[ "$(uname -s)" != "Linux" ]]; then
  printf 'This check applies to the Linux deployment host; nothing to verify on %s.\n' "$(uname -s)"
  exit 0
fi

printf 'MGO operations verification\n'

# --- backup directory ------------------------------------------------------

step "Backup directory"

if [[ -d "${backup_dir}" ]]; then
  owner="$(stat -c '%U:%G' "${backup_dir}")"
  mode="$(stat -c '%a' "${backup_dir}")"
  mode="${mode: -3}"
  if [[ "${owner}" == "${service_user}:${service_group}" && "${mode}" == "750" ]]; then
    pass "${backup_dir}  ${owner}  ${mode}"
  else
    fail "${backup_dir} is ${owner} ${mode}; expected ${service_user}:${service_group} 750"
  fi
  if [[ "${mode: -1}" =~ [2367] ]]; then
    fail "${backup_dir} is world-writable"
  fi

  # The backup root must not sit inside the tree it protects.
  case "${backup_dir}" in
    "${state_dir}"|"${state_dir}"/*)
      fail "${backup_dir} is inside ${state_dir}; backups must not live in the tree they protect" ;;
    *)
      pass "the backup root is outside ${state_dir}" ;;
  esac

  world_readable="$(find "${backup_dir}" -maxdepth 1 -type f -perm -o=r 2>/dev/null | head -n 5)"
  if [[ -n "${world_readable}" ]]; then
    fail "world-readable file(s) in ${backup_dir}: ${world_readable}"
  else
    pass "no world-readable file in ${backup_dir}"
  fi
else
  fail "${backup_dir} is missing"
fi

# --- backup service --------------------------------------------------------

step "Backup service"

unit_value() { grep -m1 "^$1=" "$2" | cut -d= -f2- ; }

if [[ -f "${backup_unit_path}" ]]; then
  pass "${backup_unit_path} exists"

  [[ "$(unit_value Type "${backup_unit_path}")" == "oneshot" ]] \
    && pass "Type=oneshot" \
    || fail "Type is not oneshot — a scheduled backup must not be a long-running service"

  for directive in User:"${service_user}" Group:"${service_group}"; do
    key="${directive%%:*}"
    want="${directive#*:}"
    got="$(unit_value "${key}" "${backup_unit_path}")"
    if [[ "${got}" == "${want}" ]]; then
      pass "${key}=${got}"
    else
      fail "${key}=${got:-<unset>}; expected ${want}"
    fi
  done

  grep -q "^User=root$" "${backup_unit_path}" \
    && fail "the backup service runs as root" \
    || pass "the backup service does not run as root"

  grep -q "^Environment=MGO_CONFIG_PATH=${config_path}$" "${backup_unit_path}" \
    && pass "MGO_CONFIG_PATH=${config_path}" \
    || fail "the backup unit does not point MGO_CONFIG_PATH at ${config_path}"

  grep -q "^CapabilityBoundingSet=$" "${backup_unit_path}" \
    && pass "CapabilityBoundingSet is empty (no Linux capabilities)" \
    || fail "CapabilityBoundingSet is not empty"

  [[ "$(unit_value NoNewPrivileges "${backup_unit_path}")" == "yes" ]] \
    && pass "NoNewPrivileges=yes" \
    || fail "NoNewPrivileges is not enabled"

  [[ "$(unit_value ProtectSystem "${backup_unit_path}")" == "strict" ]] \
    && pass "ProtectSystem=strict" \
    || fail "ProtectSystem is not strict"

  [[ -n "$(unit_value TimeoutStartSec "${backup_unit_path}")" ]] \
    && pass "TimeoutStartSec bounds the run" \
    || fail "the backup unit has no run-time bound"

  writable="$(unit_value ReadWritePaths "${backup_unit_path}")"
  if [[ "${writable}" == *"${backup_dir}"* ]]; then
    pass "ReadWritePaths includes ${backup_dir}"
  else
    fail "ReadWritePaths does not include ${backup_dir}"
  fi
  # Required for SQLite WAL shared-memory access, even for a read-only reader.
  if [[ "${writable}" == *"${database_dir}"* ]]; then
    pass "ReadWritePaths includes ${database_dir} (needed for WAL shared memory)"
  else
    fail "ReadWritePaths does not include ${database_dir}; a WAL read will fail"
  fi

  # The backup must never manage the API.
  for forbidden in "systemctl" "ExecStartPre=.*mgo.service" "Requires=${service_unit}"; do
    if grep -Eq "^[^#]*${forbidden}" "${backup_unit_path}"; then
      fail "the backup unit references '${forbidden}' — it must not manage the API"
    fi
  done
  pass "the backup unit does not start, stop or require ${service_unit}"
else
  fail "${backup_unit_path} is missing"
fi

# --- backup timer ----------------------------------------------------------

step "Backup timer"

if [[ -f "${backup_timer_path}" ]]; then
  pass "${backup_timer_path} exists"

  grep -q "^Persistent=true$" "${backup_timer_path}" \
    && pass "Persistent=true (a missed backup is caught up after downtime)" \
    || fail "Persistent is not true — backups would be skipped silently while the Pi is off"

  schedule="$(grep -m1 '^OnCalendar=' "${backup_timer_path}" | cut -d= -f2-)"
  if [[ -n "${schedule}" ]]; then
    pass "OnCalendar=${schedule}"
  else
    fail "the timer has no OnCalendar schedule"
  fi

  delay="$(grep -m1 '^RandomizedDelaySec=' "${backup_timer_path}" | cut -d= -f2-)"
  [[ -n "${delay}" ]] \
    && pass "RandomizedDelaySec=${delay}" \
    || fail "the timer has no randomised delay"

  [[ "$(grep -m1 '^Unit=' "${backup_timer_path}" | cut -d= -f2-)" == "${backup_unit}" ]] \
    && pass "the timer triggers ${backup_unit}" \
    || fail "the timer does not trigger ${backup_unit}"
else
  fail "${backup_timer_path} is missing"
fi

# --- timer state -----------------------------------------------------------

step "Timer state"

if command -v systemctl >/dev/null 2>&1; then
  if systemctl is-enabled --quiet "${backup_timer}" 2>/dev/null; then
    pass "${backup_timer} is enabled (it will survive a reboot)"
  else
    fail "${backup_timer} is not enabled"
  fi

  if systemctl is-active --quiet "${backup_timer}" 2>/dev/null; then
    pass "${backup_timer} is active"
  else
    fail "${backup_timer} is not active"
  fi

  next="$(systemctl show -p NextElapseUSecRealtime --value "${backup_timer}" 2>/dev/null)"
  if [[ -n "${next}" && "${next}" != "0" && "${next}" != "n/a" ]]; then
    pass "next scheduled run: ${next}"
  else
    skip "next scheduled run is not reported by this systemd version"
  fi

  # The backup service itself must be inert between runs.
  backup_state="$(systemctl show -p ActiveState --value "${backup_unit}" 2>/dev/null)"
  case "${backup_state}" in
    inactive|failed|activating|active|"")
      pass "${backup_unit} ActiveState=${backup_state:-unknown}" ;;
    *)
      fail "${backup_unit} is in an unexpected state: ${backup_state}" ;;
  esac

  if [[ "${backup_state}" == "failed" ]]; then
    fail "the last scheduled backup FAILED — inspect: journalctl -u ${backup_unit}"
  fi
else
  skip "systemctl unavailable"
fi

# --- log rotation ----------------------------------------------------------

step "Log rotation"

if [[ -f "${logrotate_path}" ]]; then
  pass "${logrotate_path} exists"

  owner="$(stat -c '%U:%G' "${logrotate_path}")"
  mode="$(stat -c '%a' "${logrotate_path}")"
  if [[ "${owner}" == "root:root" && "${mode}" == "644" ]]; then
    pass "${logrotate_path}  ${owner}  ${mode}"
  else
    fail "${logrotate_path} is ${owner} ${mode}; expected root:root 644"
  fi

  # The glob must be confined to the MGO log directory.
  targets="$(grep -oE '^[[:space:]]*/[^ {]+' "${logrotate_path}" | tr -d '[:blank:]')"
  if [[ -z "${targets}" ]]; then
    fail "the policy names no rotation target"
  else
    for target in ${targets}; do
      case "${target}" in
        "${log_dir}"/*.log) pass "rotation target is confined: ${target}" ;;
        *)                  fail "unsafe rotation target: ${target}" ;;
      esac
    done
  fi

  # It must never be pointed at data.
  for forbidden in "${state_dir}" "/var/lib" "/var/backups" ".db" "captures"; do
    if grep -q -- "${forbidden}" "${logrotate_path}"; then
      # The explanatory comment block legitimately mentions these paths; only a
      # rotation TARGET line is a problem.
      if grep -E '^[[:space:]]*/' "${logrotate_path}" | grep -q -- "${forbidden}"; then
        fail "the policy targets ${forbidden} — it must only rotate MGO logs"
      fi
    fi
  done
  pass "the policy targets no database, backup or media path"

  grep -qE '^[[:space:]]*rotate[[:space:]]+[0-9]+' "${logrotate_path}" \
    && pass "retention is bounded ($(grep -m1 -oE 'rotate[[:space:]]+[0-9]+' "${logrotate_path}"))" \
    || fail "retention is not bounded"

  grep -qE '^[[:space:]]*create[[:space:]]+0640' "${logrotate_path}" \
    && pass "rotated files are created 0640 (never world-readable)" \
    || fail "the policy does not create rotated files with a secure mode"

  grep -qE "^[[:space:]]*su[[:space:]]+${service_user}[[:space:]]+${service_group}" "${logrotate_path}" \
    && pass "su ${service_user} ${service_group}" \
    || fail "the policy does not rotate as ${service_user}:${service_group}"

  # logrotate ships in /usr/sbin on Debian, and /usr/sbin is NOT on an
  # unprivileged account's PATH. "command -v logrotate" therefore fails on a
  # host where logrotate is installed and working perfectly well -- which is
  # exactly what happened during Pi validation, where this check reported
  # "logrotate is not installed" about the very binary the installer had just
  # used successfully. Search PATH first, then the canonical administrative
  # locations. The caller's PATH is never modified and no root is needed.
  # >>> logrotate-discovery >>>
  logrotate_bin=""
  for candidate in \
    "$(command -v logrotate 2>/dev/null || true)" \
    "/usr/sbin/logrotate" \
    "/sbin/logrotate"
  do
    if [[ -n "${candidate}" && -x "${candidate}" ]]; then
      logrotate_bin="${candidate}"
      break
    fi
  done

  if [[ -n "${logrotate_bin}" ]]; then
    if "${logrotate_bin}" --debug "${logrotate_path}" >/dev/null 2>&1; then
      pass "the installed policy parses cleanly (${logrotate_bin})"
    else
      fail "logrotate cannot parse ${logrotate_path}"
    fi
  else
    # Deliberately a statement about DISCOVERY, not about the package: this
    # check cannot tell an uninstalled logrotate from one it failed to find.
    skip "no logrotate executable found in PATH, /usr/sbin or /sbin"
  fi
  # <<< logrotate-discovery <<<
else
  fail "${logrotate_path} is missing"
fi

# --- journald --------------------------------------------------------------
#
# Reported for information only. Task 10 deliberately does not change the
# host's global journald retention: it governs every service on the machine.

step "Journal (information only)"

if command -v journalctl >/dev/null 2>&1; then
  usage="$(journalctl --disk-usage 2>/dev/null)"
  [[ -n "${usage}" ]] && printf '  INFO  %s\n' "${usage}"
  printf '  INFO  MGO runtime logs live in the journal, not in %s\n' "${log_dir}"
  printf '  INFO  host retention: /etc/systemd/journald.conf (SystemMaxUse=)\n'
else
  skip "journalctl unavailable"
fi

# --- API untouched ---------------------------------------------------------
#
# A light cross-check only. verify-service-identity.sh remains the authority.

step "API service (cross-check)"

if command -v systemctl >/dev/null 2>&1; then
  if systemctl is-active --quiet "${service_unit}"; then
    running_user="$(systemctl show -p User --value "${service_unit}")"
    if [[ "${running_user}" == "${service_user}" ]]; then
      pass "${service_unit} is still active as '${service_user}'"
    else
      fail "${service_unit} runs as '${running_user:-root}'"
    fi
  else
    skip "${service_unit} is not active"
  fi
  printf '  INFO  run verify-service-identity.sh for the full identity check\n'
else
  skip "systemctl unavailable"
fi

# --- result ----------------------------------------------------------------

printf '\n'
if (( failures == 0 )); then
  printf 'Operations verification PASSED.\n'
  exit 0
fi

printf 'Operations verification FAILED (%d problem(s)).\n' "${failures}"
exit 1
