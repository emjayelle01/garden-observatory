#!/usr/bin/env bash
#
# install-mgo-validate.sh — install the deployment gateway and its sudoers rule.
#
# Run as root on the Raspberry Pi:
#   sudo bash scripts/deploy/install-mgo-validate.sh
#   sudo bash scripts/deploy/install-mgo-validate.sh --dry-run
#
# Installs two files:
#   scripts/deploy/mgo-validate         -> /usr/local/sbin/mgo-validate  0755
#   scripts/deploy/mgo-validate.sudoers -> /etc/sudoers.d/mgo-validate   0440
#
# This installer provisions the *deployment control plane* only. It never
# deploys application code, never touches the approval file, never runs Git,
# never changes a checkout and never restarts or reloads a service. Deploying
# code is what the installed gateway's deploy-main action is for, and
# provisioning the service identity is what install-service-identity.sh is for.
# Keeping the three apart is the point of this task: one verb, one meaning.
#
# Both files are validated before either is installed, and both are installed
# atomically by rename. If the second install fails, the first is rolled back,
# so the host is never left with a gateway that has no sudoers rule or a
# sudoers rule pointing at a gateway that is not there.

set -Eeuo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

readonly GATEWAY_SOURCE="${script_dir}/mgo-validate"
readonly SUDOERS_SOURCE="${script_dir}/mgo-validate.sudoers"
readonly GATEWAY_TARGET="/usr/local/sbin/mgo-validate"
readonly SUDOERS_TARGET="/etc/sudoers.d/mgo-validate"
readonly GATEWAY_MODE="0755"
readonly SUDOERS_MODE="0440"

dry_run=0

die() {
    printf 'install-mgo-validate: %s\n' "$*" >&2
    exit 1
}

log() {
    printf 'install-mgo-validate: %s\n' "$*"
}

usage() {
    cat <<'USAGE'
Usage: install-mgo-validate.sh [--dry-run]

  --dry-run   Validate everything and report what would change. Installs
              nothing, requires no privilege, and leaves the host untouched.
USAGE
}

while [[ "$#" -gt 0 ]]; do
    case "$1" in
        --dry-run)
            dry_run=1
            shift
            ;;
        -h | --help)
            usage
            exit 0
            ;;
        *)
            usage >&2
            die "unsupported argument"
            ;;
    esac
done

# --- privilege -------------------------------------------------------------
# A dry run reads tracked files and reports; it needs nothing. A real install
# writes to /usr/local/sbin and /etc/sudoers.d, so it needs root.

if [[ "$dry_run" -eq 0 && "${EUID}" -ne 0 ]]; then
    die "installation requires root; re-run with sudo, or use --dry-run"
fi

# --- source assets ---------------------------------------------------------

[[ -f "$GATEWAY_SOURCE" ]] || die "gateway source is missing"
[[ -f "$SUDOERS_SOURCE" ]] || die "sudoers source is missing"

# --- validation, before anything is installed ------------------------------
# Order matters. An invalid sudoers file that reached /etc/sudoers.d can lock
# every account out of sudo on the host, so it is validated before the gateway
# is installed, not after.

log "checking gateway syntax"
bash -n "$GATEWAY_SOURCE" \
    || die "the gateway failed its shell syntax check"

log "checking sudoers syntax"
if command -v visudo >/dev/null 2>&1; then
    visudo -cf "$SUDOERS_SOURCE" >/dev/null \
        || die "the sudoers policy failed validation"
elif [[ "$dry_run" -eq 1 ]]; then
    # A dry run installs nothing, so a host without visudo is a reportable gap
    # rather than a hazard — and saying so is more useful than refusing to
    # report at all. The real install below still refuses outright.
    log "visudo is not available here; the policy was NOT validated"
else
    die "visudo is not available; refusing to install an unvalidated policy"
fi

# --- current state ---------------------------------------------------------

file_checksum() {
    local path="$1"
    [[ -f "$path" ]] || return 1
    sha256sum "$path" | awk '{ print $1 }'
}

gateway_source_sum="$(file_checksum "$GATEWAY_SOURCE")"
sudoers_source_sum="$(file_checksum "$SUDOERS_SOURCE")"
gateway_target_sum="$(file_checksum "$GATEWAY_TARGET" || true)"
sudoers_target_sum="$(file_checksum "$SUDOERS_TARGET" || true)"

