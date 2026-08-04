#!/bin/bash -p
#
# install-mgo-validate.sh — install the deployment gateway and its sudoers rule.
#
# The interpreter is fixed, not resolved through the environment: `/usr/bin/env
# bash` would search an inherited PATH for something called bash before this
# file could sanitise anything. `-p` is privileged mode, which stops Bash
# reading $BASH_ENV and $ENV, importing exported shell functions, and honouring
# SHELLOPTS, BASHOPTS, CDPATH and GLOBIGNORE during its own startup — none of
# which any statement in this file could undo afterwards.
#
# Always **executed directly**, so the shebang above is the interpreter that
# runs. There are three distinct invocations and they do not mean the same
# thing:
#
#   1. Unprivileged staging/source validation — checks the sources this
#      checkout is about to install, on any host, without privilege:
#        ./scripts/deploy/install-mgo-validate.sh --dry-run
#
#   2. Authoritative root pre-installation validation — the same checks with
#      the privilege needed to see the installed targets as they really are:
#        sudo ./scripts/deploy/install-mgo-validate.sh --dry-run
#
#   3. Installation, on the Raspberry Pi, as root:
#        sudo ./scripts/deploy/install-mgo-validate.sh
#
# (1) is not a substitute for (2). The installed targets are root-owned, and
# /etc/sudoers.d is not readable by an ordinary account: an unprivileged dry
# run cannot fully inspect them, so it cannot tell a target that is already
# current from one it merely could not read, and it cannot re-validate the
# installed policy with visudo. It reports on the sources and on as much of the
# host as the caller may see. Only the root dry run (2) is evidence about what
# an installation would actually do here.
#
# Never `sudo bash scripts/deploy/install-mgo-validate.sh`: naming the
# interpreter on the command line discards the shebang, and with it privileged
# mode.
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
#   * both staged source snapshots are validated before either installed
#     target is touched;
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
readonly EX_STALE=65
readonly EX_BUSY=75
readonly EX_RESTORE_FAILED=78

# An internal signal, never a process exit status: install_pair uses it to say
# "both targets were already correct and I changed nothing", which run_installation
# reports as success. It exists because the currency decision now has to happen
# *after* the locked snapshot — the checksums it compares against are the staged
# ones — and so can no longer be made by the caller beforehand.
readonly EX_CURRENT=64

# The same construction the gateway performs, for the same reasons: this script
# runs stat, install, mktemp, cp, mv, sha256sum, visudo and bash, and every one
# of them reads the environment. Correctness must not depend on the host's
# optional sudo `env_reset` or `secure_path` settings being configured.
readonly SAFE_PATH="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
readonly ROOT_HOME="/root"
readonly ENV_COMMAND="/usr/bin/env"
readonly PERMITTED_ENVIRONMENT="HOME LC_ALL MGO_INSTALL_ENVIRONMENT_CONSTRUCTED OLDPWD PATH PWD SHLVL SUDO_USER _"

# The same control-plane lock the gateway takes. Installing replaces the very
# file a running deploy-main is executing from, so the two must exclude each
# other. Descriptor 9, matching the gateway, held for the whole run.
readonly LOCK_FILE="/run/lock/mgo-deployment.lock"
readonly LOCK_FD=9

# Backups live here, not beside the target. A backup written into
# /etc/sudoers.d is a second policy file in a directory sudo includes, and
# renaming it back later is not worth that risk. Root-owned, 0700, on tmpfs.
#
# This is the *parent*. Each run creates its own uniquely named workspace
# inside it and removes only that workspace, so a run can never delete
# another's evidence, and anything left behind is attributable rather than
# anonymous.
readonly TRANSACTION_DIRECTORY="/run/mgo-validate-install"

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
Usage: sudo ./install-mgo-validate.sh [--dry-run]

Run it directly, as above. Naming the interpreter on the command line
discards the shebang, and with it the privileged mode that stops Bash
reading BASH_ENV and importing exported shell functions before this
script's first statement.

  --dry-run   Validate everything and report what would change. Installs
              nothing and leaves the host untouched. Validation is not
              skipped: a dry run on a host without visudo fails, because it
              cannot honour the promise to validate everything. The exit
              status agrees with the report — a state the real installation
              would refuse exits non-zero here too, with the same code.
              It holds no lock, so it describes a point-in-time view of the
              sources and claims no exclusion against a concurrent deployment.
