#!/usr/bin/env bash
#
# install-service-identity.sh — provision the MGO production runtime identity.
#
# Creates the dedicated non-login service account, its runtime group, the
# persistent filesystem layout under /etc, /var/lib and /var/log, and renders
# the systemd unit so the service runs under that identity instead of a human
# operator's account.
#
# Run on the Raspberry Pi as root:
#   sudo bash scripts/deploy/install-service-identity.sh
#
# Idempotent: re-running it re-asserts ownership and permissions, never
# recreates an existing account, and never overwrites an existing production
# configuration file. It only ever ADDS the minimum group access required; it
# never widens permissions to "other" and never makes anything world-writable.
#
# It refuses to install the systemd unit when the checkout's virtual environment
# belongs to a different directory (the usual result of relocating a checkout),
# because such a unit could never start. It never recreates the environment
# itself — "uv sync" must run as the administrative user.
#
# It does NOT start or restart the service — the operator does that explicitly
# once the configuration has been reviewed.

set -euo pipefail

# --- defaults --------------------------------------------------------------

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
default_app_root="$(cd "${script_dir}/../.." && pwd)"

app_root="${default_app_root}"
service_user="mgo"
service_group="mgo"
camera_group="video"
service_unit="mgo.service"

config_dir="/etc/garden-observatory"
config_path="${config_dir}/mgo.toml"
state_dir="/var/lib/garden-observatory"
log_dir="/var/log/garden-observatory"

bind_host="0.0.0.0"
bind_port="8080"

unit_template="${script_dir}/mgo.service.template"
unit_destination="/etc/systemd/system/${service_unit}"
config_example="${app_root}/config/mgo.production.example.toml"

install_unit=1
dry_run=0

# Every persistent data directory, parents first.
state_subdirectories=(
  "${state_dir}/db"
  "${state_dir}/media"
  "${state_dir}/media/captures"
  "${state_dir}/queues"
  "${state_dir}/state"
)

usage() {
  cat <<'USAGE'
Usage: sudo bash scripts/deploy/install-service-identity.sh [options]

Options:
  --app-root PATH     Application checkout to run from
                      (default: the checkout containing this script)
  --user NAME         Runtime account name       (default: mgo)
  --group NAME        Runtime primary group      (default: mgo)
  --camera-group NAME Supplementary camera group (default: video)
  --host ADDRESS      Bind address for the unit  (default: 0.0.0.0)
  --port PORT         Bind port for the unit     (default: 8080)
  --no-unit           Provision identity and directories only; do not touch
                      /etc/systemd/system/mgo.service
  --dry-run           Print what would change without changing anything
  -h, --help          Show this help
USAGE
}

# --- argument parsing ------------------------------------------------------

while [[ $# -gt 0 ]]; do
  case "$1" in
    --app-root)     app_root="$2"; shift 2 ;;
    --user)         service_user="$2"; shift 2 ;;
    --group)        service_group="$2"; shift 2 ;;
    --camera-group) camera_group="$2"; shift 2 ;;
    --host)         bind_host="$2"; shift 2 ;;
    --port)         bind_port="$2"; shift 2 ;;
    --no-unit)      install_unit=0; shift ;;
    --dry-run)      dry_run=1; shift ;;
    -h|--help)      usage; exit 0 ;;
    *)              printf 'Unknown option: %s\n\n' "$1" >&2; usage >&2; exit 2 ;;
  esac
done

# Re-derive paths that depend on a caller-supplied --app-root.
config_example="${app_root}/config/mgo.production.example.toml"

# --- helpers ---------------------------------------------------------------

note()  { printf '  %s\n' "$*"; }
step()  { printf '\n== %s\n' "$*"; }
warn()  { printf 'WARNING: %s\n' "$*" >&2; }
fail()  { printf 'ERROR: %s\n' "$*" >&2; exit 1; }

# Run a mutating command, or describe it under --dry-run.
run() {
  if (( dry_run )); then
    printf '  [dry-run] %s\n' "$*"
  else
    "$@"
  fi
}

# Run a command as the runtime account. Used for read-only access probes.
as_service_user() {
  runuser -u "${service_user}" -- "$@"
}

# --- preconditions ---------------------------------------------------------

[[ "$(uname -s)" == "Linux" ]] \
  || fail "This script provisions a Linux service identity; it cannot run on $(uname -s)."

if (( ! dry_run )) && [[ "$(id -u)" != "0" ]]; then
  fail "Must be run as root: sudo bash ${BASH_SOURCE[0]}"