# Plain if-statements rather than `test && assign`: under `set -e` a bare test
# that fails is the last command on the line, and the script would exit on the
# perfectly ordinary case of a file not yet being installed.
gateway_current=0
sudoers_current=0
verify_only=0

if [[ "$gateway_source_sum" == "${gateway_target_sum:-}" ]]; then
    gateway_current=1
fi
if [[ "$sudoers_source_sum" == "${sudoers_target_sum:-}" ]]; then
    sudoers_current=1
fi
if [[ "$gateway_current" -eq 1 && "$sudoers_current" -eq 1 ]]; then
    verify_only=1
    log "gateway and sudoers policy are already installed and identical"
fi

if [[ "$dry_run" -eq 1 ]]; then
    if [[ "$gateway_current" -eq 1 ]]; then
        log "dry run: $GATEWAY_TARGET is current"
    else
        log "dry run: would install $GATEWAY_TARGET ($GATEWAY_MODE root:root)"
    fi
    if [[ "$sudoers_current" -eq 1 ]]; then
        log "dry run: $SUDOERS_TARGET is current"
    else
        log "dry run: would install $SUDOERS_TARGET ($SUDOERS_MODE root:root)"
    fi
    log "dry run complete; nothing was changed"
    exit 0
fi

# --- installation ----------------------------------------------------------
# install(1) writes to a temporary name and renames, so a reader never sees a
# half-written file and an interrupted run cannot leave a truncated gateway
# that sudo would happily execute.

install_file() {
    local source="$1"
    local target="$2"
    local mode="$3"
    local directory
    local temporary

    directory="$(dirname "$target")"
    [[ -d "$directory" ]] || install -d -o root -g root -m 0755 "$directory"

    temporary="$(mktemp "${target}.XXXXXX")"
    install -o root -g root -m "$mode" "$source" "$temporary"
    mv -f "$temporary" "$target"
}

restore_previous() {
    local target="$1"
    local backup="$2"

    if [[ -n "$backup" && -f "$backup" ]]; then
        mv -f "$backup" "$target"
    else
        rm -f "$target"
    fi
}

gateway_backup=""
if [[ -f "$GATEWAY_TARGET" ]]; then
    gateway_backup="$(mktemp "${GATEWAY_TARGET}.previous.XXXXXX")"
    cp -p "$GATEWAY_TARGET" "$gateway_backup"
fi

log "installing $GATEWAY_TARGET"
install_file "$GATEWAY_SOURCE" "$GATEWAY_TARGET" "$GATEWAY_MODE"

log "installing $SUDOERS_TARGET"
if ! install_file "$SUDOERS_SOURCE" "$SUDOERS_TARGET" "$SUDOERS_MODE"; then
    # The gateway is already in place and the policy is not. Undo the gateway
    # rather than leave a half-installed control plane behind.
    restore_previous "$GATEWAY_TARGET" "$gateway_backup"
    die "the sudoers policy could not be installed; the gateway was rolled back"
fi

if [[ -n "$gateway_backup" ]]; then
    rm -f "$gateway_backup"
fi

# --- verification ----------------------------------------------------------
# Verify what is on disk rather than trusting that the writes above succeeded.

verify_installed() {
    local target="$1"
    local expected_sum="$2"
    local expected_mode="$3"
    local actual_sum
    local actual_mode
    local actual_owner

    actual_sum="$(file_checksum "$target")" \
        || die "$target was not installed"
    [[ "$actual_sum" == "$expected_sum" ]] \
        || die "$target does not match its source"

    actual_mode="$(stat -c '%a' "$target")"
    [[ "$actual_mode" == "${expected_mode#0}" || \
        "$actual_mode" == "$expected_mode" ]] \
        || die "$target has the wrong mode"

    actual_owner="$(stat -c '%U:%G' "$target")"
    [[ "$actual_owner" == "root:root" ]] \
        || die "$target has the wrong owner"
}

verify_installed "$GATEWAY_TARGET" "$gateway_source_sum" "$GATEWAY_MODE"
verify_installed "$SUDOERS_TARGET" "$sudoers_source_sum" "$SUDOERS_MODE"

if [[ "$verify_only" -eq 1 ]]; then
    log "verified; nothing changed"
else
    log "installed and verified"
fi

log "the approval file, the production repository and mgo.service were not touched"