USAGE
}

# --- execution environment -------------------------------------------------

# The three fixed values, and nothing else exported.
#
# An allowlist, because the property is "nothing else is here". A denylist can
# only ever say "not these", which is the weaker statement this installer used
# to rely on through sudo's optional env_reset.
environment_is_constructed() {
    local name

    [[ "${PATH:-}" == "$SAFE_PATH" ]] || return 1
    [[ "${HOME:-}" == "$ROOT_HOME" ]] || return 1
    [[ "${LC_ALL:-}" == "C" ]] || return 1

    while read -r name; do
        [[ -n "$name" ]] || continue
        case " $PERMITTED_ENVIRONMENT " in
            *" $name "*) ;;
            *) return 1 ;;
        esac
    done < <(compgen -e || true)
    return 0
}

purge_environment() {
    local name
    local failures=0

    while read -r name; do
        [[ -n "$name" ]] || continue
        case " $PERMITTED_ENVIRONMENT " in
            *" $name "*) continue ;;
        esac
        unset "$name" 2>/dev/null || failures=$((failures + 1))
    done < <(compgen -e || true)
    [[ "$failures" -eq 0 ]]
}

# Re-execute in a constructed environment, then make it true, or refuse.
#
# The re-execution is what BASH_ENV, ENV, LD_PRELOAD and LD_LIBRARY_PATH
# require: they have already acted by the time a script's first statement runs,
# so only a fresh interpreter started from ``env -i`` escapes them. The purge is
# what makes the result a guarantee rather than a request — it removes anything
# a platform runtime injected, and reduces a forged marker to nothing more than
# a guard against re-executing twice.
#
# TMPDIR is deliberately not carried across: every temporary file this
# installer creates is named relative to a fixed directory instead.
require_constructed_environment() {
    environment_is_constructed && return 0

    if [[ -z "${MGO_INSTALL_ENVIRONMENT_CONSTRUCTED:-}" ]]; then
        exec "$ENV_COMMAND" -i \
            "PATH=$SAFE_PATH" \
            "HOME=$ROOT_HOME" \
            "LC_ALL=C" \
            "SUDO_USER=${SUDO_USER:-}" \
            "MGO_INSTALL_ENVIRONMENT_CONSTRUCTED=1" \
            /bin/bash -p "$0" "$@"
    fi

    PATH="$SAFE_PATH"
    HOME="$ROOT_HOME"
    LC_ALL="C"
    export PATH HOME LC_ALL
    purge_environment || die "the execution environment could not be constructed"
    environment_is_constructed \
        || die "the execution environment could not be constructed"
}

# --- control-plane lock ----------------------------------------------------

# The same lock-object contract the gateway enforces.
#
# A readable lock file lets any unprivileged process hold an exclusive flock on
# it and deny every deployment and restart, so it must be a root-owned 0600
# regular file reached through a real directory.
require_secure_lock_object() {
    local lock_path="$1"
    local directory
    local ownership
    local mode

    directory="$(dirname "$lock_path")"
    [[ ! -L "$directory" ]] || return 1
    [[ -d "$directory" ]] || return 1

    [[ ! -L "$lock_path" ]] || return 1
    [[ -f "$lock_path" ]] || return 1

    ownership="$(stat -c '%u:%g' "$lock_path")" || return 1
    [[ "$ownership" == "0:0" ]] || return 1

    mode="$(stat -c '%a' "$lock_path")" || return 1
    [[ "$mode" == "600" ]]
}

# The one repair this installer is allowed to make.
#
# During the separately authorised first installation the lock may exist with a
# wider mode, left by an earlier gateway that did not enforce one. Tightening
# the mode is safe; replacing the file is not, because a legitimate holder's
# lock lives on the inode. So the inode is never touched.
repair_lock_mode() {
    local lock_path="$1"
    local ownership

    [[ ! -L "$lock_path" ]] || return 1
    [[ -f "$lock_path" ]] || return 1
    ownership="$(stat -c '%u:%g' "$lock_path")" || return 1
    [[ "$ownership" == "0:0" ]] || return 1

    chmod 0600 "$lock_path"
}