fi

[[ -d "${app_root}" ]] || fail "Application root does not exist: ${app_root}"
app_root="$(cd "${app_root}" && pwd)"

if (( install_unit )); then
  [[ -f "${unit_template}" ]] || fail "Unit template not found: ${unit_template}"
fi

printf 'MGO service identity provisioning\n'
note "application root : ${app_root}"
note "runtime account  : ${service_user}:${service_group} (+${camera_group})"
note "configuration    : ${config_path}"
note "persistent data  : ${state_dir}"
(( dry_run )) && note "mode             : DRY RUN (no changes)"

# --- 1. runtime group ------------------------------------------------------

step "Runtime group"

if getent group "${service_group}" >/dev/null; then
  note "group '${service_group}' already exists"
else
  run groupadd --system "${service_group}"
  note "created system group '${service_group}'"
fi

if ! getent group "${camera_group}" >/dev/null; then
  warn "group '${camera_group}' does not exist on this machine; the runtime account will have no camera device access until it does."
fi

# --- 2. service account ----------------------------------------------------

step "Service account"

# A non-login shell is mandatory: the runtime identity must never be usable for
# interactive or SSH access.
if [[ -x /usr/sbin/nologin ]]; then
  nologin_shell="/usr/sbin/nologin"
elif [[ -x /sbin/nologin ]]; then
  nologin_shell="/sbin/nologin"
else
  nologin_shell="/bin/false"
fi

if id -u "${service_user}" >/dev/null 2>&1; then
  note "account '${service_user}' already exists"
  current_shell="$(getent passwd "${service_user}" | cut -d: -f7)"
  if [[ "${current_shell}" != "${nologin_shell}" && "${current_shell}" != "/bin/false" ]]; then
    warn "account '${service_user}' has login shell '${current_shell}'; resetting to '${nologin_shell}'"
    run usermod --shell "${nologin_shell}" "${service_user}"
  else
    note "login shell is '${current_shell}' (non-login)"
  fi
else
  run useradd \
    --system \
    --gid "${service_group}" \
    --home-dir "${state_dir}" \
    --no-create-home \
    --shell "${nologin_shell}" \
    --comment "Matt's Garden Observatory service account" \
    "${service_user}"
  note "created system account '${service_user}' with shell '${nologin_shell}'"
fi

# No password may ever be usable for this account. Some shadow-utils versions
# report an error when the password is already locked, which is not a failure.
if run usermod --lock "${service_user}"; then
  note "password authentication locked"
else
  note "password authentication already locked"
fi

# Camera device access is the only supplementary privilege the account needs.
if getent group "${camera_group}" >/dev/null; then
  if id -nG "${service_user}" 2>/dev/null | tr ' ' '\n' | grep -qx "${camera_group}"; then
    note "already a member of '${camera_group}'"
  else
    run usermod --append --groups "${camera_group}" "${service_user}"
    note "added to supplementary group '${camera_group}' (camera devices)"
  fi
fi

# --- 3. configuration directory --------------------------------------------

step "Configuration directory"

# root-owned and group-readable: the service reads its configuration and can
# never rewrite it.
run install -d -o root -g "${service_group}" -m 0750 "${config_dir}"
note "${config_dir}  root:${service_group}  0750"

if [[ -f "${config_path}" ]]; then
  run chown root:"${service_group}" "${config_path}"
  run chmod 0640 "${config_path}"
  note "${config_path} already exists — left unchanged (ownership/mode re-asserted)"
elif [[ -f "${config_example}" ]]; then
  run install -o root -g "${service_group}" -m 0640 "${config_example}" "${config_path}"
  note "seeded ${config_path} from the production example"
  warn "review ${config_path} before starting the service (hosts, thresholds, camera enablement)."
else
  warn "no configuration at ${config_path} and no example at ${config_example}; create the file before starting the service."
fi

# --- 4. persistent data directories ----------------------------------------

step "Persistent data directories"

run install -d -o "${service_user}" -g "${service_group}" -m 0750 "${state_dir}"
note "${state_dir}  ${service_user}:${service_group}  0750"

for directory in "${state_subdirectories[@]}"; do
  run install -d -o "${service_user}" -g "${service_group}" -m 0750 "${directory}"
  note "${directory}"
done

run install -d -o "${service_user}" -g "${service_group}" -m 0750 "${log_dir}"
note "${log_dir}  ${service_user}:${service_group}  0750"

