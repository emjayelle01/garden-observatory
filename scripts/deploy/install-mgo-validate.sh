#!/usr/bin/env bash
#
# install-mgo-validate.sh — install the deployment gateway and its sudoers rule.
#
# Run as root on the Raspberry Pi:
#   sudo bash scripts/deploy/install-mgo-validate.sh
#   bash scripts/deploy/install-mgo-validate.sh --dry-run
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
# Transaction shape:
#
#   * both sources are validated before either target is touched;
#   * a fully current installation is verified and exits before any temporary
#     file is created — nothing correct is rewritten;
#   * both previous states are recorded first, and "absent" is a state;
#   * every mutating command checks its own status. Nothing here relies on
#     errexit inside a function called as an `if !` condition, because Bash
#     disables errexit for everything such a function runs;
#   * any failure after the first mutation restores BOTH previous states;
#   * the installed policy is re-validated with visudo before success;
#   * a failed restoration exits distinctly and never claims the host is clean.
#
# Like the gateway, this file is a library when sourced and a program when
# executed, so the transaction above can be exercised against temporary targets
# without root (see tests/test_deployment_gateway.py).

set -Eeuo pipefail

readonly GATEWAY_TARGET="/usr/local/sbin/mgo-validate"
readonly SUDOERS_TARGET="/etc/sudoers.d/mgo-validate"
readonly GATEWAY_MODE="0755"
readonly SUDOERS_MODE="0440"

readonly EX_FAILED=1
readonly EX_RESTORE_FAILED=78

log() {
    printf 'install-mgo-validate: %s\n' "$*"
}

warn() {
    printf 'install-mgo-validate: %s\n' "$*" >&2
}

die() {
    printf 'install-mgo-validate: %s\n' "$*" >&2
    exit "$EX_FAILED"
}

usage() {
    cat <<'USAGE'
Usage: install-mgo-validate.sh [--dry-run]

  --dry-run   Validate everything and report what would change. Installs
              nothing and leaves the host untouched. Validation is not
              skipped: a dry run on a host without visudo fails, because it
              cannot honour the promise to validate everything.
USAGE
}

# --- inspection ------------------------------------------------------------

file_checksum() {
    local path="$1"
    [[ -f "$path" ]] || return 1
    sha256sum "$path" | awk '{ print $1 }'
}

file_metadata() {
    local path="$1"
    [[ -e "$path" ]] || return 1
    stat -c '%U:%G:%a' "$path"
}

# Content *and* metadata. Matching bytes with the wrong owner or mode is not an
# installed gateway, it is a security defect wearing one, and it is repaired
# transactionally rather than reported as current.
target_is_current() {
    local target="$1"
    local expected_sum="$2"
    local expected_mode="$3"
    local actual_sum
    local actual_meta

    actual_sum="$(file_checksum "$target")" || return 1
    [[ "$actual_sum" == "$expected_sum" ]] || return 1
    actual_meta="$(file_metadata "$target")" || return 1
    [[ "$actual_meta" == "root:root:${expected_mode#0}" ]]
}

policy_is_valid() {
    local path="$1"
    [[ -f "$path" ]] || return 1
    visudo -cf "$path" >/dev/null 2>&1
}

# --- mutating primitives ---------------------------------------------------

# Write to a temporary name in the target directory, then rename.
#
# Every step checks its own status and returns immediately. This function is
# invoked as an `if !` condition, where Bash disables errexit for its internal
# commands, so an unchecked failure would fall through to the rename and
# publish a truncated file that sudo would happily execute.
install_file() {
    local source="$1"
    local target="$2"
    local mode="$3"
    local directory
    local temporary

    directory="$(dirname "$target")"
    if [[ ! -d "$directory" ]]; then
        install -d -o root -g root -m 0755 "$directory" || return 1
    fi

    temporary="$(mktemp "${target}.XXXXXX")" || return 1

    if ! install -o root -g root -m "$mode" "$source" "$temporary"; then
        rm -f "$temporary"
        return 1
    fi

    if ! mv -f "$temporary" "$target"; then
        rm -f "$temporary"
        return 1
    fi
}

# Record a target's previous state, printing the backup path.
#
# An empty result means "this target did not exist", which is a state like any
# other: restoring it means removing whatever we installed.
record_previous() {
    local target="$1"
    local backup

    if [[ -f "$target" ]]; then
        backup="$(mktemp "${target}.previous.XXXXXX")" || return 1
        if ! cp -p "$target" "$backup"; then
            rm -f "$backup"
            return 1
        fi
        printf '%s\n' "$backup"
        return 0
    fi
    printf '\n'
}