acquire_transaction_lock() {
    local lock_path="$1"
    local directory

    directory="$(dirname "$lock_path")"
    if [[ ! -d "$directory" ]]; then
        mkdir -p "$directory" || return "$EX_FAILED"
    fi

    if [[ ! -e "$lock_path" && ! -L "$lock_path" ]]; then
        (
            umask 0077
            set -C
            : >"$lock_path"
        ) 2>/dev/null || true
    elif ! require_secure_lock_object "$lock_path"; then
        repair_lock_mode "$lock_path" || return "$EX_FAILED"
    fi

    require_secure_lock_object "$lock_path" || return "$EX_FAILED"

    exec 9>>"$lock_path" || return "$EX_FAILED"
    flock -n "$LOCK_FD" || return "$EX_BUSY"
}

# --- inspection ------------------------------------------------------------

file_checksum() {
    local path="$1"
    is_regular_file "$path" || return 1
    sha256sum "$path" | awk '{ print $1 }'
}

# Numeric owner and group, not rendered names.
#
# A name comparison depends on what the passwd database happens to render, and
# an unresolvable UID renders as a bare number that could equally be a real
# account. Numbers are what the kernel enforces.
file_metadata() {
    local path="$1"
    is_regular_file "$path" || return 1
    stat -c '%u:%g:%a' "$path"
}

# A regular file, and not a symbolic link to one.
#
# ``-f`` follows symlinks, so a link pointing at matching content would pass
# every content check while the thing on disk is not the file at all — and
# installing over it would replace the link, while restoring would put back an
# ordinary file where a link used to be.
is_regular_file() {
    local path="$1"

    [[ ! -L "$path" ]] || return 1
    [[ -f "$path" ]]
}

# Absent, or a regular non-symlink file. Anything else is refused, not fixed.
#
# A directory, FIFO, socket or device where a target belongs is not something
# an installer should quietly resolve: opening it as content can block for
# ever, and replacing it would destroy whatever put it there.
target_type_is_supported() {
    local path="$1"

    [[ -e "$path" || -L "$path" ]] || return 0
    is_regular_file "$path"
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

    is_regular_file "$target" || return 1
    actual_sum="$(file_checksum "$target")" || return 1
    [[ "$actual_sum" == "$expected_sum" ]] || return 1
    actual_meta="$(file_metadata "$target")" || return 1
    [[ "$actual_meta" == "0:0:${expected_mode#0}" ]]
}

