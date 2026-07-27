#!/usr/bin/env bash
#
# verify-service-identity.sh — READ-ONLY check of the MGO runtime identity.
#
# Confirms the dedicated service account, its groups, the persistent filesystem
# layout, the ownership/permission model and the systemd unit are all as the
# deployment expects. It changes nothing.
#
# Run on the Raspberry Pi:
#   bash scripts/deploy/verify-service-identity.sh
#
# Some probes (running a command as the runtime account) need root; without it
# they are reported as SKIP rather than failing. Exits non-zero if any check
# fails.

set -uo pipefail

service_user="mgo"
service_group="mgo"
camera_group="video"
service_unit="mgo.service"

config_dir="/etc/garden-observatory"
config_path="${config_dir}/mgo.toml"
state_dir="/var/lib/garden-observatory"
log_dir="/var/log/garden-observatory"
unit_path="/etc/systemd/system/${service_unit}"

state_subdirectories=(
  "${state_dir}/db"
  "${state_dir}/media"
  "${state_dir}/media/captures"
  "${state_dir}/queues"
  "${state_dir}/state"
)

failures=0

pass() { printf '  PASS  %s\n' "$*"; }
skip() { printf '  SKIP  %s\n' "$*"; }
fail() { printf '  FAIL  %s\n' "$*"; failures=$((failures + 1)); }
step() { printf '\n== %s\n' "$*"; }

if [[ "$(uname -s)" != "Linux" ]]; then
  printf 'This check applies to the Linux deployment host; nothing to verify on %s.\n' "$(uname -s)"
  exit 0
fi

is_root=0
[[ "$(id -u)" == "0" ]] && is_root=1

printf 'MGO service identity verification\n'

# --- account and groups ----------------------------------------------------

step "Service account"

if id -u "${service_user}" >/dev/null 2>&1; then
  pass "account '${service_user}' exists"

  shell="$(getent passwd "${service_user}" | cut -d: -f7)"
  case "${shell}" in
    */nologin|/bin/false) pass "login shell is non-login (${shell})" ;;
    *)                    fail "login shell is '${shell}' — must be nologin/false" ;;
  esac

  uid="$(id -u "${service_user}")"
  if (( uid < 1000 )); then
    pass "system account (uid ${uid})"
  else
    fail "uid ${uid} is in the regular-user range; expected a system account"
  fi

  primary="$(id -gn "${service_user}")"
  if [[ "${primary}" == "${service_group}" ]]; then
    pass "primary group is '${service_group}'"
  else
    fail "primary group is '${primary}'; expected '${service_group}'"
  fi

  memberships="$(id -nG "${service_user}" 2>/dev/null | tr ' ' '\n')"
  if grep -qx "${camera_group}" <<<"${memberships}"; then
    pass "member of '${camera_group}' (camera device access)"
  else
    fail "not a member of '${camera_group}' — the camera will be unreachable"
  fi

  # Least privilege: no administrative group memberships.
  for privileged in sudo adm root admin wheel; do
    if grep -qx "${privileged}" <<<"${memberships}"; then
      fail "member of privileged group '${privileged}' — the runtime identity must not be administrative"
    fi
  done

  password_field="$(getent shadow "${service_user}" 2>/dev/null | cut -d: -f2)"
  if [[ -z "${password_field}" ]]; then
    skip "password state (needs root to read /etc/shadow)"
  elif [[ "${password_field}" == "!"* || "${password_field}" == "*" ]]; then
    pass "password login is locked"
  else
    fail "account has a usable password — it must not be able to log in"
  fi
else
  fail "account '${service_user}' does not exist"
fi

# --- filesystem layout -----------------------------------------------------

step "Filesystem layout"

check_directory() {
  local path="$1" expect_owner="$2" expect_group="$3" expect_mode="$4"
  if [[ ! -d "${path}" ]]; then
    fail "${path} is missing"
    return
  fi
  local owner group mode
  owner="$(stat -c '%U' "${path}")"
  group="$(stat -c '%G' "${path}")"
  mode="$(stat -c '%a' "${path}")"
  # Ignore the setgid bit when comparing; only the access bits matter here.
  mode="${mode: -3}"
  if [[ "${owner}:${group}" == "${expect_owner}:${expect_group}" && "${mode}" == "${expect_mode}" ]]; then
    pass "${path}  ${owner}:${group}  ${mode}"
  else
    fail "${path} is ${owner}:${group} ${mode}; expected ${expect_owner}:${expect_group} ${expect_mode}"
  fi
  if [[ "${mode: -1}" =~ [2367] ]]; then
    fail "${path} is world-writable"
  fi
}

check_directory "${config_dir}" root "${service_group}" 750
check_directory "${state_dir}" "${service_user}" "${service_group}" 750
for directory in "${state_subdirectories[@]}"; do
  check_directory "${directory}" "${service_user}" "${service_group}" 750
