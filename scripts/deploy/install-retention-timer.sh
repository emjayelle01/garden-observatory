#!/usr/bin/env bash
#
# install-retention-timer.sh — install (and, only on explicit request, enable)
# the scheduled capture-media retention units. Task 14.5.
#
# Renders scripts/deploy/mgo-retention.service.template, takes
# scripts/deploy/mgo-retention.timer verbatim, validates both, and publishes
# the pair atomically into the systemd unit directory. It is deliberately a
# SEPARATE installer from install-service-identity.sh and from the deployment
# gateway installer: those own other authority boundaries (the runtime
# identity, and the privileged deployment path), and neither should acquire
# the power to schedule deletion as a side effect.
#
# Two properties are the whole point:
#
#   * INSTALLING NEVER ENABLES. Without --enable the timer is written and
#     nothing is scheduled. Enabling is a distinct, root-only commissioning
#     action, taken after the operator has reviewed the configuration.
#   * NOTHING ELSE IS TOUCHED. No application configuration, no database, no
#     media, no backup, no approval file, no repository, no other unit. The
#     only reason the API service is even named is the After= ordering.
#
# Usage (on the Raspberry Pi):
#   bash scripts/deploy/install-retention-timer.sh --dry-run     # any user
#   sudo bash scripts/deploy/install-retention-timer.sh          # install only
#   sudo bash scripts/deploy/install-retention-timer.sh --enable # + schedule
#
# Idempotent: re-running with identical inputs changes nothing and says so.
# If publishing the second unit fails after the first was published, the
# first is restored from the copy taken before publication (rollback), so the
# unit directory never holds a half-installed pair.
#
# Exit status: 0 success (including "already up to date"); 2 usage; 65 a
# precondition failed and nothing was written; 70 publication failed and the
# previous pair was restored; 71 enabling failed after a successful install.

set -euo pipefail

# --- defaults --------------------------------------------------------------

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
default_app_root="$(cd "${script_dir}/../.." && pwd)"

app_root="${default_app_root}"
service_user="mgo"
service_group="mgo"
config_path="/etc/garden-observatory/mgo.toml"
state_dir="/var/lib/garden-observatory"
database_dir="${state_dir}/db"
capture_dir="${state_dir}/media/captures"
backup_dir="/var/backups/garden-observatory"
unit_directory="/etc/systemd/system"
default_unit_directory="/etc/systemd/system"

service_unit="mgo-retention.service"
timer_unit="mgo-retention.timer"
service_template="${script_dir}/mgo-retention.service.template"
timer_source="${script_dir}/mgo-retention.timer"
timer_stamp="/var/lib/systemd/timers/stamp-${timer_unit}"

dry_run=0
enable_timer=0
# Validation aid: make the SECOND publication fail so the rollback of the
# first can be proved by the test suite. Refused with the default unit
# directory, so it can never be used against a real host's systemd directory.
fail_after_first_publish=0

EX_USAGE=2
EX_PRECONDITION=65
EX_PUBLISH=70
EX_ENABLE=71

usage() {
  cat <<'USAGE'
Usage: sudo bash scripts/deploy/install-retention-timer.sh [options]

Installs mgo-retention.service and mgo-retention.timer. Installing does NOT
enable or start the timer; pass --enable for that, deliberately and separately.

Options:
  --app-root PATH        Application checkout to run from
                         (default: the checkout containing this script)
  --user NAME            Runtime account name        (default: mgo)
  --group NAME           Runtime primary group       (default: mgo)
  --config PATH          Production configuration    (default: /etc/garden-observatory/mgo.toml)
  --database-dir PATH    Database directory          (default: /var/lib/garden-observatory/db)
  --capture-dir PATH     Capture media directory     (default: /var/lib/garden-observatory/media/captures)
  --backup-dir PATH      Backup root whose lock the retention run holds for
                         its duration, so it can never overlap a backup
                         (default: /var/backups/garden-observatory)
  --unit-directory PATH  Where to publish the units  (default: /etc/systemd/system)
                         A non-default value is a developer validation aid:
                         root is not required, ownership is not enforced,
                         and --enable is refused.
  --enable               After installing, seed the timer stamp, enable and
                         start mgo-retention.timer, and verify both states.
                         Root and systemctl are required. The retention
                         SERVICE is never started by this script.
  --dry-run              Render, validate and report; write nothing.
  -h, --help             Show this help
USAGE
}

