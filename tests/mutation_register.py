"""The deployment gateway's mutation register.

Every entry names one deliberate defect in a shipped shell asset and the tests
that must fail because of it. A contract nothing fails for is a contract
nothing is enforcing, and a test suite that passes against a broken gateway is
worth exactly what it costs to run.

This file exists because the earlier rounds of this work applied their
mutations by hand and recorded only their outcome. That made the result
unreproducible: a mutation written against code a later round rewrote goes
stale silently, and a historical pass cannot be re-earned against a new tip. A
register in the repository can be re-run against any commit, by anyone, and it
fails loudly when it goes stale.

Run it with::

    uv run python scripts/dev/run-mutations.py

Each mutation is applied to a byte-exact copy of the asset, the named tests are
run, the asset is restored and its digest compared, and the mutation is
recorded as detected only if the tests actually failed.

``old`` must appear **exactly once** in the asset. That is not a convenience:
it is what makes a mutation mean one thing. When a mutation stops applying,
the code it described has changed and the register entry has to be rewritten
rather than quietly dropped.
"""

from __future__ import annotations

from typing import NamedTuple


class Mutation(NamedTuple):
    """One deliberate defect and the tests that must catch it."""

    identifier: str
    asset: str
    old: str
    new: str
    tests: str
    note: str


GATEWAY = "scripts/deploy/mgo-validate"
INSTALLER = "scripts/deploy/install-mgo-validate.sh"