done
check_directory "${log_dir}" "${service_user}" "${service_group}" 750

if [[ -f "${config_path}" ]]; then
  owner="$(stat -c '%U:%G' "${config_path}")"
  mode="$(stat -c '%a' "${config_path}")"
  if [[ "${owner}" == "root:${service_group}" && "${mode}" == "640" ]]; then
    pass "${config_path}  ${owner}  ${mode} (readable, not writable, by the service)"
  else
    fail "${config_path} is ${owner} ${mode}; expected root:${service_group} 640"
  fi
else
  fail "${config_path} is missing"
fi

# --- systemd unit ----------------------------------------------------------

step "systemd unit"

if [[ -f "${unit_path}" ]]; then
  pass "${unit_path} exists"

  unit_value() { grep -m1 "^$1=" "${unit_path}" | cut -d= -f2- ; }

  for directive in User:"${service_user}" Group:"${service_group}"; do
    key="${directive%%:*}"
    want="${directive#*:}"
    got="$(unit_value "${key}")"
    if [[ "${got}" == "${want}" ]]; then
      pass "${key}=${got}"
    else
      fail "${key}=${got:-<unset>}; expected ${want}"
    fi
  done

  if [[ "$(unit_value SupplementaryGroups)" == *"${camera_group}"* ]]; then
    pass "SupplementaryGroups includes ${camera_group}"
  else
    fail "SupplementaryGroups does not include ${camera_group}"
  fi

  if grep -q '^CapabilityBoundingSet=$' "${unit_path}"; then
    pass "CapabilityBoundingSet is empty (no Linux capabilities)"
  else
    fail "CapabilityBoundingSet is not empty"
  fi

  if [[ "$(unit_value NoNewPrivileges)" == "yes" ]]; then
    pass "NoNewPrivileges=yes"
  else
    fail "NoNewPrivileges is not enabled"
  fi

  if grep -q "^Environment=MGO_CONFIG_PATH=${config_path}$" "${unit_path}"; then
    pass "MGO_CONFIG_PATH=${config_path}"
  else
    fail "the unit does not point MGO_CONFIG_PATH at ${config_path}"
  fi
else
  fail "${unit_path} is missing"
fi

# --- effective access ------------------------------------------------------

step "Effective access"

if (( ! is_root )); then
  skip "runtime-account access probes (re-run with sudo to include them)"
elif ! command -v runuser >/dev/null 2>&1; then
  skip "runtime-account access probes ('runuser' unavailable)"
else
  for directory in "${state_dir}" "${state_subdirectories[@]}" "${log_dir}"; do
    if runuser -u "${service_user}" -- test -w "${directory}" 2>/dev/null; then
      pass "writable by '${service_user}': ${directory}"
    else
      fail "not writable by '${service_user}': ${directory}"
    fi
  done

  if [[ -f "${config_path}" ]]; then
    if runuser -u "${service_user}" -- test -r "${config_path}" 2>/dev/null; then
      pass "readable by '${service_user}': ${config_path}"
    else
      fail "not readable by '${service_user}': ${config_path}"
    fi
    if runuser -u "${service_user}" -- test -w "${config_path}" 2>/dev/null; then
      fail "${config_path} is WRITABLE by the service — it must be read-only to the runtime identity"
    else
      pass "not writable by '${service_user}': ${config_path}"
    fi
  fi
fi

# --- service state ---------------------------------------------------------

step "Service state"

if command -v systemctl >/dev/null 2>&1; then
  if systemctl is-active --quiet "${service_unit}"; then
    running_user="$(systemctl show -p User --value "${service_unit}")"
    if [[ "${running_user}" == "${service_user}" ]]; then
      pass "${service_unit} is active and configured to run as '${service_user}'"
    else
      fail "${service_unit} is active but runs as '${running_user:-root}'"
    fi
    main_pid="$(systemctl show -p MainPID --value "${service_unit}")"
    if [[ -n "${main_pid}" && "${main_pid}" != "0" && -r "/proc/${main_pid}" ]]; then
      actual="$(stat -c '%U' "/proc/${main_pid}")"
      if [[ "${actual}" == "${service_user}" ]]; then
        pass "main process ${main_pid} is owned by '${actual}'"
      else
        fail "main process ${main_pid} is owned by '${actual}'; expected '${service_user}'"
      fi
    fi
  else
    skip "${service_unit} is not active"
  fi
else
  skip "systemctl unavailable"
fi

# --- result ----------------------------------------------------------------

printf '\n'
if (( failures == 0 )); then
  printf 'Service identity verification PASSED.\n'
  exit 0
fi

printf 'Service identity verification FAILED (%d problem(s)).\n' "${failures}"
exit 1