# --- argument parsing ------------------------------------------------------

while [[ $# -gt 0 ]]; do
  case "$1" in
    --app-root)       app_root="$2"; shift 2 ;;
    --user)           service_user="$2"; shift 2 ;;
    --group)          service_group="$2"; shift 2 ;;
    --config)         config_path="$2"; shift 2 ;;
    --database-dir)   database_dir="$2"; shift 2 ;;
    --capture-dir)    capture_dir="$2"; shift 2 ;;
    --backup-dir)     backup_dir="$2"; shift 2 ;;
    --unit-directory) unit_directory="$2"; shift 2 ;;
    --enable)         enable_timer=1; shift ;;
    --dry-run)        dry_run=1; shift ;;
    --fail-after-first-publish) fail_after_first_publish=1; shift ;;
    -h|--help)        usage; exit 0 ;;
    *)
      printf 'error: unknown option: %s\n\n' "$1" >&2
      usage >&2
      exit "${EX_USAGE}"
      ;;
  esac
done

# --- helpers ---------------------------------------------------------------

note() { printf '  %s\n' "$*"; }
warn() { printf 'warning: %s\n' "$*" >&2; }
fail() { printf 'error: %s\n' "$1" >&2; exit "${2:-${EX_PRECONDITION}}"; }
step() { printf '\n== %s\n' "$*"; }

is_root() { [[ "$(id -u)" == "0" ]]; }