# --- 5. application checkout access ----------------------------------------

step "Application checkout access"

# The runtime account needs to READ and EXECUTE the code, never to modify it.
# Group ownership plus g+rX gives exactly that; the setgid bit keeps newly
# created files in the runtime group after a git pull or uv sync.
run chgrp -R "${service_group}" "${app_root}"
run chmod -R g+rX "${app_root}"
run find "${app_root}" -type d -exec chmod g+s {} +
note "${app_root} is group-readable by '${service_group}' (read/execute only, never writable)"

# --- 6. virtual environment -------------------------------------------------

step "Virtual environment"

# A Python virtual environment is LOCATION-DEPENDENT: every launcher in
# .venv/bin hard-codes the absolute path of the interpreter that created it.
# Moving or copying a checkout (for example from a home directory to /opt)
# leaves those launchers pointing at the old path, and systemd then fails the
# service with status=203/EXEC. Detect that here rather than installing a unit
# that cannot possibly start.
#
# The block between the markers below is self-contained: it reads only
# ``venv_dir`` and ``venv_launcher`` and sets ``venv_problem`` to a description
# of the first problem found (empty when the environment is usable). Keeping it
# free of side effects is what lets the test suite execute the real logic.

venv_dir="${app_root}/.venv"
venv_launcher="${venv_dir}/bin/uvicorn"
interpreter=""

# >>> venv-detection >>>
venv_problem=""

if [[ ! -d "${venv_dir}" ]]; then
  venv_problem="no virtual environment at ${venv_dir}"
elif [[ ! -f "${venv_launcher}" ]]; then
  venv_problem="no launcher at ${venv_launcher}"
elif [[ ! -x "${venv_launcher}" ]]; then
  venv_problem="${venv_launcher} is not executable"