restore_one() {
    local target="$1"
    local backup="$2"

    if [[ -n "$backup" ]]; then
        [[ -f "$backup" ]] || return 1
        mv -f "$backup" "$target" || return 1
        return 0
    fi
    rm -f "$target" || return 1
}

# Both targets, always. A restoration that put one back is not a restoration.
restore_pair() {
    local gateway_target="$1"
    local gateway_backup="$2"
    local sudoers_target="$3"
    local sudoers_backup="$4"
    local failures=0

    restore_one "$gateway_target" "$gateway_backup" || failures=$((failures + 1))
    restore_one "$sudoers_target" "$sudoers_backup" || failures=$((failures + 1))
    [[ "$failures" -eq 0 ]]
}

verify_installed() {
    local target="$1"
    local expected_sum="$2"
    local expected_mode="$3"

    target_is_current "$target" "$expected_sum" "$expected_mode"
}

# --- the transaction -------------------------------------------------------
#
# Every path is a parameter so the whole transaction can be executed against
# temporary targets. Production values are passed by main() and appear nowhere
# else; there is no environment override.
#
# Returns 0 on success, EX_FAILED when the installation failed and both
# previous states were restored, and EX_RESTORE_FAILED when restoration itself
# failed.
install_pair() {
    local gateway_source="$1"
    local gateway_target="$2"
    local gateway_mode="$3"
    local sudoers_source="$4"
    local sudoers_target="$5"
    local sudoers_mode="$6"
    local gateway_sum
    local sudoers_sum
    local gateway_backup
    local sudoers_backup

    gateway_sum="$(file_checksum "$gateway_source")" || return "$EX_FAILED"
    sudoers_sum="$(file_checksum "$sudoers_source")" || return "$EX_FAILED"

    gateway_backup="$(record_previous "$gateway_target")" || {
        warn "the existing gateway could not be recorded"
        return "$EX_FAILED"
    }
    sudoers_backup="$(record_previous "$sudoers_target")" || {
        warn "the existing sudoers policy could not be recorded"
        # Nothing has been mutated yet; drop the gateway backup and stop.
        [[ -n "$gateway_backup" ]] && rm -f "$gateway_backup"
        return "$EX_FAILED"
    }

    # --- from here, any failure restores both targets ---
    log "installing $gateway_target"
    if ! install_file "$gateway_source" "$gateway_target" "$gateway_mode"; then
        abort_pair "the gateway could not be installed" \
            "$gateway_target" "$gateway_backup" \
            "$sudoers_target" "$sudoers_backup"
        return "$?"
    fi

    log "installing $sudoers_target"
    if ! install_file "$sudoers_source" "$sudoers_target" "$sudoers_mode"; then
        abort_pair "the sudoers policy could not be installed" \
            "$gateway_target" "$gateway_backup" \
            "$sudoers_target" "$sudoers_backup"
        return "$?"
    fi

    if ! verify_installed "$gateway_target" "$gateway_sum" "$gateway_mode"; then
        abort_pair "the installed gateway does not match its source" \
            "$gateway_target" "$gateway_backup" \
            "$sudoers_target" "$sudoers_backup"
        return "$?"
    fi

    if ! verify_installed "$sudoers_target" "$sudoers_sum" "$sudoers_mode"; then
        abort_pair "the installed sudoers policy does not match its source" \
            "$gateway_target" "$gateway_backup" \
            "$sudoers_target" "$sudoers_backup"
        return "$?"
    fi

    # Validate what actually landed, not only what was about to be written.
    if ! policy_is_valid "$sudoers_target"; then
        abort_pair "the installed sudoers policy failed validation" \
            "$gateway_target" "$gateway_backup" \
            "$sudoers_target" "$sudoers_backup"
        return "$?"
    fi

    [[ -n "$gateway_backup" ]] && rm -f "$gateway_backup"
    [[ -n "$sudoers_backup" ]] && rm -f "$sudoers_backup"
    return 0
}

abort_pair() {
    local reason="$1"
    shift

    warn "$reason"
    if restore_pair "$@"; then
        warn "installation failed; both targets were restored"
        return "$EX_FAILED"
    fi
    warn "installation failed AND restoration failed; the host was NOT restored"
    return "$EX_RESTORE_FAILED"
}