require_absolute() {
  # Absolute, and made only of characters that survive both the sed
  # renderer and a systemd directive value unchanged: no '|' (the sed
  # delimiter), no '&' (sed's "the match"), no backslash (a sed escape --
  # '\n' would become a real newline and a second directive), no newline, and
  # no whitespace (an unquoted ExecStart= argument would split on it).
  local label="$1" value="$2"
  [[ "${value}" == /* ]] || fail "${label} must be an absolute path."
  case "${value}" in
    *'|'*|*'&'*|*\\*|*[[:space:]]*) fail "${label} contains a character the unit renderer cannot carry." ;;
  esac
}

require_account_name() {
  # A plain account name and nothing else: it is substituted into User= and
  # Group=, so anything that is not a name is a directive injection.
  local label="$1" value="$2"
  [[ "${value}" =~ ^[A-Za-z_][A-Za-z0-9_.-]{0,31}$ ]] || fail "${label} must be a plain account name."
}

canonical_path() {
  # The physical, normalised form of a path that need not exist yet: symlinks
  # in existing components resolved, '.' and '..' folded, trailing separators
  # dropped. The default-directory decision is made on this form, so no
  # spelling of the real unit directory can pass as a developer directory.
  realpath -m -- "$1"
}

require_no_symlink_component() {
  # Every existing component of the destination path, as supplied, must be
  # a real directory, so a symlink cannot redirect the publication elsewhere.
  local path="$1" current="$1"
  while [[ -n "${current}" && "${current}" != "/" ]]; do
    [[ ! -L "${current}" ]] || fail "${current} is a symlink; refusing to publish through it."
    current="${current%/*}"
  done
  [[ -e "${path}" ]] || return 0
  [[ -d "${path}" ]] || fail "${path} is not a directory."
}

# --- preconditions ---------------------------------------------------------

step "Preconditions"

for value_label in "--app-root:${app_root}" "--config:${config_path}" \
  "--database-dir:${database_dir}" "--capture-dir:${capture_dir}" \
  "--backup-dir:${backup_dir}" "--unit-directory:${unit_directory}"; do
  require_absolute "${value_label%%:*}" "${value_label#*:}"
done
require_account_name "--user" "${service_user}"
require_account_name "--group" "${service_group}"

# The unit directory is compared in canonical form (Task 14.5A): a trailing
# separator, a '.' or '..' component, or a symlink to the real directory is
# still the real directory, and must get the real directory's rules --
# root required, ownership enforced, the validation aid refused.
command -v realpath >/dev/null 2>&1 || fail "realpath is required to validate --unit-directory."
require_no_symlink_component "${unit_directory}"
unit_directory="$(canonical_path "${unit_directory}")" || fail "--unit-directory could not be resolved."
canonical_default_unit_directory="$(canonical_path "${default_unit_directory}")" || canonical_default_unit_directory="${default_unit_directory}"

developer_directory=0
if [[ "${unit_directory}" != "${canonical_default_unit_directory}" ]]; then
  developer_directory=1
  note "non-default unit directory: ownership will not be enforced and --enable is refused"
fi

if (( enable_timer )) && (( developer_directory )); then
  fail "--enable is only valid with the default unit directory."
fi

if (( fail_after_first_publish )) && (( ! developer_directory )); then
  fail "--fail-after-first-publish is a validation aid and is refused with the default unit directory."
fi

if (( ! dry_run )) && (( ! developer_directory )) && ! is_root; then
  fail "installing into ${unit_directory} requires root (use --dry-run to preview)."
fi

if (( enable_timer )) && ! is_root; then
  fail "--enable requires root."
fi

[[ -f "${service_template}" ]] || fail "template not found: ${service_template}"
[[ -f "${timer_source}" ]] || fail "timer not found: ${timer_source}"
note "template: ${service_template}"
note "timer:    ${timer_source}"

entry_point="${app_root}/.venv/bin/mgo-retention"
if [[ -x "${entry_point}" ]]; then
  note "entry point present: ${entry_point}"
elif (( dry_run )); then
  warn "entry point ${entry_point} is absent or not executable; the rendered unit could not start on this host"
else
  fail "entry point ${entry_point} is absent or not executable; refusing to install a unit that cannot start."
fi

[[ -d "${unit_directory}" ]] || fail "unit directory does not exist: ${unit_directory}"

for existing in "${unit_directory}/${service_unit}" "${unit_directory}/${timer_unit}"; do
  if [[ -e "${existing}" ]]; then
    [[ ! -L "${existing}" ]] || fail "${existing} is a symlink; refusing to replace it."
    [[ -f "${existing}" ]] || fail "${existing} exists and is not a regular file."
  fi
done

# --- render ----------------------------------------------------------------

step "Render"

staging="$(mktemp -d /tmp/mgo-retention-timer-XXXXXXXX)" \
  || fail "could not create a private staging directory."
chmod 0700 "${staging}"
trap 'rm -rf "${staging}"' EXIT
mkdir -p "${staging}/previous"

rendered_service="${staging}/${service_unit}"
rendered_timer="${staging}/${timer_unit}"

sed \
  -e "s|@APP_ROOT@|${app_root}|g" \
  -e "s|@SERVICE_USER@|${service_user}|g" \
  -e "s|@SERVICE_GROUP@|${service_group}|g" \
  -e "s|@CONFIG_PATH@|${config_path}|g" \
  -e "s|@DATABASE_DIR@|${database_dir}|g" \
  -e "s|@CAPTURE_DIR@|${capture_dir}|g" \
  -e "s|@BACKUP_DIR@|${backup_dir}|g" \
  "${service_template}" > "${rendered_service}"
cp "${timer_source}" "${rendered_timer}"
chmod 0644 "${rendered_service}" "${rendered_timer}"

# --- validate --------------------------------------------------------------

step "Validate"

if grep -q '@[A-Z_]*@' "${rendered_service}"; then
  fail "the rendered service still contains an unsubstituted placeholder."
fi
note "every placeholder substituted"

validate_unit_structure() {
  local file="$1" kind="$2"
  grep -q '^\[Unit\]$' "${file}" || fail "${file}: missing [Unit] section."
  case "${kind}" in
    service)
      grep -q '^\[Service\]$' "${file}" || fail "${file}: missing [Service] section."
      grep -q '^Type=oneshot$' "${file}" || fail "${file}: the service must be Type=oneshot."
      grep -q "^User=${service_user}$" "${file}" || fail "${file}: the service must run as ${service_user}."
      grep -q '^ExecStart=/' "${file}" || fail "${file}: ExecStart must be an absolute executable path."
      grep -q 'scheduled-run --execute' "${file}" || fail "${file}: ExecStart must invoke scheduled-run --execute."
      grep -q '^NoNewPrivileges=yes$' "${file}" || fail "${file}: NoNewPrivileges=yes is required."
      grep -q '^ProtectSystem=strict$' "${file}" || fail "${file}: ProtectSystem=strict is required."
      grep -q '^ReadWritePaths=' "${file}" || fail "${file}: ReadWritePaths= is required."
      if grep -q '^\[Install\]$' "${file}"; then
        fail "${file}: the retention service must not carry an [Install] section; only the timer is enabled."
      fi
      ;;
    timer)
      grep -q '^\[Timer\]$' "${file}" || fail "${file}: missing [Timer] section."
      grep -q '^OnCalendar=' "${file}" || fail "${file}: OnCalendar= is required."
      grep -q '^Persistent=true$' "${file}" || fail "${file}: Persistent=true is required."
      grep -q "^Unit=${service_unit}$" "${file}" || fail "${file}: the timer must trigger ${service_unit}."
      grep -q '^WantedBy=timers.target$' "${file}" || fail "${file}: WantedBy=timers.target is required."
      ;;
  esac
  if grep -q $'\r' "${file}"; then
    fail "${file}: carriage returns would corrupt directive values."
  fi
}

validate_unit_structure "${rendered_service}" service
validate_unit_structure "${rendered_timer}" timer
note "structure and hardening directives present"

if command -v systemd-analyze >/dev/null 2>&1; then
  # Authoritative parse by systemd itself. Both files are in the staging
  # directory, so the timer's Unit= resolves to the rendered service.
  if diagnostics="$(systemd-analyze verify "${rendered_service}" "${rendered_timer}" 2>&1)"; then
    note "systemd-analyze verify passed"
  else
    printf '%s\n' "${diagnostics}" >&2
    fail "systemd-analyze verify rejected the rendered units."
  fi
else
  note "systemd-analyze is not available on this host; structural validation only (authoritative validation is a later on-host gate)"
fi

# --- compare with what is installed -----------------------------------------

step "Compare"

destination_service="${unit_directory}/${service_unit}"
destination_timer="${unit_directory}/${timer_unit}"

changed=0
for pair in "${rendered_service}:${destination_service}" "${rendered_timer}:${destination_timer}"; do
  source="${pair%%:*}"; destination="${pair#*:}"
  if [[ -f "${destination}" ]] && cmp -s "${source}" "${destination}"; then
    note "${destination} is already up to date"
  else
    changed=1
    if [[ -f "${destination}" ]]; then
      note "${destination} differs and would be replaced"
    else
      note "${destination} is absent and would be created"
    fi
  fi
done

if (( dry_run )); then
  step "Dry run"
  note "would publish ${destination_service} and ${destination_timer} (root:root 0644) atomically"
  if (( changed )); then
    note "would reload the systemd daemon"
  else
    note "nothing would change; no daemon reload"
  fi
  if (( enable_timer )); then
    note "would seed ${timer_stamp} if absent, enable and start ${timer_unit}, and verify both states"
  else
    note "would NOT enable or start ${timer_unit} (pass --enable to schedule it)"
  fi
  printf '\n--- rendered %s ---\n' "${service_unit}"
  cat "${rendered_service}"
  exit 0
fi

# --- publish atomically, with rollback ---------------------------------------

step "Publish"

publish_one() {
  # Publish ${1} to ${2}: stage a temporary in the destination directory (same
  # filesystem, so the final rename is atomic) and rename it over the target.
  local source="$1" destination="$2" temporary
  temporary="$(mktemp -p "${unit_directory}" ".${service_unit%.service}.XXXXXXXX")" || return 1
  if ! cp "${source}" "${temporary}"; then rm -f "${temporary}"; return 1; fi
  if is_root; then
    if ! chown root:root "${temporary}"; then rm -f "${temporary}"; return 1; fi
  fi
  if ! chmod 0644 "${temporary}"; then rm -f "${temporary}"; return 1; fi
  if ! mv -T -- "${temporary}" "${destination}"; then rm -f "${temporary}"; return 1; fi
  return 0
}

restore_one() {
  # Put back what was there before this run -- bytes and mode -- or remove
  # what this run created, so a rollback restores exact prior content or
  # exact prior absence.
  local destination="$1" previous="$2" previous_mode="$3"
  if [[ -f "${previous}" ]]; then
    publish_one "${previous}" "${destination}" || warn "could not restore ${destination}"
    if [[ -n "${previous_mode}" ]]; then
      chmod "${previous_mode}" "${destination}" || warn "could not restore the mode of ${destination}"
    fi
  else
    rm -f -- "${destination}" || warn "could not remove ${destination}"
  fi
}

if (( ! changed )); then
  note "both units are already up to date; nothing published"
else
  previous_service="${staging}/previous/${service_unit}"
  previous_timer="${staging}/previous/${timer_unit}"
  previous_service_mode=""
  if [[ -f "${destination_service}" ]]; then
    cp -p "${destination_service}" "${previous_service}"
    previous_service_mode="$(stat -c '%a' "${destination_service}")" || previous_service_mode=""
  fi
  [[ -f "${destination_timer}" ]] && cp -p "${destination_timer}" "${previous_timer}"

  if ! publish_one "${rendered_service}" "${destination_service}"; then
    fail "could not publish ${destination_service}; nothing was changed." "${EX_PUBLISH}"
  fi
  note "published ${destination_service}"

  if (( fail_after_first_publish )) || ! publish_one "${rendered_timer}" "${destination_timer}"; then
    warn "could not publish ${destination_timer}; rolling back ${destination_service}"
    restore_one "${destination_service}" "${previous_service}" "${previous_service_mode}"
    fail "publication failed; the previous pair was restored." "${EX_PUBLISH}"
  fi
  note "published ${destination_timer}"
fi

# --- verify what is installed -------------------------------------------------

step "Verify"

for pair in "${rendered_service}:${destination_service}" "${rendered_timer}:${destination_timer}"; do
  source="${pair%%:*}"; destination="${pair#*:}"
  [[ -f "${destination}" && ! -L "${destination}" ]] || fail "${destination} is not a regular file after publication." "${EX_PUBLISH}"
  cmp -s "${source}" "${destination}" || fail "${destination} does not match the validated bytes." "${EX_PUBLISH}"
  if is_root; then
    owner="$(stat -c '%U:%G' "${destination}")"
    [[ "${owner}" == "root:root" ]] || fail "${destination} is owned by ${owner}, expected root:root." "${EX_PUBLISH}"
  fi
  mode="$(stat -c '%a' "${destination}")"
  [[ "${mode}" == "644" ]] || fail "${destination} has mode ${mode}, expected 644." "${EX_PUBLISH}"
  note "${destination}: bytes, type and mode verified"
done

# --- daemon reload -------------------------------------------------------------

if (( changed )) && (( ! developer_directory )); then
  if command -v systemctl >/dev/null 2>&1; then
    systemctl daemon-reload || fail "daemon-reload failed; the units are installed but systemd has not re-read them." "${EX_PUBLISH}"
    note "reloaded the systemd daemon"
  else
    warn "systemctl is unavailable; the units are installed but the daemon was not reloaded"
  fi
fi

# --- enable (explicit only) ------------------------------------------------------

if (( enable_timer )); then  # the only path that schedules anything
  step "Enable"
  command -v systemctl >/dev/null 2>&1 \
    || fail "systemctl is unavailable, so ${timer_unit} cannot be enabled." "${EX_ENABLE}"

  # Seed the persistence stamp BEFORE enabling. Persistent=true makes systemd
  # catch up a run it believes was missed; with no stamp and today's 04:00
  # already past, enabling would fire retention immediately. Enabling a
  # schedule must never be the thing that starts a retention run.
  if [[ ! -e "${timer_stamp}" ]]; then
    install -d -o root -g root -m 0755 "$(dirname "${timer_stamp}")" \
      || fail "could not create $(dirname "${timer_stamp}")." "${EX_ENABLE}"
    : > "${timer_stamp}" || fail "could not write ${timer_stamp}." "${EX_ENABLE}"
    chown root:root "${timer_stamp}" && chmod 0644 "${timer_stamp}" \
      || fail "could not set ownership or mode on ${timer_stamp}." "${EX_ENABLE}"
    note "seeded ${timer_stamp} so enabling the timer triggers no catch-up run"
  else
    note "timer stamp already present — left unchanged"
  fi

  systemctl enable "${timer_unit}" >/dev/null \
    || fail "enable failed for ${timer_unit}; it is installed but NOT scheduled." "${EX_ENABLE}"
  systemctl start "${timer_unit}" >/dev/null \
    || fail "start failed for ${timer_unit}; it is enabled but NOT running now." "${EX_ENABLE}"
  systemctl is-enabled --quiet "${timer_unit}" \
    || fail "${timer_unit} does not report itself as enabled." "${EX_ENABLE}"
  systemctl is-active --quiet "${timer_unit}" \
    || fail "${timer_unit} does not report itself as active." "${EX_ENABLE}"
  note "${timer_unit} is enabled and active (the retention service itself was NOT run)"
else
  step "Not enabled"
  note "${timer_unit} was installed but NOT enabled or started; pass --enable to schedule it"
fi

step "Done"
note "retention deletes nothing unless the configuration at ${config_path} enables it with a bound"