policy_is_valid() {
    local path="$1"
    is_regular_file "$path" || return 1
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
# The transaction parent must be a private, root-owned directory.
#
# It holds copies of the previous sudoers policy, so anything able to read it
# can read that policy, and anything able to write it can choose what a failed
# installation restores. A symlink is refused rather than followed: it would
# put the backups wherever whoever planted it chose.
require_secure_transaction_parent() {
    local parent="$1"
    local ownership
    local mode

    [[ ! -L "$parent" ]] || return 1
    [[ -d "$parent" ]] || return 1

    ownership="$(stat -c '%u:%g' "$parent")" || return 1
    [[ "$ownership" == "0:0" ]] || return 1

    mode="$(stat -c '%a' "$parent")" || return 1
    [[ "$mode" == "700" ]]
}

# Absent or safe and empty (0); unsupported or insecure (1); stale (2).
#
# The stale case is the one that matters. A run whose cleanup failed leaves its
# workspace behind, and on the next run both installed targets can be correct —
# so without this check the installer would report "verified; nothing changed"
# over the top of an unfinished transaction, turning "installation succeeded
# but cleanup failed" into a clean bill of health with the recovery artefacts
# still on disk.
#
# Reported, never resolved: this installer does not know what an unfinished run
# left or why, and destroying it would destroy the evidence an operator needs.
transaction_parent_state() {
    local parent="$1"
    local entries

    [[ ! -L "$parent" ]] || return 1
    if [[ ! -e "$parent" ]]; then
        return 0
    fi
    require_secure_transaction_parent "$parent" || return 1

    # -print -quit: existence, not an inventory, and it must not depend on
    # glob settings to notice a dotted name.
    #
    # The status is checked. An unreadable directory makes find fail with no
    # output, and treating that as "nothing is here" would turn the one
    # condition this function exists to detect into a clean answer.
    entries="$(find "$parent" -mindepth 1 -maxdepth 1 -print -quit)" || return 1
    if [[ -n "$entries" ]]; then
        return 2
    fi
    return 0
}

# A root-owned 0700 scratch parent, created before the first mutation.
open_transaction_parent() {
    local parent="$1"

    [[ ! -L "$parent" ]] || return 1
    if [[ -e "$parent" ]]; then
        [[ -d "$parent" ]] || return 1
    fi
    install -d -o root -g root -m 0700 "$parent" || return 1
    require_secure_transaction_parent "$parent"
}

# This run's own workspace, uniquely named, inside the parent.
#
# Created while the deployment lock is held, so the name cannot collide and no
# other run can be operating in the parent at the same time. Every backup and
# staging file this run makes belongs to this directory and no other, which is
# what lets cleanup remove exactly what this run created — no pattern, no
# wildcard, and nothing that could reach another run's evidence.
open_transaction_workspace() {
    local parent="$1"

    mktemp -d --tmpdir="$parent" "run.XXXXXX"
}

# What a workspace must be before anything is staged in it.
#
# mktemp -d creates it 0700 and owned by the caller, but that is a claim about
# what mktemp did, not about what is on disk — and this run is about to put a
# copy of the sudoers policy inside. The location is checked too: a workspace
# outside the fixed parent is not this installer's workspace.
require_secure_workspace() {
    local workspace="$1"
    local parent="$2"
    local ownership
    local mode

    [[ -n "$workspace" ]] || return 1
    [[ "$workspace" == "$parent"/* ]] || return 1

    [[ ! -L "$workspace" ]] || return 1
    [[ -d "$workspace" ]] || return 1

    ownership="$(stat -c '%u:%g' "$workspace")" || return 1
    [[ "$ownership" == "0:0" ]] || return 1

    mode="$(stat -c '%a' "$workspace")" || return 1
    [[ "$mode" == "700" ]]
}

# Remove this run's workspace, and say whether it went.
#
# "Not a directory" is not success. If the tracked path is still there as a
# regular file, a symlink or anything else, this run left something behind and
# the next one will find it — so it must report that now rather than let the
# discovery happen later, attributed to nobody.
close_transaction_workspace() {
    local workspace="$1"

    [[ -n "$workspace" ]] || return 1
    if [[ ! -e "$workspace" && ! -L "$workspace" ]]; then
        return 0
    fi
    [[ ! -L "$workspace" ]] || return 1
    [[ -d "$workspace" ]] || return 1
    rm -rf -- "$workspace" || return 1
    [[ ! -e "$workspace" && ! -L "$workspace" ]]
}

# --- the locked source snapshot --------------------------------------------

# Copy one repository source into this run's workspace, printing where it went.
#
# Everything after this point reads the copy. The live checkout is a moving
# target: a concurrent deploy-main can fast-forward it between the syntax check
# and the checksum, or between the checksum and the install, and the result
# would be a host running bytes nothing validated. Taking the snapshot under
# the lock makes "the bytes installed are the bytes validated" a fact rather
# than a race that usually goes the right way.
stage_source() {
    local source="$1"
    local workspace="$2"
    local name="$3"
    local staged="${workspace}/${name}"

    is_regular_file "$source" || return 1
    # 0600, root-owned: the staged sudoers policy is as sensitive as the
    # installed one, and nothing else needs to read either.
    install -o root -g root -m 0600 "$source" "$staged" || return 1
    printf '%s\n' "$staged"
}

# A staged copy must be exactly what this run put there.
require_secure_staged_file() {
    local staged="$1"
    local workspace="$2"
    local ownership
    local mode

    [[ "$staged" == "$workspace"/* ]] || return 1
    is_regular_file "$staged" || return 1

    ownership="$(stat -c '%u:%g' "$staged")" || return 1
    [[ "$ownership" == "0:0" ]] || return 1

    mode="$(stat -c '%a' "$staged")" || return 1
    [[ "$mode" == "600" ]]
}

# The shipped gateway must parse, under the same interpreter mode it will run
# in. A syntax error published under /usr/local/sbin is a control plane that
# cannot be invoked at all.
gateway_syntax_is_valid() {
    local path="$1"

    is_regular_file "$path" || return 1
    /bin/bash -p -n "$path" >/dev/null 2>&1
}

# Record a target's previous state into the transaction directory.
#
# Content, numeric owner, numeric group and mode are all preserved, so a
# restoration puts back the file that was there rather than an approximation of
# it. An empty result means the target was absent, which is a state whose
# restoration is removal.
record_previous() {
    local target="$1"
    local directory="$2"
    local backup

    if [[ ! -e "$target" && ! -L "$target" ]]; then
        printf '\n'
        return 0
    fi

    is_regular_file "$target" || return 1

    backup="$(mktemp "${directory}/previous.XXXXXX")" || return 1
    # --preserve=all keeps owner, group, mode and timestamps; --no-dereference
    # is redundant here because the type was already proven, and is kept as a
    # statement that this never follows a link.
    if ! cp --preserve=all --no-dereference "$target" "$backup"; then
        rm -f -- "$backup"
        return 1
    fi
    printf '%s\n' "$backup"
}

restore_one() {
    local target="$1"
    local backup="$2"

    if [[ -n "$backup" ]]; then
        [[ -f "$backup" ]] || return 1
        # Copy rather than rename: the backup lives on tmpfs and the target may
        # not, so a rename can cross filesystems and fail.
        cp -f --preserve=all --no-dereference "$backup" "$target" || return 1
        rm -f -- "$backup" || return 1
        return 0
    fi
    rm -f -- "$target" || return 1
    [[ ! -e "$target" ]]
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
    local transaction="${7:-$TRANSACTION_DIRECTORY}"
    local workspace
    local staged_gateway
    local staged_sudoers
    local gateway_sum
    local sudoers_sum
    local gateway_backup
    local sudoers_backup

    # Types before anything else. An unsupported target is refused, never
    # replaced: something put it there, and it is not this installer's to undo.
    is_regular_file "$gateway_source" \
        || { warn "the gateway source must be a regular non-symlink file"; return "$EX_FAILED"; }
    is_regular_file "$sudoers_source" \
        || { warn "the sudoers source must be a regular non-symlink file"; return "$EX_FAILED"; }
    target_type_is_supported "$gateway_target" \
        || { warn "the gateway target is not a regular file"; return "$EX_FAILED"; }
    target_type_is_supported "$sudoers_target" \
        || { warn "the sudoers target is not a regular file"; return "$EX_FAILED"; }

    open_transaction_parent "$transaction" || {
        warn "the transaction directory could not be created"
        return "$EX_FAILED"
    }
    workspace="$(open_transaction_workspace "$transaction")" || {
        warn "the transaction workspace could not be created"
        return "$EX_FAILED"
    }
    require_secure_workspace "$workspace" "$transaction" || {
        warn "the transaction workspace is not a root-owned 0700 directory inside $transaction"
        close_transaction_workspace "$workspace" || true
        return "$EX_FAILED"
    }

    # --- the locked snapshot -------------------------------------------------
    #
    # Both sources are copied here, and everything below reads the copies. The
    # live checkout is under a concurrent deploy-main's control; the workspace
    # is under this run's. Validating one and installing the other is how a host
    # ends up running bytes nothing checked.
    staged_gateway="$(stage_source "$gateway_source" "$workspace" "mgo-validate")" || {
        warn "the gateway source could not be staged"
        close_transaction_workspace "$workspace" || true
        return "$EX_FAILED"
    }
    staged_sudoers="$(stage_source "$sudoers_source" "$workspace" "mgo-validate.sudoers")" || {
        warn "the sudoers source could not be staged"
        close_transaction_workspace "$workspace" || true
        return "$EX_FAILED"
    }
    require_secure_staged_file "$staged_gateway" "$workspace" \
        && require_secure_staged_file "$staged_sudoers" "$workspace" || {
        warn "a staged source is not a root-owned 0600 regular file"
        close_transaction_workspace "$workspace" || true
        return "$EX_FAILED"
    }

    # Validated from the snapshot, not from the checkout. An invalid file under
    # /etc/sudoers.d can lock every account out of sudo, so this happens before
    # either target is touched.
    log "checking staged gateway syntax"
    gateway_syntax_is_valid "$staged_gateway" || {
        warn "the staged gateway failed its shell syntax check"
        close_transaction_workspace "$workspace" || true
        return "$EX_FAILED"
    }
    log "checking staged sudoers policy"
    policy_is_valid "$staged_sudoers" || {
        warn "the staged sudoers policy failed validation"
        close_transaction_workspace "$workspace" || true
        return "$EX_FAILED"
    }

    # And the expected checksums come from the snapshot too, so "installed
    # matches expected" is a statement about the bytes that were validated.
    gateway_sum="$(file_checksum "$staged_gateway")" || {
        close_transaction_workspace "$workspace" || true
        return "$EX_FAILED"
    }
    sudoers_sum="$(file_checksum "$staged_sudoers")" || {
        close_transaction_workspace "$workspace" || true
        return "$EX_FAILED"
    }

    # Verified and finished before a target is touched. An installer that
    # rewrites files that are already correct changes their inode and
    # modification time for nothing, and cannot be run safely on a whim.
    #
    # This is judged against the *staged* checksums, which is why it happens
    # here rather than before the snapshot: "current" has to mean "matches the
    # bytes this run validated".
    if target_is_current "$gateway_target" "$gateway_sum" "$gateway_mode" \
        && target_is_current "$sudoers_target" "$sudoers_sum" "$sudoers_mode" \
        && policy_is_valid "$sudoers_target"; then
        log "gateway and sudoers policy are already installed, correct and valid"
        if ! close_transaction_workspace "$workspace"; then
            warn "the transaction directory could not be removed"
            return "$EX_FAILED"
        fi
        log "verified; nothing changed"
        return "$EX_CURRENT"
    fi

    gateway_backup="$(record_previous "$gateway_target" "$workspace")" || {
        warn "the existing gateway could not be recorded"
        close_transaction_workspace "$workspace" || true
        return "$EX_FAILED"
    }
    sudoers_backup="$(record_previous "$sudoers_target" "$workspace")" || {
        warn "the existing sudoers policy could not be recorded"
        # Nothing has been mutated yet.
        close_transaction_workspace "$workspace" || true
        return "$EX_FAILED"
    }

    # --- from here, any failure restores both targets ---
    #
    # Installed from the snapshot. Nothing below reads the live checkout again.
    log "installing $gateway_target"
    if ! install_file "$staged_gateway" "$gateway_target" "$gateway_mode"; then
        abort_pair "the gateway could not be installed" \
            "$workspace" \
            "$gateway_target" "$gateway_backup" \
            "$sudoers_target" "$sudoers_backup"
        return "$?"
    fi

    log "installing $sudoers_target"
    if ! install_file "$staged_sudoers" "$sudoers_target" "$sudoers_mode"; then
        abort_pair "the sudoers policy could not be installed" \
            "$workspace" \
            "$gateway_target" "$gateway_backup" \
            "$sudoers_target" "$sudoers_backup"
        return "$?"
    fi

    if ! verify_installed "$gateway_target" "$gateway_sum" "$gateway_mode"; then
        abort_pair "the installed gateway does not match its source" \
            "$workspace" \
            "$gateway_target" "$gateway_backup" \
            "$sudoers_target" "$sudoers_backup"
        return "$?"
    fi

    if ! verify_installed "$sudoers_target" "$sudoers_sum" "$sudoers_mode"; then
        abort_pair "the installed sudoers policy does not match its source" \
            "$workspace" \
            "$gateway_target" "$gateway_backup" \
            "$sudoers_target" "$sudoers_backup"
        return "$?"
    fi

    # Validate what actually landed, not only what was about to be written.
    # The checksum says the bytes match; these say the bytes still *work* after
    # the rename, under the interpreter and the parser that will read them.
    if ! gateway_syntax_is_valid "$gateway_target"; then
        abort_pair "the installed gateway failed its shell syntax check" \
            "$workspace" \
            "$gateway_target" "$gateway_backup" \
            "$sudoers_target" "$sudoers_backup"
        return "$?"
    fi

    if ! policy_is_valid "$sudoers_target"; then
        abort_pair "the installed sudoers policy failed validation" \
            "$workspace" \
            "$gateway_target" "$gateway_backup" \
            "$sudoers_target" "$sudoers_backup"
        return "$?"
    fi

    # Cleanup is part of the transaction, not an afterthought. A run that left
    # a copy of the previous sudoers policy on disk has not finished, and
    # saying otherwise would hide it — the next run refuses on exactly this
    # evidence, so reporting success here would be reporting it twice wrongly.
    #
    # Only this run's workspace is removed. The parent stays, empty, and
    # anything else inside it belongs to a run that is not this one.
    if ! close_transaction_workspace "$workspace"; then
        warn "the transaction directory could not be removed"
        return "$EX_FAILED"
    fi
    return 0
}

abort_pair() {
    local reason="$1"
    local workspace="$2"
    shift 2

    warn "$reason"
    if ! restore_pair "$@"; then
        warn "installation failed AND restoration failed; the host was NOT restored"
        return "$EX_RESTORE_FAILED"
    fi
    if ! close_transaction_workspace "$workspace"; then
        warn "installation failed, both targets were restored, but cleanup failed"
        return "$EX_RESTORE_FAILED"
    fi
    warn "installation failed; both targets were restored"
    return "$EX_FAILED"
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
    local transaction="${9:-$TRANSACTION_DIRECTORY}"
    local lock_path="${10:-$LOCK_FILE}"
    local lock_outcome
    local transaction_state
    local outcome

    if [[ "$dry_run" -eq 0 && "$effective_uid" -ne 0 ]]; then
        warn "installation requires root; re-run with sudo, or use --dry-run"
        return "$EX_FAILED"
    fi

    [[ -f "$gateway_source" ]] || { warn "gateway source is missing"; return "$EX_FAILED"; }
    [[ -f "$sudoers_source" ]] || { warn "sudoers source is missing"; return "$EX_FAILED"; }

    # visudo is required in both modes. A dry run promises to validate
    # everything, so a host without it fails rather than reporting a success it
    # did not earn.
    if ! command -v visudo >/dev/null 2>&1; then
        warn "visudo is not available; refusing to proceed without validation"
        return "$EX_FAILED"
    fi

    if [[ "$dry_run" -eq 1 ]]; then
        dry_run_installation \
            "$gateway_source" "$gateway_target" "$gateway_mode" \
            "$sudoers_source" "$sudoers_target" "$sudoers_mode" \
            "$transaction" "$lock_path"
        return "$?"
    fi

    # --- the lock comes first ------------------------------------------------
    #
    # Before any source is read for validation, not after. The installer used
    # to check syntax, validate the policy and checksum the sources, and only
    # then take the lock — leaving a window in which a concurrent deploy-main
    # could fast-forward the checkout underneath all three. Everything that
    # follows now happens with the control plane held.
    lock_outcome=0
    acquire_transaction_lock "$lock_path" || lock_outcome="$?"
    if [[ "$lock_outcome" -eq "$EX_BUSY" ]]; then
        warn "another deployment or restart is already in progress"
        return "$EX_BUSY"
    fi
    if [[ "$lock_outcome" -ne 0 ]]; then
        warn "the deployment lock could not be opened"
        return "$EX_FAILED"
    fi

    # Read under the lock, so a workspace belonging to a run that is still
    # going cannot be mistaken for one a finished run abandoned.
    transaction_state=0
    transaction_parent_state "$transaction" || transaction_state="$?"

    if [[ "$transaction_state" -eq 1 ]]; then
        warn "the transaction directory $transaction is not a root-owned 0700 directory"
        return "$EX_FAILED"
    fi
    if [[ "$transaction_state" -eq 2 ]]; then
        warn "stale transaction state is present in $transaction: a previous run did not complete its cleanup"
        warn "it is preserved for inspection; remove it deliberately before installing again"
        return "$EX_STALE"
    fi

    outcome=0
    install_pair \
        "$gateway_source" "$gateway_target" "$gateway_mode" \
        "$sudoers_source" "$sudoers_target" "$sudoers_mode" \
        "$transaction" || outcome="$?"
    [[ "$outcome" -ne "$EX_CURRENT" ]] || return 0
    [[ "$outcome" -eq 0 ]] || return "$outcome"

    log "installed and verified"
    log "the approval file, the production repository and mgo.service were not touched"
}

# A report, and an exit status that agrees with it.
#
# Read-only and deliberately non-locking, so it validates a point-in-time view
# of the sources and claims no exclusion against a concurrent deployment. What
# it must not do is exit zero for a state the real installation would refuse: a
# validation command that answers "fine" and then refuses when run for real has
# told the operator nothing.
dry_run_installation() {
    local gateway_source="$1"
    local gateway_target="$2"
    local gateway_mode="$3"
    local sudoers_source="$4"
    local sudoers_target="$5"
    local sudoers_mode="$6"
    local transaction="$7"
    local lock_path="$8"
    local transaction_state
    local gateway_sum
    local sudoers_sum
    local outcome=0

    log "dry run: validating a point-in-time view; no lock is held"

    if ! gateway_syntax_is_valid "$gateway_source"; then
        warn "the gateway failed its shell syntax check"
        outcome="$EX_FAILED"
    fi
    if ! visudo -cf "$sudoers_source" >/dev/null 2>&1; then
        warn "the sudoers policy failed validation"
        outcome="$EX_FAILED"
    fi
    if ! target_type_is_supported "$gateway_target"; then
        warn "the gateway target is not a regular file"
        outcome="$EX_FAILED"
    fi
    if ! target_type_is_supported "$sudoers_target"; then
        warn "the sudoers target is not a regular file"
        outcome="$EX_FAILED"
    fi
    # The lock object is inspected, never taken: a dry run must not be able to
    # block a real deployment, but an unsafe lock is a refusal it would meet.
    if [[ -e "$lock_path" || -L "$lock_path" ]] \
        && ! require_secure_lock_object "$lock_path"; then
        warn "the deployment lock is not a root-owned 0600 regular file"
        outcome="$EX_FAILED"
    fi

    gateway_sum="$(file_checksum "$gateway_source")" || return "$EX_FAILED"
    sudoers_sum="$(file_checksum "$sudoers_source")" || return "$EX_FAILED"

    if target_is_current "$gateway_target" "$gateway_sum" "$gateway_mode"; then
        log "dry run: $gateway_target is current"
    else
        log "dry run: would install $gateway_target ($gateway_mode root:root)"
    fi
    if target_is_current "$sudoers_target" "$sudoers_sum" "$sudoers_mode" \
        && policy_is_valid "$sudoers_target"; then
        log "dry run: $sudoers_target is current"
    else
        log "dry run: would install $sudoers_target ($sudoers_mode root:root)"
    fi

    transaction_state=0
    transaction_parent_state "$transaction" || transaction_state="$?"
    if [[ "$transaction_state" -eq 1 ]]; then
        warn "the transaction directory $transaction is not a root-owned 0700 directory"
        outcome="$EX_FAILED"
    fi
    if [[ "$transaction_state" -eq 2 ]]; then
        warn "stale transaction state is present in $transaction: a previous run did not complete its cleanup"
        warn "it is preserved for inspection; remove it deliberately before installing again"
        # The code the real installation would exit with, so a wrapper reading
        # the status learns the same thing from either mode.
        outcome="$EX_STALE"
    fi

    if [[ "$outcome" -eq 0 ]]; then
        log "dry run complete; nothing was changed and validation passed"
        return 0
    fi
    warn "dry run complete; nothing was changed, and the installation would refuse"
    return "$outcome"
}

main() {
    local script_dir
    local dry_run=0
    local outcome=0

    # Before anything else, including reading the arguments.
    require_constructed_environment "$@"

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