else
  venv_shebang="$(head -n 1 "${venv_launcher}")"
  # Strip the "#!" and take the first field: a shebang may carry an argument,
  # as in "#!/usr/bin/env python".
  interpreter="${venv_shebang#\#!}"
  interpreter="${interpreter%% *}"

  if [[ -z "${interpreter}" || "${interpreter}" != /* ]]; then
    venv_problem="${venv_launcher} has no absolute interpreter in its shebang: ${venv_shebang}"
  elif [[ "${interpreter}" != "${venv_dir}/"* ]]; then
    # The relocation failure: the launcher still refers to another checkout.
    venv_problem="${venv_launcher} belongs to another checkout — its interpreter is ${interpreter}, outside ${venv_dir}"
  elif [[ ! -x "${interpreter}" ]]; then
    venv_problem="${venv_launcher} refers to a missing or non-executable interpreter: ${interpreter}"
  fi
fi
# <<< venv-detection <<<

if [[ -n "${venv_problem}" ]]; then
  printf '\n'
  warn "${venv_problem}"
  cat >&2 <<EOF

         A Python virtual environment is LOCATION-DEPENDENT. Every launcher in
         .venv/bin hard-codes the absolute path of the interpreter that created
         it, and moving or copying a checkout does NOT update them. systemd then
         fails to start the service with status=203/EXEC.

         Recreate the environment as the administrative user (NOT as root, so
         the checkout keeps its ownership):

           cd ${app_root}
           rm -rf .venv
           uv sync

         Then re-run this script.

EOF

  # Refuse to install a unit that cannot start. The environment is never
  # recreated automatically: "uv sync" must run as the administrative user, and
  # silently deleting a virtual environment from a root-run installer would be
  # a surprising, unrequested action.
  if (( install_unit )); then
    fail "refusing to install ${service_unit} against an unusable virtual environment."
  fi

  warn "continuing only because --no-unit was given: no systemd unit will be pointed at this virtual environment."
else
  note "${venv_dir} belongs to this checkout"
  note "launcher interpreter: ${interpreter}"
fi

# --- 7. access verification -------------------------------------------------

step "Access verification"

access_ok=1

if (( dry_run )); then
  note "skipped under --dry-run"
elif ! command -v runuser >/dev/null 2>&1; then
  warn "'runuser' is unavailable; skipping runtime-account access probes."
else
  # Every ancestor of the application root must be traversable by the runtime
  # account. A checkout inside a private home directory is the usual blocker.
  ancestors=()
  probe="${app_root}"
  while [[ "${probe}" != "/" ]]; do
    ancestors=("${probe}" "${ancestors[@]}")
    probe="$(dirname "${probe}")"
  done
  ancestors=("/" "${ancestors[@]}")

  for directory in "${ancestors[@]}"; do
    if ! as_service_user test -x "${directory}" 2>/dev/null; then
      warn "'${service_user}' cannot traverse ${directory} — the service will fail to start."
      printf '         Remedy: move the checkout to a service-owned location, e.g.\n' >&2
      printf '           sudo mkdir -p /opt && sudo mv %s /opt/garden-observatory\n' "${app_root}" >&2
      printf '           sudo bash /opt/garden-observatory/scripts/deploy/install-service-identity.sh\n' >&2
      access_ok=0
      break
    fi
  done

  if (( access_ok )); then
    note "'${service_user}' can traverse every parent of ${app_root}"
    if [[ -x "${venv_launcher}" ]] && ! as_service_user test -x "${venv_launcher}" 2>/dev/null; then
      warn "'${service_user}' cannot execute ${venv_launcher}"
      access_ok=0
    fi
  fi

  for directory in "${state_dir}" "${state_subdirectories[@]}" "${log_dir}"; do
    if ! as_service_user test -w "${directory}" 2>/dev/null; then
      warn "'${service_user}' cannot write ${directory}"
      access_ok=0
    fi
  done

  if [[ -f "${config_path}" ]] && ! as_service_user test -r "${config_path}" 2>/dev/null; then
    warn "'${service_user}' cannot read ${config_path}"
    access_ok=0
  fi

  (( access_ok )) && note "runtime account has exactly the access it needs"
fi

# --- 8. legacy data notice --------------------------------------------------

legacy_database="${app_root}/data/mgo.db"
if [[ -f "${legacy_database}" && ! -f "${state_dir}/db/mgo.db" ]]; then
  step "Existing data"
  warn "an earlier in-checkout database exists at ${legacy_database} but ${state_dir}/db/mgo.db does not."
  printf '         Copy it across BEFORE starting the service if you want to keep the\n' >&2
  printf '         existing observation timeline and capture catalogue:\n' >&2
  printf '           sudo cp -a %s* %s/db/\n' "${legacy_database}" "${state_dir}" >&2
  printf '           sudo cp -a %s/data/captures/. %s/media/captures/\n' "${app_root}" "${state_dir}" >&2
  printf '           sudo chown -R %s:%s %s\n' "${service_user}" "${service_group}" "${state_dir}" >&2
fi

# --- 9. systemd unit --------------------------------------------------------

if (( install_unit )); then
  step "systemd unit"

  if [[ -f "${unit_destination}" ]]; then
    backup="${unit_destination}.bak-$(date +%Y%m%d%H%M%S)"
    run cp -a "${unit_destination}" "${backup}"
    note "backed up the existing unit to ${backup}"
  fi

  rendered="$(
    sed \
      -e "s|@APP_ROOT@|${app_root}|g" \
      -e "s|@SERVICE_USER@|${service_user}|g" \
      -e "s|@SERVICE_GROUP@|${service_group}|g" \
      -e "s|@CAMERA_GROUP@|${camera_group}|g" \
      -e "s|@CONFIG_PATH@|${config_path}|g" \
      -e "s|@HOST@|${bind_host}|g" \
      -e "s|@PORT@|${bind_port}|g" \
      "${unit_template}"
  )"

  if (( dry_run )); then
    printf '  [dry-run] would write %s:\n\n' "${unit_destination}"
    printf '%s\n' "${rendered}"
  else
    printf '%s\n' "${rendered}" > "${unit_destination}"
    chown root:root "${unit_destination}"
    chmod 0644 "${unit_destination}"
    note "wrote ${unit_destination} (User=${service_user}, Group=${service_group})"
    systemctl daemon-reload
    note "reloaded the systemd daemon"
    systemctl enable "${service_unit}" >/dev/null 2>&1 || true
    note "enabled ${service_unit}"
  fi
else
  step "systemd unit"
  note "skipped (--no-unit)"
fi

# --- summary ---------------------------------------------------------------

printf '\n== Next steps\n'
note "1. Review ${config_path}"
note "2. sudo systemctl restart ${service_unit}"
note "3. bash ${app_root}/scripts/deploy/verify-service-identity.sh"

if (( ! access_ok )); then
  printf '\n'
  fail "provisioning completed with access problems (see the warnings above); fix them before starting the service."
fi

printf '\nService identity provisioning complete.\n'