MUTATIONS: tuple[Mutation, ...] = (
    Mutation(
        'approval-existence',
        GATEWAY,
        '    [[ -e "$path" ]] \\\n'
        '        || die "$EX_REQUEST" "no approval file is installed"',
        '    true',
        'missing_approval_file',
        'An absent approval file stops being its own diagnosis.',
    ),
    Mutation(
        'approval-symlink',
        GATEWAY,
        '    [[ ! -L "$path" ]] \\\n'
        '        || die "$EX_REQUEST" "the approval file must not be a symlink"',
        '    true',
        'symlink_refusal_precedes_the_regular_file',
        'A link redirects the deployment authority.',
    ),
    Mutation(
        'approval-regular-file',
        GATEWAY,
        '    [[ -f "$path" ]] \\\n'
        '        || die "$EX_REQUEST" "the approval file must be a regular file"',
        '    true',
        'directory_is_not_a_valid_approval',
        'A directory or device is accepted as authority.',
    ),
    Mutation(
        'approval-owner',
        GATEWAY,
        '    [[ "$owner" == "0" ]] \\',
        '    [[ -n "$owner" ]] \\',
        'non_root_owner_is_rejected',
        'Any account may then write the approval.',
    ),
    Mutation(
        'approval-group-write',
        GATEWAY,
        '        [2367])\n'
        '            die "$EX_REQUEST" \\\n'
        '                "the approval file must not be writable by group"\n'
        '            ;;',
        '        [2367]) ;;',
        'group_or_other_writable',
        "A group-writable authority is outside root's control.",
    ),
    Mutation(
        'approval-other-write',
        GATEWAY,
        '        [2367])\n'
        '            die "$EX_REQUEST" \\\n'
        '                "the approval file must not be writable by other"\n'
        '            ;;',
        '        [2367]) ;;',
        'group_or_other_writable',
        "A world-writable authority is anybody's to set.",
    ),
    Mutation(
        'approval-size-upper-bound',
        GATEWAY,
        '    if [[ "$size" == "41" ]]; then',
        '    if [[ "$size" -ge "41" ]]; then',
        'long_sha_is_rejected or byte_exact_approval',
        'Trailing data rides along behind a valid SHA.',
    ),
    Mutation(
        'approval-final-byte',
        GATEWAY,
        '        [[ "$last_byte" == "0a" ]] \\',
        '        [[ -n "$last_byte" ]] \\',
        'approval',
        'A trailing NUL or CR passes as a newline.',
    ),
    Mutation(
        'approval-size-lower-bound',
        GATEWAY,
        '    elif [[ "$size" != "40" ]]; then',
        '    elif [[ "$size" -lt "40" ]]; then',
        'approval',
        'A short file is no longer refused on length.',
    ),
    Mutation(
        'approval-case',
        GATEWAY,
        '    [[ "$content" =~ ^[0-9a-f]{40}$ ]] \\\n'
        '        || die "$EX_REQUEST" "approved SHA is missing or malformed"',
        '    [[ "$content" =~ ^[0-9a-fA-F]{40}$ ]] \\\n'
        '        || die "$EX_REQUEST" "approved SHA is missing or malformed"',
        'uppercase_sha',
        'Two spellings of one commit are two authorities.',
    ),
    Mutation(
        'root-uid',
        GATEWAY,
        '    [[ "$effective_uid" -eq 0 ]] \\',
        '    [[ "$effective_uid" -ge 0 ]] \\',
        'non_root_caller_is_rejected',
        'The privilege check stops requiring privilege.',
    ),
    Mutation(
        'sudo-caller-present',
        GATEWAY,
        '    [[ -n "$sudo_caller" ]] \\',
        '    [[ -z "$sudo_caller" || -n "$sudo_caller" ]] \\',
        'missing_sudo_caller',
        'A direct root invocation is no longer distinguished.',
    ),
    Mutation(
        'sudo-caller-identity',
        GATEWAY,
        '    [[ "$sudo_caller" == "$admin_account" ]] \\',
        '    [[ -n "$sudo_caller" ]] \\',
        'another_caller_is_rejected',
        'Any sudoer becomes a deployer.',
    ),
    Mutation(
        'admin-account-exists',
        GATEWAY,
        '    id -u "$admin_account" >/dev/null 2>&1 \\',
        '    true \\',
        'caller_account_that_does_not_exist',
        'A host missing the account fails later, mid-deployment.',
    ),
    Mutation(
        'admin-environment',
        GATEWAY,
        '    runuser -u "$admin_account" -- env -i \\',
        '    runuser -u "$admin_account" -- env \\',
        'admin_environment_is_constructed',
        'GIT_DIR and friends reach a root-invoked deployment.',
    ),
    Mutation(
        'admin-home-absolute',
        GATEWAY,
        '    [[ "$home" == /* ]] || return 1',
        '    true',
        'unusable_account_home',
        'A relative home is guessed at rather than refused.',
    ),
    Mutation(
        'admin-home-source',
        GATEWAY,
        '    home="$(getent passwd "$account" | cut -d: -f6)" || return 1',
        '    home="${HOME:-/root}"',
        'admin_home_comes_from_the_account_database or falls_back_to_the_environment',
        'The caller chooses the SSH key and Git configuration.',
    ),
    Mutation(
        'runtime-probe-environment',
        GATEWAY,
        '    runuser -u "$runtime_account" -- env -i \\',
        '    runuser -u "$runtime_account" -- env \\',
        'runtime_probe_environment',
        'The probe may import a different application.',
    ),
    Mutation(
        'env-shebang',
        GATEWAY,
        '#!/bin/bash\n'
        '',
        '#!/usr/bin/env bash\n'
        '',
        'fixed_interpreter',
        "The interpreter is chosen by the caller's PATH.",
    ),
    Mutation(
        'installer-shebang',
        INSTALLER,
        '#!/bin/bash\n'
        '',
        '#!/usr/bin/env bash\n'
        '',
        'fixed_interpreter',
        'Same, for the installer.',
    ),
    Mutation(
        'env-no-construction',
        GATEWAY,
        'require_constructed_environment() {\n'
        '    environment_is_constructed && return 0',
        'require_constructed_environment() {\n'
        '    return 0\n'
        '    environment_is_constructed && return 0',
        'inherited_variable_survives or inherited_path_cannot',
        "Nothing is constructed; the caller's environment is used.",
    ),
    Mutation(
        'env-allowlist-open',
        GATEWAY,
        '        case " $MGO_PERMITTED_ENVIRONMENT " in\n'
        '            *" $name "*) ;;\n'
        '            *) return 1 ;;\n'
        '        esac',
        '        :',
        'looks_constructed',
        'Any variable at all counts as a constructed environment.',
    ),
    Mutation(
        'env-purge-disabled',
        GATEWAY,
        '        unset "$name" 2>/dev/null || failures=$((failures + 1))',
        '        :',
        'marker_does_not_skip',
        "A forged marker keeps the caller's whole environment.",
    ),
    Mutation(
        'env-path-passthrough',
        GATEWAY,
        '            "PATH=$MGO_SAFE_PATH" \\\n'
        '            "HOME=$MGO_ROOT_HOME" \\',
        '            "PATH=$PATH" \\\n'
        '            "HOME=$MGO_ROOT_HOME" \\',
        'only_the_action_and_the_sudo_caller',
        'An inherited PATH chooses which executable runs.',
    ),
    Mutation(
        'env-tmpdir-carried',
        GATEWAY,
        '            "SUDO_USER=${SUDO_USER:-}" \\\n'
        '            "MGO_ENVIRONMENT_CONSTRUCTED=1" \\',
        '            "SUDO_USER=${SUDO_USER:-}" \\\n'
        '            "TMPDIR=${TMPDIR:-/tmp}" \\\n'
        '            "MGO_ENVIRONMENT_CONSTRUCTED=1" \\',
        'only_the_action_and_the_sudo_caller',
        'The caller picks where response bodies are written.',
    ),
    Mutation(
        'env-reexec-interpreter',
        GATEWAY,
        '            /bin/bash "$0" "$@"',
        '            bash "$0" "$@"',
        'environment_boundary_is_an_allowlist',
        'The re-execution resolves bash through PATH again.',
    ),
    Mutation(
        'env-bash-env-kept',
        GATEWAY,
        '        PYTHONWARNINGS CURL_HOME SSH_AUTH_SOCK BASH_ENV ENV \\',
        '        PYTHONWARNINGS CURL_HOME SSH_AUTH_SOCK ENV \\',
        'behaviour_altering_variable',
        'BASH_ENV leaves the explicit register.',
    ),
    Mutation(
        'installer-no-construction',
        INSTALLER,
        'require_constructed_environment() {\n'
        '    environment_is_constructed && return 0',
        'require_constructed_environment() {\n'
        '    return 0\n'
        '    environment_is_constructed && return 0',
        'inherited_variable_survives or inherited_path_cannot',
        "The installer relies on sudo's optional env_reset again.",
    ),
    Mutation(
        'installer-purge-disabled',
        INSTALLER,
        '        unset "$name" 2>/dev/null || failures=$((failures + 1))',
        '        :',
        'marker_does_not_skip',
        'Same forged-marker hole, in the installer.',
    ),
    Mutation(
        'installer-tmpdir-carried',
        INSTALLER,
        '            "SUDO_USER=${SUDO_USER:-}" \\\n'
        '            "MGO_INSTALL_ENVIRONMENT_CONSTRUCTED=1" \\',
        '            "SUDO_USER=${SUDO_USER:-}" \\\n'
        '            "TMPDIR=${TMPDIR:-/tmp}" \\\n'
        '            "MGO_INSTALL_ENVIRONMENT_CONSTRUCTED=1" \\',
        'only_the_action_and_the_sudo_caller',
        "The caller picks where the installer's backups are staged.",
    ),
    Mutation(
        'show-approval-creates-tmpdir',
        GATEWAY,
        '        show-approval)\n'
        '            require_root_caller "$MGO_ADMIN_ACCOUNT" "$EUID" '
        '"${SUDO_USER:-}"\n'
        '            action_show_approval',
        '        show-approval)\n'
        '            require_root_caller "$MGO_ADMIN_ACCOUNT" "$EUID" '
        '"${SUDO_USER:-}"\n'
        'prepare_root_tmpdir "$MGO_ROOT_TMPDIR" "$MGO_ROOT_TMPDIR_PARENT"\n'
        '            action_show_approval',
        'show_approval_prepares_nothing',
        'The read-only action changes the host.',
    ),
    Mutation(
        'tmpdir-follow-symlink',
        GATEWAY,
        '    [[ ! -L "$path" ]] || return 1\n'
        '    [[ -d "$path" ]] || return 1',
        '    [[ -d "$path" ]] || return 1',
        'temporary_directory_symlink_is_refused',
        'A planted link redirects every temporary file.',
    ),
    Mutation(
        'tmpdir-prepare-follows-symlink',
        GATEWAY,
        '    [[ ! -L "$path" ]] \\\n'
        '        || die "$EX_PRECONDITION" \\\n'
        '            "the temporary directory must not be a symlink"',
        '    true',
        'temporary_directory_symlink_is_refused',
        'The symlink refusal moves after creation.',
    ),
    Mutation(
        'tmpdir-owner',
        GATEWAY,
        '    [[ "$ownership" == "0:0" ]] || return 1\n'
        '\n'
        '    mode="$(stat -c \'%a\' "$path")" || return 1\n'
        '    [[ "$mode" == "700" ]] || return 1',
        '    [[ -n "$ownership" ]] || return 1\n'
        '\n'
        '    mode="$(stat -c \'%a\' "$path")" || return 1\n'
        '    [[ "$mode" == "700" ]] || return 1',
        'temporary_directory_owned_by_another_account',
        'An unprivileged account owns the staging directory.',
    ),
    Mutation(
        'tmpdir-mode',
        GATEWAY,
        '    [[ "$mode" == "700" ]] || return 1',
        '    [[ -n "$mode" ]] || return 1',
        'temporary_directory_with_any_other_mode',
        'A world-readable staging directory is accepted.',
    ),
    Mutation(
        'tmpdir-repaired',
        GATEWAY,
        '    require_secure_root_tmpdir "$path" "$parent" \\\n'
        '        || die "$EX_PRECONDITION" \\',
        '    chmod 0700 "$path" 2>/dev/null || true\n'
        '    require_secure_root_tmpdir "$path" "$parent" \\\n'
        '        || die "$EX_PRECONDITION" \\',
        'temporary_directory_is_never_repaired',
        'An unsafe object is adopted instead of refused.',
    ),
    Mutation(
        'tmpdir-parent-physical',
        GATEWAY,
        '    canonical="$(cd "$parent" && pwd -P)" || return 1\n'
        '    [[ "$canonical" == "$parent" ]] || return 1',
        '    canonical="$parent"\n'
        '    [[ "$canonical" == "$parent" ]] || return 1',
        'temporary_directory_parent_must_be_its_physical_path',
        'A symlinked /run moves every response body.',
    ),
    Mutation(
        'tmpdir-created-then-chmodded',
        GATEWAY,
        '        umask 0077\n'
        '        mkdir -- "$path"',
        '        mkdir -- "$path"\n'
        '        chmod 0700 "$path"',
        'temporary_directory_is_created_in_one_step',
        'The directory exists at a wider mode before it is secured.',
    ),
    Mutation(
        'tmpdir-ignored-by-mktemp',
        GATEWAY,
        '    mktemp --tmpdir="$directory" "mgo-validate.XXXXXX"',
        '    mktemp',
        'caller_tmpdir_cannot_choose or every_temporary_file_is_named',
        'The location returns to being an environment property.',
    ),
    Mutation(
        'json-constants-accepted',
        GATEWAY,
        '        text, object_pairs_hook=Obj, parse_constant=refuse_constant',
        '        text, object_pairs_hook=Obj',
        'non_standard_numeric_constant or parser_refuses_constants',
        'NaN, Infinity and -Infinity parse as JSON.',
    ),
    Mutation(
        'json-parser-not-isolated',
        GATEWAY,
        '    "$MGO_ENV_COMMAND" -i "$parser" -I -c \'',
        '    "$parser" -c \'',
        'parser_runs_in_an_isolated_interpreter',
        'PYTHONINSPECT, PYTHONSTARTUP and PYTHONPATH reach the parser.',
    ),
    Mutation(
        'json-duplicate-keys',
        GATEWAY,
        'if keys.count("state") != 1:',
        'if keys.count("state") < 1:',
        'unusable_status_document',
        'An ambiguous document is answered anyway.',
    ),
    Mutation(
        'json-non-string-state',
        GATEWAY,
        'if not isinstance(value, str):\n'
        '    sys.exit(1)',
        'value = str(value)',
        'unusable_status_document or refused_cleanly',
        'A number or a list becomes a preview state.',
    ),
    Mutation(
        'json-object-type',
        GATEWAY,
        'if not isinstance(document, Obj):',
        'if False:',
        'refused_cleanly',
        'A JSON array reaches code that assumes ordered pairs.',
    ),
    Mutation(
        'json-utf8',
        GATEWAY,
        '    text = raw.decode("utf-8")',
        '    text = raw.decode("utf-8", "replace")',
        'parser_decodes_strictly',
        'A corrupted response is repaired into a plausible one.',
    ),
    Mutation(
        'preview-vocabulary',
        GATEWAY,
        '        running | stopped | failed | starting | stopping) ;;\n'
        '        *) return 1 ;;',
        '        *) ;;',
        'unsupported_state_token',
        'An unrecognised token becomes a deployment baseline.',
    ),
    Mutation(
        'parser-reads-shell-argument',
        GATEWAY,
        '\' "$body_file"',
        '\' "$(cat "$body_file")"',
        'parser_reads_the_file_rather_than_shell_arguments',
        'An arbitrary remote response passes through the shell.',
    ),
    Mutation(
        'curl-configuration-honoured-get',
        GATEWAY,
        '    status="$(curl --disable --noproxy \'*\' -sS --max-time 5 \\',
        '    status="$(curl --noproxy \'*\' -sS --max-time 5 \\',
        'ignores_curl_configuration',
        "A .curlrc may add --location behind the gateway's back.",
    ),
    Mutation(
        'curl-configuration-honoured-post',
        GATEWAY,
        '    status="$(curl --disable --noproxy \'*\' -sS --max-time 20 \\',
        '    status="$(curl --noproxy \'*\' -sS --max-time 20 \\',
        'ignores_curl_configuration',
        'Same, on the one write the gateway makes.',
    ),
    Mutation(
        'curl-follows-redirects-get',
        GATEWAY,
        '        --no-location --max-redirs 0 \\\n'
        '        --request GET \\',
        '        --request GET \\',
        'no_probe_follows_a_redirect',
        'A moved endpoint reads as healthy.',
    ),
    Mutation(
        'curl-follows-redirects-post',
        GATEWAY,
        '        --no-location --max-redirs 0 \\\n'
        '        --request POST \\',
        '        --request POST \\',
        'no_probe_follows_a_redirect',
        'A redirected start request reads as success.',
    ),
    Mutation(
        'curl-proxy-allowed',
        GATEWAY,
        "curl --disable --noproxy '*' -sS --max-time 5 \\",
        'curl --disable -sS --max-time 5 \\',
        'no_endpoint_probe_uses_a_proxy',
        'The answer may describe some other host.',
    ),
    Mutation(
        'http-get-inexact-status',
        GATEWAY,
        '    [[ "$status" == "200" ]]\n'
        '}\n'
        '\n'
        'http_post_200()',
        '    [[ "$status" =~ ^2 ]]\n'
        '}\n'
        '\n'
        'http_post_200()',
        'exact_200_counts_as_healthy',
        'A 201 or 204 with no body passes as healthy.',
    ),
    Mutation(
        'http-post-inexact-status',
        GATEWAY,
        '    [[ "$status" == "200" ]]\n'
        '}\n'
        '\n'
        '# Literal loopback',
        '    [[ "$status" =~ ^2 ]]\n'
        '}\n'
        '\n'
        '# Literal loopback',
        'preview_start_requires_an_exact_200',
        'A preview start that did not happen reads as one that did.',
    ),
    Mutation(
        'cleanup-failure-ignored',
        GATEWAY,
        '    http_get_200 "$url" "$body_file" || outcome=1\n'
        '    discard_temporary "$body_file" || outcome=1',
        '    http_get_200 "$url" "$body_file" || outcome=1\n'
        '    discard_temporary "$body_file" || true',
        'failed_temporary_cleanup',
        'The host accumulates response bodies and reports success.',
    ),
    Mutation(
        'cleanup-wildcard',
        GATEWAY,
        '    rm -f -- "$path" || return 1',
        '    rm -rf -- "$path"* || return 1',
        'cleanup_never_broadens',
        'A pattern removal reaches beyond the tracked file.',
    ),
    Mutation(
        'recovery-ignores-health',
        GATEWAY,
        '        if service_is_active "$service" && endpoint_is_ok "$health_url"; then',
        '        if service_is_active "$service"; then',
        'recovery_requires_both',
        'An active unit failing every request counts as recovered.',
    ),
    Mutation(
        'recovery-timeout-succeeds',
        GATEWAY,
        '    done\n'
        '    return 1\n'
        '}\n'
        '\n'
        '# --- preview preservation',
        '    done\n'
        '    return 0\n'
        '}\n'
        '\n'
        '# --- preview preservation',
        'recovery_is_bounded',
        'Exhausting the bound is reported as success.',
    ),
    Mutation(
        'preview-duplicate-start',
        GATEWAY,
        '        if [[ "$current" == "running" ]]; then\n'
        '            # Nothing to restore.',
        '        if false; then\n'
        '            # Nothing to restore.',
        'duplicate_start',
        'A second start is issued against a camera that has an owner.',
    ),
    Mutation(
        'preview-drift-ignored',
        GATEWAY,
        '    if [[ "$current" == "running" ]]; then\n'
        '        warn "preview was $previous_state before deployment but is now '
        'running"\n'
        '        return 1\n'
        '    fi',
        '    if false; then\n'
        '        warn "preview was $previous_state before deployment but is now '
        'running"\n'
        '        return 1\n'
        '    fi',
        'drifts_into_running',
        'A camera that started itself is papered over.',
    ),
    Mutation(
        'preview-baseline-running',
        GATEWAY,
        '        running) require_producer_count 1 ;;',
        '        running) true ;;',
        'baseline_reconciles_status_with_processes',
        'The reported state and the processes may disagree.',
    ),
    Mutation(
        'preview-legacy-producer',
        GATEWAY,
        '    [[ "$(count_processes libcamera-vid)" -eq 0 ]] || return 1',
        '    true',
        'legacy_producer',
        'Something other than this deployment holds the camera.',
    ),
    Mutation(
        'preview-preservation-assumed',
        GATEWAY,
        'log "preview is $current, matching its non-running state before deployment"\n'
        '    require_producer_count 0 || return 1',
        'log "preview is $current, matching its non-running state before deployment"',
        'producer_running_behind_a_stopped_preview',
        '"Left alone" becomes "not checked".',
    ),
    Mutation(
        'preview-transient-baseline',
        GATEWAY,
        '    kind="$(classify_preview_state "$state")"\n'
        '    [[ "$kind" == "stable" ]] || return 1',
        '    kind="$(classify_preview_state "$state")"',
        'transient_state_is_not_a_deployment_baseline',
        'A camera mid-transition becomes a baseline.',
    ),
    Mutation(
        'no-capture-boundary',
        GATEWAY,
        'readonly MGO_PREVIEW_START_URL="http://127.0.0.1:8080/camera/preview/start"',
        'readonly MGO_PREVIEW_START_URL="http://127.0.0.1:8080/camera/capture"',
        'never_contacts_the_stream_or_capture',
        'The gateway starts exercising the camera.',
    ),
    Mutation(
        'sync-resolves',
        GATEWAY,
        'run_as_admin "$admin_account" uv sync --frozen)',
        'run_as_admin "$admin_account" uv sync)',
        'environment_sync_is_always_frozen',
        'Production drifts to versions nothing verified.',
    ),
    Mutation(
        'uv-availability-not-proven',
        GATEWAY,
        '    run_as_admin "$admin_account" uv --version >/dev/null 2>&1 \\\n'
        '        || die "$EX_PRECONDITION" \\',
        '    true \\\n'
        '        || die "$EX_PRECONDITION" \\',
        'missing_uv_is_a_hard_failure',
        'A missing uv is discovered after the checkout has moved.',
    ),
    Mutation(
        'runtime-probe-removed',
        GATEWAY,
        "        'import mgo.core.config, mgo.api.app' \\",
        "        'pass' \\",
        'runtime_probe_imports_the_application',
        '203/EXEC or an ImportError is diagnosed from the journal instead.',
    ),
    Mutation(
        'sync-drift-ignored',
        GATEWAY,
        '    porcelain="$(git_admin "$admin_account" "$repository" \\\n'
        '        status --porcelain --untracked-files=all)"\n'
        '    [[ -z "$porcelain" ]] \\\n'
        '        || return 1\n'
        '}',
        '    porcelain="$(git_admin "$admin_account" "$repository" \\\n'
        '        status --porcelain --untracked-files=all)"\n'
        '    return 0\n'
        '}',
        'tracked_file_drift_after_a_sync',
        'A sync that changed a tracked file carries on.',
    ),
    Mutation(
        'dirty-tree-allowed',
        GATEWAY,
        '    [[ -z "$porcelain" ]] \\\n'
        '        || die "$EX_PRECONDITION" "the production working tree is not clean"',
        '    true',
        'dirty_working_tree or untracked_file_is_rejected',
        'A fast-forward runs over local modifications.',
    ),
    Mutation(
        'untracked-files-ignored',
        GATEWAY,
        '    porcelain="$(git_admin "$admin_account" "$repository" \\\n'
        '        status --porcelain --untracked-files=all)"\n'
        '    [[ -z "$porcelain" ]] \\\n'
        '        || die "$EX_PRECONDITION" "the production working tree is not clean"',
        '    porcelain="$(git_admin "$admin_account" "$repository" \\\n'
        '        status --porcelain)"\n'
        '    [[ -z "$porcelain" ]] \\\n'
        '        || die "$EX_PRECONDITION" "the production working tree is not clean"',
        'cleanliness_check_includes_untracked or untracked_file_is_rejected',
        'An untracked file shadowing a tracked path is not seen.',
    ),
    Mutation(
        'stash-allowed',
        GATEWAY,
        '    stash="$(git_admin "$admin_account" "$repository" stash list)"\n'
        '    [[ -z "$stash" ]] \\\n'
        '        || die "$EX_PRECONDITION" "the production repository has a stash"',
        '    stash="$(git_admin "$admin_account" "$repository" stash list)"',
        'stash_is_rejected',
        'Hidden work is deployed over.',
    ),
    Mutation(
        'detached-head-allowed',
        GATEWAY,
        '    [[ -n "$current_branch" ]] \\\n'
        '        || die "$EX_PRECONDITION" "the production checkout is in detached '
        'HEAD"',
        '    true',
        'detached_head',
        'A deployment onto no branch at all.',
    ),
    Mutation(
        'wrong-branch-allowed',
        GATEWAY,
        '    [[ "$current_branch" == "$branch" ]] \\\n'
        '        || die "$EX_PRECONDITION" "the production checkout is not on $branch"',
        '    true',
        'wrong_branch',
        'deploy-main stops being main-only.',
    ),
    Mutation(
        'operation-in-progress-allowed',
        GATEWAY,
        '        [[ ! -e "$git_dir/$marker" ]] || return 0\n'
        '    done\n'
        '    return 1',
        '    done\n'
        '    return 1',
        'operation_in_progress',
        'An interrupted merge or rebase is built on.',
    ),
    Mutation(
        'worktree-check-removed',
        GATEWAY,
        '    [[ "$worktrees" -eq 1 ]] \\\n'
        '        || die "$EX_PRECONDITION" \\\n'
        '            "the production repository has an unexpected worktree"',
        '    true',
        'unexpected_worktree',
        'A second worktree shares the object store unnoticed.',
    ),
    Mutation(
        'remote-substring-match',
        GATEWAY,
        '    case "$normalised" in\n'
        '        "https://github.com/$expected" | \\\n'
        '            "ssh://git@github.com/$expected" | \\\n'
        '            "git@github.com:$expected")\n'
        '            return 0\n'
        '            ;;\n'
        '    esac\n'
        '    return 1',
        '    case "$normalised" in\n'
        '        *"$expected"*)\n'
        '            return 0\n'
        '            ;;\n'
        '    esac\n'
        '    return 1',
        'unexpected_remote',
        'A look-alike host passes on a substring.',
    ),
    Mutation(
        'canonical-path-deploy',
        GATEWAY,
        '    canonical="$(cd "$repository" && pwd -P)"\n'
        '    [[ "$canonical" == "$(cd "$repository" && pwd -L)" ]] \\\n'
        '        || die "$EX_PRECONDITION" \\\n'
        '            "the production path resolves through a symlink"\n'
        '\n'
        '    current_branch=',
        '    current_branch=',
        'both_privileged_actions_prove_the_canonical_path',
        'A replaced symlink component points the checks at another tree.',
    ),
    Mutation(
        'canonical-path-restart',
        GATEWAY,
        '    canonical="$(cd "$repository" && pwd -P)"\n'
        '    [[ "$canonical" == "$(cd "$repository" && pwd -L)" ]] \\\n'
        '        || die "$EX_PRECONDITION" \\\n'
        '            "the production path resolves through a symlink"\n'
        '\n'
        '    branch=',
        '    branch=',
        'both_privileged_actions_prove_the_canonical_path',
        'Same, on the restart path.',
    ),
    Mutation(
        'fast-forward-ancestry',
        GATEWAY,
        '    git_admin "$admin_account" "$repository" \\\n'
        '        merge-base --is-ancestor "$head" "$target" \\\n'
        '        || die "$EX_PRECONDITION" \\\n'
        '            "the approved SHA is not a descendant of the deployed commit"',
        '    true',
        'divergent_history or fast_forward_proofs_are_stated',
        'A divergent target is deployed as if it were a descendant.',
    ),
    Mutation(
        'downgrade-allowed',
        GATEWAY,
        '    behind="$(git_admin "$admin_account" "$repository" \\\n'
        '        rev-list --count "$target..$head")"\n'
        '    [[ "$behind" -eq 0 ]] \\\n'
        '        || die "$EX_PRECONDITION" \\\n'
        '            "the approved SHA is behind the deployed commit"',
        '    true',
        'downgrade_is_rejected or fast_forward_proofs_are_stated',
        'Production may be moved backwards.',
    ),
    Mutation(
        'remote-authority-unchecked',
        GATEWAY,
        '    [[ "$remote_sha" == "$approved" ]] \\\n'
        '        || die "$EX_PRECONDITION" \\\n'
        '            "$MGO_REMOTE/$MGO_BRANCH does not match the approved SHA"',
        '    true',
        'proven_before_the_fetch or requires_the_deployed_commit',
        'A remote that moved past the approval is deployed.',
    ),
    Mutation(
        'tracking-ref-unchecked',
        GATEWAY,
        '    [[ "$tracking" == "$approved" ]] \\\n'
        '        || die "$EX_PRECONDITION" \\\n'
        '            "$MGO_REMOTE/$MGO_BRANCH is not the approved SHA after fetching"',
        '    true',
        'tracking_ref_is_verified_after_the_fetch',
        'The fetch result is trusted without being read.',
    ),
    Mutation(
        'merge-not-ff-only',
        GATEWAY,
        'merge --ff-only "$MGO_REMOTE/$MGO_BRANCH"',
        'merge "$MGO_REMOTE/$MGO_BRANCH"',
        'fast_forward_precedes_the_frozen_sync or transaction_opens_before_the_merge',
        'A merge commit appears in production.',
    ),
    Mutation(
        'restart-requires-approved-head',
        GATEWAY,
        '    [[ "$head" == "$approved" ]] \\\n'
        '        || die "$EX_PRECONDITION" "the checked-out commit is not the approved '
        'SHA"',
        '    true',
        'checkout_behind_its_approved_upstream',
        'restart-api restarts something nobody approved.',
    ),
    Mutation(
        'restart-upstream-unchecked',
        GATEWAY,
        '    [[ "$upstream_sha" == "$approved" ]] \\\n'
        '        || die "$EX_PRECONDITION" "$upstream is not the approved SHA"',
        '    true',
        'restart_api_refuses_every_unsafe_checkout',
        'A local branch nobody pushed is restarted.',
    ),
    Mutation(
        'install-action-restored',
        GATEWAY,
        '        show-approval | deploy-main | restart-api) ;;',
        '        show-approval | deploy-main | restart-api | install) ;;',
        'install_action_is_rejected or exposes_exactly_three_public_actions',
        'The action whose name lied is accepted again.',
    ),
    Mutation(
        'extra-arguments-accepted',
        GATEWAY,
        '    [[ "$#" -eq 0 ]] \\\n'
        '        || die "$EX_REQUEST" "this gateway takes no arguments beyond an '
        'action"',
        '    true',
        'extra_arguments_are_rejected',
        'The input surface stops being one word.',
    ),
    Mutation(
        'unsupported-action-accepted',
        GATEWAY,
        '        *)\n'
        '            die "$EX_REQUEST" \\\n'
        '                "unsupported action; expected show-approval, deploy-main or '
        'restart-api"\n'
        '            ;;',
        '        *) ;;',
        'unsupported_action',
        'An unrecognised word falls through the dispatch.',
    ),
    Mutation(
        'lock-blocking',
        GATEWAY,
        '    flock -n "$MGO_LOCK_FD" \\',
        '    flock "$MGO_LOCK_FD" \\',
        'busy_control_plane_refuses_rather_than_waits',
        'A second caller queues behind a transaction it cannot see.',
    ),
    Mutation(
        'lock-not-taken-by-deploy',
        GATEWAY,
        '    acquire_transaction_lock "$MGO_LOCK_FILE"\n'
        '\n'
        '    # 1. approval',
        '    # 1. approval',
        'every_mutating_action_takes_the_lock_first',
        'Two deployments capture the same baseline.',
    ),
    Mutation(
        'lock-not-taken-by-restart',
        GATEWAY,
        '    acquire_transaction_lock "$MGO_LOCK_FILE"\n'
        '\n'
        '    approved=',
        '    approved=',
        'every_mutating_action_takes_the_lock_first',
        "A restart lands inside a deployment's window.",
    ),
    Mutation(
        'lock-object-unvalidated',
        GATEWAY,
        '    require_secure_lock_object "$lock_path" \\\n'
        '        || die "$EX_PRECONDITION" \\\n'
        '            "the deployment lock is not a root-owned 0600 regular file"',
        '    true',
        'insecure_lock_object_is_refused or lock_object_is_validated_before_flock',
        'Any account can deny every deployment and restart.',
    ),
    Mutation(
        'lock-follows-symlink',
        GATEWAY,
        '    [[ ! -L "$lock_path" ]] || return 1\n'
        '    [[ -f "$lock_path" ]] || return 1\n'
        '\n'
        '    ownership="$(stat -c \'%u:%g\' "$lock_path")" || return 1\n'
        '    [[ "$ownership" == "0:0" ]] || return 1\n'
        '\n'
        '    mode="$(stat -c \'%a\' "$lock_path")" || return 1\n'
        '    [[ "$mode" == "600" ]] || return 1',
        '    [[ -f "$lock_path" ]] || return 1\n'
        '\n'
        '    ownership="$(stat -c \'%u:%g\' "$lock_path")" || return 1\n'
        '    [[ "$ownership" == "0:0" ]] || return 1\n'
        '\n'
        '    mode="$(stat -c \'%a\' "$lock_path")" || return 1\n'
        '    [[ "$mode" == "600" ]] || return 1',
        'lock_symlink_refusal_precedes',
        'The lock is taken on whatever the link points at.',
    ),
    Mutation(
        'lock-mode-loose',
        GATEWAY,
        '    [[ "$mode" == "600" ]] || return 1',
        '    [[ -n "$mode" ]] || return 1',
        'insecure_lock_object_is_refused',
        'A readable lock is a denial-of-deployment primitive.',
    ),
    Mutation(
        'lock-clobbers',
        GATEWAY,
        '        umask 0077\n'
        '        set -C\n'
        '        : >"$lock_path"',
        '        : >"$lock_path"',
        'lock_is_created_privately_and_without_clobbering',
        "A first-run loser replaces the winner's inode.",
    ),
    Mutation(
        'installer-lock-replaced',
        INSTALLER,
        '    chmod 0600 "$lock_path"',
        '    rm -f -- "$lock_path"\n'
        '    (umask 0077; : >"$lock_path")',
        'installer_never_replaces_an_insecure_lock_inode',
        "A legitimate holder's lock is dropped by the repair.",
    ),
    Mutation(
        'installer-lock-symlink',
        INSTALLER,
        '    [[ ! -L "$lock_path" ]] || return 1\n'
        '    [[ -f "$lock_path" ]] || return 1\n'
        '\n'
        '    ownership="$(stat -c \'%u:%g\' "$lock_path")" || return 1',
        '    [[ -f "$lock_path" ]] || return 1\n'
        '\n'
        '    ownership="$(stat -c \'%u:%g\' "$lock_path")" || return 1',
        'installer_lock_refuses_a_symlink',
        'Same symlink hole, in the installer.',
    ),
    Mutation(
        'rollback-not-verified',
        GATEWAY,
        '    if [[ "$head" != "$previous_sha" || "$current_branch" != "$branch" \\\n'
        '        || -n "$porcelain" ]]; then\n'
        '        ROLLBACK_STAGE="verification"\n'
        '        return 1\n'
        '    fi',
        '    if false; then\n'
        '        ROLLBACK_STAGE="verification"\n'
        '        return 1\n'
        '    fi',
        'rollback_verifies_rather_than_assumes',
        'A rollback that did not work reports that it did.',
    ),
    Mutation(
        'rollback-stage-lost',
        GATEWAY,
        '    if ! sync_environment "$admin_account" "$repository"; then\n'
        '        ROLLBACK_STAGE="environment"\n'
        '        return 1\n'
        '    fi',
        '    if ! sync_environment "$admin_account" "$repository"; then\n'
        '        return 1\n'
        '    fi',
        'rollback_names_the_stage_that_failed',
        'The operator is told restoration failed, not where.',
    ),
    Mutation(
        'pre-restart-rollback-restarts',
        GATEWAY,
        '    if ! service_is_active "$MGO_SERVICE"; then\n'
        '        die "$EX_ROLLBACK" \\',
        '    restart_service "$MGO_SERVICE"\n'
        '    if ! service_is_active "$MGO_SERVICE"; then\n'
        '        die "$EX_ROLLBACK" \\',
        'pre_restart_rollback_never_restarts or pre_restart_failure_does_not_restart',
        'A failure that disturbed nothing causes an outage.',
    ),
    Mutation(
        'pre-restart-rollback-skips-health',
        GATEWAY,
        '    if ! endpoint_is_ok "$MGO_HEALTH_URL"; then\n'
        '        die "$EX_ROLLBACK" \\\n'
        '            "deployment failed and health does not answer 200; production was '
        'NOT restored"\n'
        '    fi',
        '    true',
        'pre_restart_rollback_proves_the_service_still_serves',
        'A surviving process is mistaken for a serving one.',
    ),
    Mutation(
        'post-restart-rollback-loops',
        GATEWAY,
        '    if ! restart_service "$MGO_SERVICE"; then\n'
        '        die "$EX_ROLLBACK" \\\n'
        '            "deployment failed and the rollback restart failed; production '
        'was NOT restored"\n'
        '    fi',
        '    while ! restart_service "$MGO_SERVICE"; do :; done',
        'rollback_does_not_loop',
        'One failed deployment becomes a flapping service.',
    ),
    Mutation(
        'rollback-exit-code-merged',
        GATEWAY,
        'readonly EX_ROLLBACK=78',
        'readonly EX_ROLLBACK=70',
        'failed_rollback_uses_a_distinct_high_severity_code',
        '"Not restored" stops being distinguishable from "restored".',
    ),
    Mutation(
        'final-verification-removed',
        GATEWAY,
        '    if ! final_verification "$approved" "$previous_preview"; then\n'
        '        fail_after_restart "final verification failed" \\\n'
        '            "$head" "$previous_preview"\n'
        '    fi',
        '    true',
        'runs_after_preview_restoration or no_success_is_printed',
        'The deployment claims success nothing proved.',
    ),
    Mutation(
        'final-verification-skips-approval',
        GATEWAY,
        '    [[ "$reapproved" == "$approved" ]] \\\n'
        '        || { warn "final check: the approval no longer names this commit"; '
        'return 1; }',
        '    true',
        'final_verification_refuses_every_wrong_end_state',
        'The authority may have changed during the deployment.',
    ),
    Mutation(
        'final-verification-preview',
        GATEWAY,
        '        [[ "$current" != "running" ]] \\\n'
        '            || { warn "final check: preview is running but was '
        '$expected_preview"; return 1; }',
        '        true',
        'final_verification_refuses_a_preview_that_should_not_be_running',
        'A camera nobody asked for is left running.',
    ),
    Mutation(
        'fast-forward-verification-removed',
        GATEWAY,
        '    if ! verify_after_fast_forward "$approved"; then\n'
        '        fail_before_restart "the checkout is wrong after the fast-forward" '
        '\\\n'
        '            "$head" "$previous_pid" "$previous_timestamp" '
        '"$previous_preview"\n'
        '    fi',
        '    true',
        'checkout_is_verified_immediately_after_the_merge',
        'The environment is built on an unverified checkout.',
    ),
    Mutation(
        'merge-failure-not-transactional',
        GATEWAY,
        '    if ! git_admin "$MGO_ADMIN_ACCOUNT" "$MGO_REPOSITORY" \\\n'
        '        merge --ff-only "$MGO_REMOTE/$MGO_BRANCH"; then\n'
        '        fail_before_restart "the fast-forward failed" \\\n'
        '            "$head" "$previous_pid" "$previous_timestamp" '
        '"$previous_preview"\n'
        '    fi',
        '    git_admin "$MGO_ADMIN_ACCOUNT" "$MGO_REPOSITORY" \\\n'
        '        merge --ff-only "$MGO_REMOTE/$MGO_BRANCH" \\\n'
        '        || die "$EX_DEPLOY" "the fast-forward failed"',
        'failed_fast_forward_enters_the_rollback or post_mutation_failure',
        'A part-way merge is left in production.',
    ),
    Mutation(
        'installer-stale-ignored',
        INSTALLER,
        '    if [[ "$transaction_state" -eq 2 ]]; then\n'
        '        warn "stale transaction state is present in $transaction: a previous '
        'run did not complete its cleanup"\n'
        '        warn "it is preserved for inspection; remove it deliberately before '
        'installing again"\n'
        '        return "$EX_STALE"\n'
        '    fi',
        '    true',
        'stale_transaction',
        '"Cleanup failed" is reported as "verified; nothing changed".',
    ),
    Mutation(
        'installer-stale-after-idempotent',
        INSTALLER,
        '    # Before any conclusion about the installed targets, including the\n'
        '    # comfortable one. Stale transaction state means a previous run did not\n'
        '    # finish, and "both targets are correct" is not an answer to that.\n'
        '    if [[ "$transaction_state" -eq 1 ]]; then',
        '    if [[ "$gateway_current" -eq 1 && "$sudoers_current" -eq 1 ]]; then\n'
        'log "gateway and sudoers policy are already installed, correct and valid"\n'
        '        log "verified; nothing changed"\n'
        '        return 0\n'
        '    fi\n'
        '\n'
        '    if [[ "$transaction_state" -eq 1 ]]; then',
        'stale_check_precedes_the_idempotent_return or stale_transaction',
        'The idempotent return happens before anything looks.',
    ),
    Mutation(
        'installer-shared-workspace',
        INSTALLER,
        '    workspace="$(open_transaction_workspace "$transaction")" || {',
        '    workspace="$transaction" || {',
        'leaves_no_transaction_artefacts',
        'Every run works in — and then deletes — the shared parent.',
    ),
    Mutation(
        'installer-workspace-not-unique',
        INSTALLER,
        '    mktemp -d --tmpdir="$parent" "run.XXXXXX"',
        '    mkdir -p "$parent/run" && printf \'%s\\n\' "$parent/run"',
        'each_run_gets_its_own_workspace',
        'Two runs share one workspace name.',
    ),
    Mutation(
        'installer-deletes-unknown-state',
        INSTALLER,
        '    [[ -n "$workspace" ]] || return 1\n'
        '    [[ -d "$workspace" ]] || return 0\n'
        '    rm -rf -- "$workspace" || return 1',
        '    [[ -n "$workspace" ]] || return 1\n'
        '    [[ -d "$workspace" ]] || return 0\n'
        '    rm -rf -- "$(dirname "$workspace")"/* || return 1',
        'run_never_removes_another',
        "One run destroys another run's recovery evidence.",
    ),
    Mutation(
        'installer-parent-owner',
        INSTALLER,
        '    ownership="$(stat -c \'%u:%g\' "$parent")" || return 1\n'
        '    [[ "$ownership" == "0:0" ]] || return 1',
        '    ownership="$(stat -c \'%u:%g\' "$parent")" || return 1',
        'transaction_parent_must_be_root_owned',
        'An unprivileged account chooses what a rollback restores.',
    ),
    Mutation(
        'installer-parent-mode',
        INSTALLER,
        '    mode="$(stat -c \'%a\' "$parent")" || return 1\n'
        '    [[ "$mode" == "700" ]]',
        '    mode="$(stat -c \'%a\' "$parent")" || return 1\n'
        '    [[ -n "$mode" ]]',
        'transaction_parent_must_be_private',
        'The previous sudoers policy becomes readable.',
    ),
    Mutation(
        'installer-parent-symlink',
        INSTALLER,
        '    [[ ! -L "$parent" ]] || return 1\n'
        '    if [[ ! -e "$parent" ]]; then\n'
        '        return 0\n'
        '    fi',
        '    if [[ ! -e "$parent" ]]; then\n'
        '        return 0\n'
        '    fi',
        'transaction_parent_symlink_is_refused',
        'Backups go wherever the link points.',
    ),
    Mutation(
        'installer-cleanup-failure-ignored',
        INSTALLER,
        '    if ! close_transaction_workspace "$workspace"; then\n'
        '        warn "the transaction directory could not be removed"\n'
        '        return "$EX_FAILED"\n'
        '    fi\n'
        '    return 0',
        '    close_transaction_workspace "$workspace" || true\n'
        '    return 0',
        'cleanup_failure_after_success',
        'A run that left the previous policy on disk claims to have finished.',
    ),
    Mutation(
        'installer-restore-one-target',
        INSTALLER,
        '    restore_one "$gateway_target" "$gateway_backup" || failures=$((failures + '
        '1))\n'
        '    restore_one "$sudoers_target" "$sudoers_backup" || failures=$((failures + '
        '1))',
        '    restore_one "$gateway_target" "$gateway_backup" || failures=$((failures + '
        '1))',
        'also_restores_the_gateway or restores_the_previous_pair',
        'Half a control plane is left behind.',
    ),
    Mutation(
        'installer-restore-failure-silent',
        INSTALLER,
        '    if ! restore_pair "$@"; then\n'
        '        warn "installation failed AND restoration failed; the host was NOT '
        'restored"\n'
        '        return "$EX_RESTORE_FAILED"\n'
        '    fi',
        '    restore_pair "$@" || true',
        'failed_restoration_is_reported_distinctly',
        'A host that was not restored is reported as one that was.',
    ),
    Mutation(
        'installer-no-post-install-verification',
        INSTALLER,
        'if ! verify_installed "$gateway_target" "$gateway_sum" "$gateway_mode"; then',
        '    if false; then',
        'gateway-only',
        'What landed is never compared with what was meant to.',
    ),
    Mutation(
        'installer-policy-not-revalidated',
        INSTALLER,
        '    if ! policy_is_valid "$sudoers_target"; then',
        '    if false; then',
        'installer_failure_restores_the_previous_pair',
        'An invalid policy under /etc/sudoers.d locks everyone out.',
    ),
    Mutation(
        'installer-metadata-ignored',
        INSTALLER,
        '    actual_meta="$(file_metadata "$target")" || return 1\n'
        '    [[ "$actual_meta" == "0:0:${expected_mode#0}" ]]',
        '    actual_meta="$(file_metadata "$target")" || return 1\n'
        '    [[ -n "$actual_meta" ]]',
        'matching_content_with_wrong_metadata',
        'Right bytes with the wrong owner counts as installed.',
    ),
    Mutation(
        'installer-target-type',
        INSTALLER,
        '    [[ -e "$path" || -L "$path" ]] || return 0\n'
        '    is_regular_file "$path"',
        '    [[ -e "$path" || -L "$path" ]] || return 0\n'
        '    true',
        'unsafe_file_type_is_refused or directory_where_a_target_belongs',
        'A directory or link where a target belongs is replaced.',
    ),
    Mutation(
        'installer-source-syntax-unchecked',
        INSTALLER,
        '    if ! bash -n "$gateway_source"; then\n'
        '        warn "the gateway failed its shell syntax check"\n'
        '        return "$EX_FAILED"\n'
        '    fi',
        '    true',
        'validation_precedes_every_mutation',
        'A syntactically broken gateway is installed as root.',
    ),
    Mutation(
        'installer-visudo-optional',
        INSTALLER,
        '    if ! command -v visudo >/dev/null 2>&1; then\n'
        '        warn "visudo is not available; refusing to proceed without '
        'validation"\n'
        '        return "$EX_FAILED"\n'
        '    fi',
        '    true',
        'fails_closed_without_visudo',
        'An unvalidated policy is written under /etc/sudoers.d.',
    ),
    Mutation(
        'installer-root-not-required',
        INSTALLER,
        '    if [[ "$dry_run" -eq 0 && "$effective_uid" -ne 0 ]]; then',
        '    if [[ "$dry_run" -eq 0 && "$effective_uid" -lt 0 ]]; then',
        'installer_requires_root_for_a_real_run',
        'A non-root run proceeds and fails halfway.',
    ),
    Mutation(
        'installer-lock-not-taken',
        INSTALLER,
        '        acquire_transaction_lock "$lock_path" || lock_outcome="$?"',
        '        lock_outcome=0',
        'installer_takes_the_lock_before_inspecting or busy_installer',
        'The file a running deploy-main is executing from is replaced.',
    ),
    Mutation(
        'installer-busy-code',
        INSTALLER,
        'readonly EX_BUSY=75',
        'readonly EX_BUSY=1',
        'busy_installer_reports_the_shared_exit_code',
        'Busy stops being distinguishable from failed.',
    ),
    Mutation(
        'installer-rewrites-current',
        INSTALLER,
        '    if [[ "$gateway_current" -eq 1 && "$sudoers_current" -eq 1 ]]; then\n'
        '        log "gateway and sudoers policy are already installed, correct and '
        'valid"\n'
        '        log "verified; nothing changed"\n'
        '        return 0\n'
        '    fi',
        '    if false; then\n'
        '        log "gateway and sudoers policy are already installed, correct and '
        'valid"\n'
        '        log "verified; nothing changed"\n'
        '        return 0\n'
        '    fi',
        'fully_current_installation_mutates_nothing or clean_idempotent_run',
        'Correct files are rewritten for nothing.',
    ),
    Mutation(
        'installer-backup-loses-metadata',
        INSTALLER,
        '    if ! cp --preserve=all --no-dereference "$target" "$backup"; then',
        '    if ! cp "$target" "$backup"; then',
        'restoration_preserves_numeric_owner_group_and_mode',
        'Restoring an approximation of the file is not restoring it.',
    ),
    Mutation(
        'installer-temporary-not-removed',
        INSTALLER,
        '    if ! mv -f "$temporary" "$target"; then\n'
        '        rm -f "$temporary"\n'
        '        return 1\n'
        '    fi',
        '    if ! mv -f "$temporary" "$target"; then\n'
        '        return 1\n'
        '    fi',
        'install_file_removes_its_temporary_file_on_failure',
        'A half-written sibling is left beside the target.',
    ),
    Mutation(
        'wrapper-fallback',
        GATEWAY,
        'readonly MGO_REPOSITORY="/opt/garden-observatory"',
        'readonly MGO_REPOSITORY="${MGO_REPOSITORY_OVERRIDE:-/opt/garden-observatory}"',
        'accepts_no_caller_supplied_production_value or falls_back_to_the_environment',
        'A fixed production constant becomes tunable.',
    ),
)
