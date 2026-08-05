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
SUDOERS = "scripts/deploy/mgo-validate.sudoers"
WRAPPER = "scripts/deploy/update-main.sh"

#: The suite itself is a shipped asset for this purpose. A test module that
#: executes entry points is a program that runs on the host under test, and the
#: isolation that keeps it off the control plane is code like any other: it can
#: be weakened, and nothing would notice unless something fails when it is.
#:
#: Every mutation of this asset is targeted at a test that fails *before* the
#: weakened isolation could be used — either a static audit of the module's own
#: AST, or a harness guard that refuses to start a child process. None of them
#: reaches a real sudo or an installed path, which is the whole point.
TESTS = "tests/test_deployment_gateway.py"

#: The two records that carry the staging-incident facts. A record is mutated
#: here for one narrow reason: the 2026-08-01 `install` failure and the
#: 2026-08-04 test escape both ended with production untouched, for different
#: reasons, and the first version of these documents borrowed the first event's
#: explanation for the second. A distinction a document merely states is a
#: distinction nothing keeps.
REMEDIATION_RECORD = "docs/tasks/Task-012-Deployment-Gateway-Remediation.md"
ACCEPTANCE_RECORD = "docs/tasks/Task-012-Physical-Camera-Acceptance.md"

#: The operator-facing gateway document. It now carries a production
#: installation record, and the one claim in it that must never soften is that
#: the retired wildcard policy stays retired.
DEPLOYMENT_DOC = "docs/Deployment-Gateway.md"


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
        '#!/bin/bash -p\n',
        '#!/usr/bin/env bash\n',
        'fixed_interpreter',
        "The interpreter is chosen by the caller's PATH.",
    ),
    Mutation(
        'installer-shebang',
        INSTALLER,
        '#!/bin/bash -p\n',
        '#!/usr/bin/env bash\n',
        'fixed_interpreter',
        'Same, for the installer.',
    ),
    Mutation(
        'gateway-unprivileged-shebang',
        GATEWAY,
        '#!/bin/bash -p\n',
        '#!/bin/bash\n',
        'privileged_bash',
        'BASH_ENV runs before the first statement, unopposed.',
    ),
    Mutation(
        'installer-unprivileged-shebang',
        INSTALLER,
        '#!/bin/bash -p\n',
        '#!/bin/bash\n',
        'privileged_bash',
        'Same, for the installer.',
    ),
    Mutation(
        'gateway-unprivileged-reexec',
        GATEWAY,
        '            /bin/bash -p "$0" "$@"',
        '            /bin/bash "$0" "$@"',
        'privileged_bash or environment_boundary_is_an_allowlist',
        'The operational process is the weaker of the two.',
    ),
    Mutation(
        'installer-unprivileged-reexec',
        INSTALLER,
        '            /bin/bash -p "$0" "$@"',
        '            /bin/bash "$0" "$@"',
        'privileged_bash or environment_boundary_is_an_allowlist',
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
        # Deliberately the direct post-startup test, not the entry-path one.
        # The entry path hands PATH in through the process environment, and a
        # host that rewrites it makes the function return at its first guard --
        # so on Windows the entry-path test passes with the allowlist removed.
        # This selector names the test that reaches the loop on every platform.
        'environment_allowlist_rejects_an_unknown_variable_after_shell_startup',
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
        '            /bin/bash -p "$0" "$@"',
        '            bash -p "$0" "$@"',
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
        'installer-dry-run-stale-exits-zero',
        INSTALLER,
        '        # The code the real installation would exit with, so a wrapper '
        'reading\n'
        '        # the status learns the same thing from either mode.\n'
        '        outcome="$EX_STALE"',
        '        log "dry run: would refuse"',
        'dry_run_reports_stale_state',
        'A validation command answers "fine" for a state that refuses.',
    ),
    Mutation(
        'installer-dry-run-always-succeeds',
        INSTALLER,
        '    warn "dry run complete; nothing was changed, and the installation '
        'would refuse"\n'
        '    return "$outcome"',
        '    warn "dry run complete; nothing was changed, and the installation '
        'would refuse"\n'
        '    return 0',
        'dry_run',
        'Every dry run exits zero, whatever it just reported.',
    ),
    Mutation(
        'installer-parent-inspection-failure-ignored',
        INSTALLER,
        '    entries="$(find "$parent" -mindepth 1 -maxdepth 1 -print -quit)" '
        '|| return 1',
        '    entries="$(find "$parent" -mindepth 1 -maxdepth 1 -print -quit '
        '2>/dev/null || true)"',
        'cannot_be_inspected_fails_closed or uninspectable',
        'An unreadable transaction parent reads as an empty one.',
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
        '    rm -rf -- "$workspace" || return 1',
        '    rm -rf -- "$(dirname "$workspace")"/* || return 1',
        'run_never_removes_another',
        "One run destroys another run's recovery evidence.",
    ),
    Mutation(
        'installer-cleanup-succeeds-with-leftovers',
        INSTALLER,
        '    [[ ! -L "$workspace" ]] || return 1\n'
        '    [[ -d "$workspace" ]] || return 1\n'
        '    rm -rf -- "$workspace" || return 1\n'
        '    [[ ! -e "$workspace" && ! -L "$workspace" ]]',
        '    [[ -d "$workspace" ]] || return 0\n'
        '    rm -rf -- "$workspace" || return 1\n'
        '    return 0',
        'cleanup_does_not_report_success',
        'A surviving object is reported as a finished cleanup.',
    ),
    Mutation(
        'installer-workspace-unverified',
        INSTALLER,
        '    require_secure_workspace "$workspace" "$transaction" || {',
        '    true || {',
        'workspace_that_is_not_root_owned',
        'The sudoers policy is copied into a directory nobody checked.',
    ),
    Mutation(
        'installer-workspace-owner-unverified',
        INSTALLER,
        '    ownership="$(stat -c \'%u:%g\' "$workspace")" || return 1\n'
        '    [[ "$ownership" == "0:0" ]] || return 1',
        '    ownership="$(stat -c \'%u:%g\' "$workspace")" || return 1',
        'workspace_that_is_not_root_owned',
        'An unprivileged account owns the staging directory.',
    ),
    Mutation(
        'installer-workspace-mode-unverified',
        INSTALLER,
        '    mode="$(stat -c \'%a\' "$workspace")" || return 1\n'
        '    [[ "$mode" == "700" ]]',
        '    mode="$(stat -c \'%a\' "$workspace")" || return 1\n'
        '    [[ -n "$mode" ]]',
        'workspace_that_is_not_root_owned',
        'A world-readable staging directory exposes the sudoers policy.',
    ),
    Mutation(
        'installer-workspace-location-unverified',
        INSTALLER,
        '    [[ "$workspace" == "$parent"/* ]] || return 1',
        '    true',
        'workspace_outside_the_transaction_parent',
        'A workspace somewhere else is accepted as this run\'s.',
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
        '    gateway_syntax_is_valid "$staged_gateway" || {\n'
        '        warn "the staged gateway failed its shell syntax check"\n'
        '        close_transaction_workspace "$workspace" || true\n'
        '        return "$EX_FAILED"\n'
        '    }',
        '    true',
        'validation_precedes_every_mutation or invalid_staged_asset',
        'A syntactically broken gateway is installed as root.',
    ),
    Mutation(
        'installer-installed-syntax-unchecked',
        INSTALLER,
        '    if ! gateway_syntax_is_valid "$gateway_target"; then',
        '    if false; then',
        'installed_gateway_that_does_not_parse',
        'A published gateway that Bash cannot parse is never invoked again.',
    ),
    Mutation(
        'installer-installs-the-live-source',
        INSTALLER,
        '    if ! install_file "$staged_gateway" "$gateway_target" '
        '"$gateway_mode"; then',
        '    if ! install_file "$gateway_source" "$gateway_target" '
        '"$gateway_mode"; then',
        'changing_a_source or staged_snapshot',
        'The bytes installed are not the bytes validated.',
    ),
    Mutation(
        'installer-installs-the-live-policy',
        INSTALLER,
        '    if ! install_file "$staged_sudoers" "$sudoers_target" '
        '"$sudoers_mode"; then',
        '    if ! install_file "$sudoers_source" "$sudoers_target" '
        '"$sudoers_mode"; then',
        'changing_a_source or staged_snapshot',
        'Same, for the sudoers policy.',
    ),
    Mutation(
        'installer-staged-file-unverified',
        INSTALLER,
        '    require_secure_staged_file "$staged_gateway" "$workspace" \\\n'
        '        && require_secure_staged_file "$staged_sudoers" "$workspace" || {',
        '    true || {',
        'staged',
        'A staged copy nobody checked becomes the installed bytes.',
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
        '    acquire_transaction_lock "$lock_path" || lock_outcome="$?"',
        '    lock_outcome=0',
        'installer_takes_the_lock_before_inspecting or busy_installer',
        'The file a running deploy-main is executing from is replaced.',
    ),
    Mutation(
        'installer-validates-before-the-lock',
        INSTALLER,
        '    # --- the lock comes first --------------------------------------'
        '----------',
        '    gateway_syntax_is_valid "$gateway_source" || return "$EX_FAILED"\n'
        '    file_checksum "$gateway_source" >/dev/null || return "$EX_FAILED"\n'
        '    # --- the lock comes first --------------------------------------'
        '----------',
        'lock_is_taken_before_any_source_is_validated',
        'A concurrent deployment can move the checkout mid-validation.',
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
        '    if target_is_current "$gateway_target" "$gateway_sum" "$gateway_mode" \\\n'
        '        && target_is_current "$sudoers_target" "$sudoers_sum" '
        '"$sudoers_mode" \\\n'
        '        && policy_is_valid "$sudoers_target"; then',
        '    if false; then',
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
    Mutation(
        'documented-sudo-bash-invocation',
        WRAPPER,
        "    printf '  sudo ./scripts/deploy/install-mgo-validate.sh\\n' >&2",
        "    printf '  sudo bash scripts/deploy/install-mgo-validate.sh\\n' >&2",
        'unprivileged_wrapper_does_no_privileged_work',
        'The documented command discards the shebang, and privileged mode.',
    ),
    Mutation(
        'sudoers-setenv',
        SUDOERS,
        'claude ALL=(root) NOPASSWD: MGO_VALIDATE\n',
        'claude ALL=(root) NOPASSWD: SETENV: MGO_VALIDATE\n',
        'grants_no_setenv',
        'sudo VAR=value hands back everything env_reset removes.',
    ),
    Mutation(
        'sudoers-no-env-reset',
        SUDOERS,
        'Defaults!MGO_VALIDATE env_reset\n',
        '\n',
        'resets_the_environment_for_this_command',
        "The command's environment depends on the rest of the host's policy.",
    ),
    Mutation(
        'sudoers-keeps-bash-env',
        SUDOERS,
        'Defaults!MGO_VALIDATE env_delete += "BASH_ENV ENV SHELLOPTS BASHOPTS"\n',
        '\n',
        'resets_the_environment_for_this_command',
        'The shell-startup variables stop being named at the sudo boundary.',
    ),
    Mutation(
        'sudoers-keeps-loader-variables',
        SUDOERS,
        'Defaults!MGO_VALIDATE env_delete += "LD_PRELOAD LD_LIBRARY_PATH LD_AUDIT"\n',
        '\n',
        'resets_the_environment_for_this_command',
        'The one class of variable no shell script can defend against.',
    ),
    Mutation(
        'sudoers-widens-the-command',
        SUDOERS,
        'Cmnd_Alias MGO_VALIDATE = /usr/local/sbin/mgo-validate\n',
        'Cmnd_Alias MGO_VALIDATE = /usr/local/sbin/\n',
        'grants_one_account_one_path or grants_nothing_else',
        'A directory prefix grants every executable inside it.',
    ),
    # --- the wrapper's delegation contract, executed -------------------------
    #
    # Both are caught only by the isolated execution test. The static
    # `"exec sudo" in body` check that used to stand for this contract passes
    # against either of them.
    Mutation(
        'wrapper-delegates-a-different-action',
        WRAPPER,
        'exec sudo -n "$GATEWAY" deploy-main',
        'exec sudo -n "$GATEWAY" restart-api',
        'wrapper_hands_the_gateway_its_exit_code',
        'The exact argument vector stops being the argument vector.',
    ),
    Mutation(
        'wrapper-discards-the-gateway-exit-code',
        WRAPPER,
        'exec sudo -n "$GATEWAY" deploy-main',
        'sudo -n "$GATEWAY" deploy-main || true',
        'wrapper_hands_the_gateway_its_exit_code',
        'A summarising wrapper reports a success the gateway did not.',
    ),
    # --- the suite's own host boundary --------------------------------------
    #
    # These mutate the test module. Each is detected by a test that fails
    # before the weakened isolation is used: the first is a static audit of the
    # module's AST, and the rest trip a harness guard that runs before any
    # child process is started. None of them can reach a real sudo.
    Mutation(
        'harness-executes-the-tracked-wrapper',
        TESTS,
        '        [_bash(), _posix(harness.wrapper)],',
        '        [_bash(), str(UPDATE_MAIN)],',
        'no_test_can_reach_the_host_control_plane',
        'The harness executes the tracked wrapper, not its disposable copy.',
    ),
    Mutation(
        'harness-keeps-the-production-gateway-constant',
        TESTS,
        '    text = source.replace(PRODUCTION_GATEWAY_CONSTANT, replacement)',
        '    text = source',
        'wrapper_reports_a_missing_gateway or wrapper_hands_the_gateway_its_exit_code',
        'The disposable copy still names the installed gateway.',
    ),
    Mutation(
        'harness-drops-the-fake-sudo-path-isolation',
        TESTS,
        '    environment["PATH"] = (\n'
        '        _posix(harness.binaries) + os.pathsep + environment.get("PATH", "")\n'
        '    )',
        '    environment["PATH"] = environment.get("PATH", "")',
        'wrapper_reports_a_missing_gateway or wrapper_hands_the_gateway_its_exit_code',
        "The fake sudo stops being ahead of the host's real one.",
    ),
    Mutation(
        'missing-gateway-case-lets-sudo-run',
        TESTS,
        '    harness = _disposable_wrapper(tmp_path, gateway_present=False)',
        '    harness = _disposable_wrapper(tmp_path, gateway_present=True)',
        'wrapper_reports_a_missing_gateway',
        'The missing-gateway case stops being the case with no gateway.',
    ),
    Mutation(
        'host-escape-audit-registers-no-executor',
        TESTS,
        '        "run_bash",\n'
        '        "call_gateway_function",\n'
        '        "run_isolated_wrapper",\n',
        '',
        'host_escape_audit_is_looking_at_something',
        'The audit stops watching the callables this suite executes through.',
    ),
    Mutation(
        'host-escape-audit-permits-the-tracked-wrapper',
        TESTS,
        'UNEXECUTABLE_NAMES = frozenset({"UPDATE_MAIN"})',
        'UNEXECUTABLE_NAMES = frozenset()',
        'host_escape_audit_is_looking_at_something',
        'Direct subprocess execution of UPDATE_MAIN stops being a finding.',
    ),
    Mutation(
        'host-escape-audit-permits-the-installed-gateway',
        TESTS,
        '    "/usr/local/sbin/mgo-validate",\n    "/etc/sudoers.d",',
        '    "/etc/sudoers.d",',
        'host_escape_audit_is_looking_at_something',
        'An executed /usr/local/sbin/mgo-validate command stops being a finding.',
    ),
    # --- the two staging incidents stay apart -------------------------------
    #
    # Each mutation restores the explanation the record originally gave: that
    # the escaped deploy-main request was refused by the task-010 branch pin.
    # It was not — the installed gateway had no such action. Both events end
    # with production untouched, which is what made the wrong reason readable
    # as the right one.
    Mutation(
        'record-borrows-the-install-failure-mechanism',
        REMEDIATION_RECORD,
        # Single-line anchors: these records are CRLF in a Windows working
        # tree and LF elsewhere, and a multi-line `old` would match on one host
        # and go stale on the other.
        'still the **legacy Task 10 gateway**, whose supported actions are',
        'still the **Task 10** gateway, which is pinned to `task-010-operations`,'
        ' and the checkout is on `main`, so its `deploy-main` failed its own'
        ' precondition. Its supported actions are',
        'two_gateway_events_are_not_conflated or staging_escape_is_recorded',
        "Event B is explained by Event A's branch precondition again.",
    ),
    Mutation(
        'acceptance-record-borrows-the-install-failure-mechanism',
        ACCEPTANCE_RECORD,
        'request because it does not implement `deploy-main` at all ',
        'request because it is pinned to `task-010-operations` and the checkout'
        ' is on `main` ',
        'camera_record_does_not_conflate or task_record_states_the_installation',
        'The acceptance summary reverts to the branch-precondition explanation.',
    ),
    # --- the installation record cannot drift ------------------------------
    #
    # These three records are now the only account of what happened on the
    # Raspberry Pi on 2026-08-04 and 2026-08-05. Nothing in this repository can
    # re-derive them, so each mutation restores a plausible earlier or easier
    # version of the story and must be caught.
    #
    # Single-line anchors throughout: these records are CRLF in a Windows
    # working tree and LF elsewhere, and a multi-line `old` would match on one
    # host and go stale on the other.
    Mutation(
        'installation-status-reverts-to-not-performed',
        REMEDIATION_RECORD,
        '| Installation on the Raspberry Pi | **Passed**',
        '| Installation on the Raspberry Pi | **Not performed**',
        'remediation_record_states_the_installation_truthfully',
        'The installed gateway silently becomes an uninstalled one again.',
    ),
    Mutation(
        'retired-wildcard-described-as-still-active',
        REMEDIATION_RECORD,
        'the wildcard grant was **not** restored',
        'the wildcard grant is still active',
        'legacy_policy_retirement_is_recorded',
        'The retired wildcard grant is described as active on the host.',
    ),
    Mutation(
        'gateway-doc-permits-restoring-the-wildcard-policy',
        DEPLOYMENT_DOC,
        'it must not be restored',
        'it may be restored if a deployment needs it',
        'deployment_document_records_the_production_installation',
        'The operator document stops forbidding the wildcard policy revival.',
    ),
    Mutation(
        'baseline-change-attributed-to-the-installation',
        REMEDIATION_RECORD,
        '**The power failure, not the gateway installation, is what changed the',
        '**The gateway installation is what changed the',
        'power_failure_not_the_installation_explains_the_baseline_change',
        'A reboot\'s MainPID and preview change is blamed on the installation.',
    ),
)