# --- entry point -----------------------------------------------------------

# The whole installation, with every path as a parameter.
#
# main() supplies the fixed production constants; nothing here is reachable
# from the command line, which accepts only --dry-run and --help. Taking the
# paths and the effective UID as arguments is what lets the real transaction —
# including each failure and restoration path — be executed against temporary
# targets, since EUID is readonly in Bash and /etc is not a test fixture.
run_installation() {
    local gateway_source="$1"
    local gateway_target="$2"
    local gateway_mode="$3"
    local sudoers_source="$4"
    local sudoers_target="$5"
    local sudoers_mode="$6"
    local dry_run="$7"
    local effective_uid="$8"
    local gateway_sum
    local sudoers_sum
    local gateway_current=0
    local sudoers_current=0
    local outcome

    if [[ "$dry_run" -eq 0 && "$effective_uid" -ne 0 ]]; then
        warn "installation requires root; re-run with sudo, or use --dry-run"
        return "$EX_FAILED"
    fi

    [[ -f "$gateway_source" ]] || { warn "gateway source is missing"; return "$EX_FAILED"; }
    [[ -f "$sudoers_source" ]] || { warn "sudoers source is missing"; return "$EX_FAILED"; }

    # An invalid file under /etc/sudoers.d can lock every account out of sudo,
    # so both sources are validated before either target is touched — and a dry
    # run validates too, because validating everything is what it promises. A
    # host without visudo therefore fails in both modes rather than reporting a
    # success it did not earn.
    log "checking gateway syntax"
    if ! bash -n "$gateway_source"; then
        warn "the gateway failed its shell syntax check"
        return "$EX_FAILED"
    fi

    log "checking sudoers syntax"
    if ! command -v visudo >/dev/null 2>&1; then
        warn "visudo is not available; refusing to proceed without validation"
        return "$EX_FAILED"
    fi
    if ! visudo -cf "$sudoers_source" >/dev/null; then
        warn "the sudoers policy failed validation"
        return "$EX_FAILED"
    fi

    gateway_sum="$(file_checksum "$gateway_source")" || return "$EX_FAILED"
    sudoers_sum="$(file_checksum "$sudoers_source")" || return "$EX_FAILED"

    if target_is_current "$gateway_target" "$gateway_sum" "$gateway_mode"; then
        gateway_current=1
    fi
    if target_is_current "$sudoers_target" "$sudoers_sum" "$sudoers_mode" \
        && policy_is_valid "$sudoers_target"; then
        sudoers_current=1
    fi

    if [[ "$dry_run" -eq 1 ]]; then
        if [[ "$gateway_current" -eq 1 ]]; then
            log "dry run: $gateway_target is current"
        else
            log "dry run: would install $gateway_target ($gateway_mode root:root)"
        fi
        if [[ "$sudoers_current" -eq 1 ]]; then
            log "dry run: $sudoers_target is current"
        else
            log "dry run: would install $sudoers_target ($sudoers_mode root:root)"
        fi
        log "dry run complete; nothing was changed"
        return 0
    fi

    # Verified and finished before a temporary file exists. An installer that
    # rewrites files that are already correct changes their inode and
    # modification time for nothing, and cannot be run safely on a whim.
    if [[ "$gateway_current" -eq 1 && "$sudoers_current" -eq 1 ]]; then
        log "gateway and sudoers policy are already installed, correct and valid"
        log "verified; nothing changed"
        return 0
    fi

    outcome=0
    install_pair \
        "$gateway_source" "$gateway_target" "$gateway_mode" \
        "$sudoers_source" "$sudoers_target" "$sudoers_mode" || outcome="$?"
    [[ "$outcome" -eq 0 ]] || return "$outcome"

    log "installed and verified"
    log "the approval file, the production repository and mgo.service were not touched"
}

main() {
    local script_dir
    local dry_run=0
    local outcome=0

    script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

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

    run_installation \
        "${script_dir}/mgo-validate" "$GATEWAY_TARGET" "$GATEWAY_MODE" \
        "${script_dir}/mgo-validate.sudoers" "$SUDOERS_TARGET" "$SUDOERS_MODE" \
        "$dry_run" "$EUID" || outcome="$?"

    exit "$outcome"
}

# Program when executed, library when sourced.
if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then
    main "$@"
fi
