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

#: The suite a mutation's ``tests`` selector is applied to, unless the entry
#: names another. Every gateway entry below predates this field and keeps this
#: default, so adding it changed no existing mutation.
GATEWAY_SUITE = "tests/test_deployment_gateway.py"


class Mutation(NamedTuple):
    """One deliberate defect and the tests that must catch it.

    ``tests`` is a ``pytest -k`` selector; ``suite`` is the whitespace-separated
    list of test modules it is applied to. ``suite`` has a default because this
    register began as the deployment gateway's alone: an entry that does not
    name a suite is a gateway entry, exactly as it was.
    """

    identifier: str
    asset: str
    old: str
    new: str
    tests: str
    note: str
    suite: str = GATEWAY_SUITE


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

#: Task 13.1 application sources. These are not shipped shell assets, but the
#: register's purpose is the same for them: a motion-triggered capture that
#: quietly stops being bounded, stops being counted, or stops going through the
#: one camera owner would keep passing every status check it has.
EVENT_CAPTURE_SERVICE = "src/mgo/event_capture/service.py"
CAPTURE_WORKFLOW = "src/mgo/captures/workflow.py"
APPLICATION = "src/mgo/api/app.py"

#: The suites that own those behaviours.
EVENT_CAPTURE_SUITE = "tests/test_event_capture.py"
CAPTURE_WORKFLOW_SUITE = "tests/test_capture_workflow.py"
APPLICATION_SUITE = "tests/test_app_routes.py"

#: Task 14.1 retention sources. Retention is the only subsystem in MGO that
#: deletes anything, and every safety property it has is a *refusal*: a check
#: that stops a deletion. A refusal is invisible while it works, so a weakened
#: one changes no status endpoint, no counter and no observation until the day
#: it removes the wrong file. That is exactly what a register is for.
#:
#: Every `old` below is a single line. The working tree checks out as CRLF on
#: Windows while this register is pinned to LF by .gitattributes, so a
#: multi-line anchor would stop matching on a developer machine and report a
#: stale mutation for code that never changed.
RETENTION_POLICY = "src/mgo/retention/policy.py"
RETENTION_SERVICE = "src/mgo/retention/service.py"
RETENTION_REPOSITORY = "src/mgo/retention/repository.py"
RETENTION_MODELS = "src/mgo/retention/models.py"

#: The observation engine, which retention now shares rather than copies. The
#: point of the shared helper is that there is one validation path and one
#: INSERT; a mutation there must be caught by a *retention* test, or the sharing
#: is decorative.
OBSERVATIONS = "src/mgo/core/observations.py"

#: The suites that own the retention behaviours.
RETENTION_POLICY_SUITE = "tests/test_retention_policy.py"

#: The Task 14.2 operator command. Every safety property it has is a *refusal*
#: standing between an operator and a deletion, and a refusal is invisible while
#: it works: a weakened gate changes no output at all until the run it should
#: have stopped goes ahead.
RETENTION_CLI = "src/mgo/retention/cli.py"

#: The suites that own the operator interface and the read-only boundary.
RETENTION_CLI_SUITE = "tests/test_retention_cli.py"
RETENTION_READONLY_SUITE = "tests/test_retention_readonly.py"
RETENTION_SERVICE_SUITE = "tests/test_retention_service.py"
RETENTION_DATABASE_SUITE = "tests/test_retention_database.py"
RETENTION_API_SUITE = "tests/test_retention_api.py"

#: The migration that creates the media-lifecycle table. It is a shipped asset
#: in exactly the sense the gateway scripts are: a one-word weakening of its
#: CREATE statement lets a database claim version 3 over a table this file never
#: created, and every test that builds a *fresh* database still passes.
MIGRATION_003 = "migrations/003_capture_media_lifecycle.sql"

#: The schema-migration runner, which now verifies the version-3 table's
#: constraints rather than only its column names.
DATABASE = "src/mgo/core/database.py"

#: The suite that owns migration and legacy-adoption behaviour.
MIGRATIONS_SUITE = "tests/test_database_migrations.py"


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
    # --- the live validations cannot regress -------------------------------
    #
    # Both actions have now moved production for real, once each. The evidence
    # exists nowhere but this record, so a status that quietly reverts to
    # "pending" would erase a completed production validation, and a baseline
    # relabelled onto the telemetry table would reinstate exactly the
    # conflation that nearly aborted the deployment.
    # Anchored inside each live section rather than on its gate-table row: the
    # tests that own these facts slice the section, so a row-only mutation
    # would go undetected by them and the register would be measuring the
    # wrong thing.
    Mutation(
        'live-deploy-main-unexercised',
        REMEDIATION_RECORD,
        '**Passed on 2026-08-05.** Preflight began 15:04:39 SAST,'
        ' the deployment ran at',
        '**Not yet exercised.** Preflight began 15:04:39 SAST,'
        ' the deployment ran at',
        'live_deploy_main_validation_is_recorded',
        'A completed production deployment reverts to pending.',
    ),
    Mutation(
        'live-restart-api-unexercised',
        REMEDIATION_RECORD,
        '**Passed on 2026-08-05.** Preflight began 15:30:53 SAST,'
        ' the restart ran at',
        '**Not yet exercised.** Preflight began 15:30:53 SAST,'
        ' the restart ran at',
        'live_restart_api_validation_is_recorded',
        'A completed production restart reverts to pending.',
    ),
    Mutation(
        'capture-baseline-labelled-observations',
        REMEDIATION_RECORD,
        '**The stable eight-record baseline is the capture catalogue.**',
        '**The stable eight-record baseline is the `observations` table, which'
        ' holds eight rows and stays there.**',
        'capture_catalogue_is_the_stable_eight_record_baseline',
        'The stable baseline is relabelled onto the growing telemetry table.',
    ),
    # --- Task 13.1: motion-triggered capture --------------------------------
    #
    # Seven entries, and each defends a property that would fail *quietly* in
    # production: a capture nobody asked for, a backlog nobody bounded, a
    # counter nobody could trust, a second camera owner, metadata that changed
    # under the archive, a worker that died on its first bad night, and pending
    # work that started during shutdown. None of them would be visible from a
    # passing status endpoint, which is exactly why they are here.
    #
    # Single-line anchors throughout: these are Python sources, which check out
    # CRLF on Windows and LF elsewhere, and a multi-line `old` would match on
    # one host and go stale on the other.
    Mutation(
        'event-capture-triggers-on-any-motion-status',
        EVENT_CAPTURE_SERVICE,
        '        if result.status is not MotionStatus.MOTION_DETECTED:',
        '        if False:',
        'only_motion_detected_is_admitted',
        'Waiting, baseline, no-motion and error all take a picture.',
        suite=EVENT_CAPTURE_SUITE,
    ),
    Mutation(
        'event-capture-queue-becomes-unbounded',
        EVENT_CAPTURE_SERVICE,
        'QUEUE_CAPACITY = 1',
        'QUEUE_CAPACITY = 0',
        'queue_capacity_is_exactly_one or third_trigger_is_dropped',
        'A windy afternoon builds an unbounded backlog of stale moments.',
        suite=EVENT_CAPTURE_SUITE,
    ),
    Mutation(
        'dropped-triggers-stop-being-counted',
        EVENT_CAPTURE_SERVICE,
        '            self._state.total_triggers_dropped += 1',
        '            pass',
        'third_trigger_is_dropped',
        'Coalesced work disappears from the only record that holds it.',
        suite=EVENT_CAPTURE_SUITE,
    ),
    Mutation(
        'automatic-capture-gets-its-own-camera-coordinator',
        APPLICATION,
        '    capture_workflow = CaptureWorkflow(camera_coordinator, capture_archive)',
        '    capture_workflow = CaptureWorkflow(\n'
        '        CameraCoordinator(capture_service, preview_service),\n'
        '        capture_archive,\n'
        '    )',
        'preview_remains_a_single_producer_with_event_capture_enabled',
        'A second coordinator ends the single-owner guarantee for the camera.',
        suite=APPLICATION_SUITE,
    ),
    Mutation(
        'capture-metadata-is-no-longer-defensively-copied',
        CAPTURE_WORKFLOW,
        '        metadata = None if extra_metadata is None else dict(extra_metadata)',
        '        metadata = extra_metadata',
        'metadata_is_defensively_copied',
        "A caller's later edit rewrites attribution already being persisted.",
        suite=CAPTURE_WORKFLOW_SUITE,
    ),
    Mutation(
        'one-failed-capture-kills-the-worker',
        EVENT_CAPTURE_SERVICE,
        '        except Exception as error:',
        '        except asyncio.CancelledError as error:',
        'worker_survives_a_failure_and_captures_again',
        'The first bad night ends automatic capture until someone restarts it.',
        suite=EVENT_CAPTURE_SUITE,
    ),
    Mutation(
        'shutdown-lets-pending-work-start',
        EVENT_CAPTURE_SERVICE,
        '                self._queue.get_nowait()',
        '                break',
        'pending_trigger_is_discarded_and_never_starts',
        'A queued trigger takes the camera after shutdown has begun.',
        suite=EVENT_CAPTURE_SUITE,
    ),
    # --- Task 13.1 review correction ----------------------------------------
    #
    # Both defects below were real, shipped in the first Task 13.1 commit, and
    # invisible from every status endpoint and every functional test: the
    # feature kept producing correct captures, correct observations and correct
    # counters while doing so. They are exactly the kind a register is for.
    Mutation(
        'observation-persistence-returns-to-the-event-loop',
        EVENT_CAPTURE_SERVICE,
        '        await asyncio.to_thread(self._record_blocking, **fields)',
        '        self._record_blocking(**fields)',
        'observation_is_written_off_the_event_loop',
        'A SQLite observation write stalls the loop it was produced on.',
        suite=EVENT_CAPTURE_SUITE,
    ),
    Mutation(
        'shutdown-signals-every-monitor-up-front',
        APPLICATION,
        '    if motion_stop_event is not None:',
        '    for _event in stop_events:\n'
        '        _event.set()\n'
        '    if motion_stop_event is not None:',
        'remaining_monitors_are_signalled_only_after_event_capture',
        'Health and camera monitoring is torn down during an in-flight capture.',
        suite=APPLICATION_SUITE,
    ),
    # --- Task 14.1 retention policy -----------------------------------------
    Mutation(
        'retention-manages-any-capture-with-an-origin',
        RETENTION_POLICY,
        '        record.origin == MANAGED_ORIGIN',
        '        record.origin is not None',
        'only_exactly_motion_is_managed or only_unknown_origin',
        "An unrecognised subsystem's media becomes automatically disposable.",
        suite=RETENTION_POLICY_SUITE,
    ),
    Mutation(
        'retention-manages-captures-it-has-already-claimed',
        RETENTION_POLICY,
        '        and record.lifecycle_state is MediaLifecycleState.PRESENT',
        '        and record.lifecycle_state is not None',
        'pending_captures_are_excluded or already_deleted_captures_are_excluded',
        'One deletion is planned twice and finalised twice.',
        suite=RETENTION_POLICY_SUITE,
    ),
    Mutation(
        'the-minimum-keep-floor-is-removed',
        RETENTION_POLICY,
        '    eligible_count = max(0, len(ordered) - config.minimum_keep_count)',
        '    eligible_count = len(ordered)',
        'minimum_keep_count_protects or floor_is_never_broken or keep_count_larger',
        'The newest captures stop being protected from the policy.',
        suite=RETENTION_POLICY_SUITE,
    ),
    Mutation(
        'the-age-boundary-becomes-exclusive',
        RETENTION_POLICY,
        '        if record.captured_at_utc <= cutoff',
        '        if record.captured_at_utc < cutoff',
        'the_age_boundary_is_inclusive',
        'The documented age boundary and the enforced one disagree.',
        suite=RETENTION_POLICY_SUITE,
    ),
    Mutation(
        'the-managed-byte-boundary-becomes-strict',
        RETENTION_POLICY,
        '        if running <= config.max_managed_bytes:',
        '        if running < config.max_managed_bytes:',
        'the_managed_byte_boundary_is_inclusive_of_the_limit',
        'A managed total exactly at the limit deletes a capture anyway.',
        suite=RETENTION_POLICY_SUITE,
    ),
    Mutation(
        'the-byte-policy-stops-counting-down',
        RETENTION_POLICY,
        '        running -= record.filesize_bytes',
        '        running -= 0',
        'byte_policy_selects_no_more_than_the_bound_requires',
        'Byte pressure selects every eligible capture instead of the oldest few.',
        suite=RETENTION_POLICY_SUITE,
    ),
    Mutation(
        'the-per-run-destructive-bound-is-removed',
        RETENTION_POLICY,
        '    capped = selected[: config.max_deletions_per_run]',
        '    capped = selected',
        'max_deletions_per_run_caps_the_plan',
        'One run may delete an unbounded number of captures.',
        suite=RETENTION_POLICY_SUITE,
    ),
    Mutation(
        'a-truncated-plan-stops-reporting-remaining-work',
        RETENTION_POLICY,
        '    more_work_remains = len(selected) > len(capped)',
        '    more_work_remains = False',
        'a_capped_plan_reports_that_more_work_remains',
        'A capped run reads as having finished the backlog.',
        suite=RETENTION_POLICY_SUITE,
    ),
    Mutation(
        'ordering-loses-its-final-tie-break',
        RETENTION_POLICY,
        '        record.capture_id,',
        '        "",',
        'identical_timestamps_fall_through_to_the_capture_id',
        "SQLite's incidental row order starts deciding what is deleted.",
        suite=RETENTION_POLICY_SUITE,
    ),
    Mutation(
        'ordering-loses-its-creation-time-tie-break',
        RETENTION_POLICY,
        '        record.created_at_utc.isoformat(),',
        '        "",',
        'identical_capture_timestamps_break_on_creation_time',
        'Captures sharing an instant are selected in an undefined order.',
        suite=RETENTION_POLICY_SUITE,
    ),
    Mutation(
        'an-unreachable-byte-target-is-reported-as-reachable',
        RETENTION_POLICY,
        '    return preserved_bytes <= config.max_managed_bytes',
        '    return True',
        'the_floor_is_never_broken_to_satisfy_the_byte_limit',
        'An operator is told a budget is being met that never can be.',
        suite=RETENTION_POLICY_SUITE,
    ),
    Mutation(
        'the-combined-policy-reason-collapses',
        RETENTION_POLICY,
        '    if by_age and by_bytes:',
        '    if False:',
        'combined_policy_marks_a_capture_reached_by_both_rules',
        'A deletion is attributed to one rule when two reached it.',
        suite=RETENTION_POLICY_SUITE,
    ),
    # --- Task 14.1 filesystem safety boundary --------------------------------
    Mutation(
        'path-containment-is-abandoned',
        RETENTION_SERVICE,
        '    if not _is_within(capture_root, _realpath(candidate.parent)):',
        '    if False:',
        'a_path_outside_the_capture_root_is_refused',
        'A tampered catalogue row deletes a file anywhere on the host.',
        suite=RETENTION_SERVICE_SUITE,
    ),
    Mutation(
        'traversal-is-no-longer-rejected',
        RETENTION_SERVICE,
        '    if ".." in candidate.parts:',
        '    if False:',
        'a_traversal',
        'A parent-directory path walks out of the capture root.',
        suite=RETENTION_SERVICE_SUITE,
    ),
    Mutation(
        'filename-agreement-is-no-longer-required',
        RETENTION_SERVICE,
        '    if candidate.name != filename:',
        '    if False:',
        'a_filename_that_disagrees_with_the_path_is_refused',
        'Two catalogue columns disagree and the deletion proceeds anyway.',
        suite=RETENTION_SERVICE_SUITE,
    ),
    Mutation(
        'symlinked-targets-become-deletable',
        RETENTION_SERVICE,
        '    if _is_symlink(candidate):',
        '    if False:',
        'a_symlinked_target_is_refused',
        'A link inside the capture root redirects the unlink outside it.',
        suite=RETENTION_SERVICE_SUITE,
    ),
    Mutation(
        'a-relative-catalogue-path-is-accepted',
        RETENTION_SERVICE,
        '    if not candidate.is_absolute():',
        '    if False:',
        'a_relative_path_is_refused',
        'A path with no defined target is resolved against the process cwd.',
        suite=RETENTION_SERVICE_SUITE,
    ),
    Mutation(
        'the-regular-file-check-is-removed',
        RETENTION_SERVICE,
        '    if not _is_regular_file(candidate):',
        '    if False:',
        'a_non_regular_target_is_refused',
        'A device node or socket is unlinked as if it were a capture.',
        suite=RETENTION_SERVICE_SUITE,
    ),
    Mutation(
        'the-size-check-is-removed',
        RETENTION_SERVICE,
        '    if _file_size(candidate) != filesize_bytes:',
        '    if False:',
        'a_size_mismatch_is_refused',
        'A file that is not the catalogued one is deleted under its name.',
        suite=RETENTION_SERVICE_SUITE,
    ),
    Mutation(
        'missing-media-is-treated-as-already-reclaimed',
        RETENTION_SERVICE,
        '        return RetentionErrorCategory.MEDIA_MISSING',
        '        return None',
        'missing_media_without_a_pending_intent_is_an_inconsistency',
        'An unexplained missing file is silently claimed as a retention success.',
        suite=RETENTION_SERVICE_SUITE,
    ),
    Mutation(
        'the-capture-root-need-not-be-absolute',
        RETENTION_SERVICE,
        '        if not root.is_absolute():',
        '        if False:',
        'a_relative_capture_root_stops_the_run',
        'Containment is checked against a root that depends on the cwd.',
        suite=RETENTION_SERVICE_SUITE,
    ),
    # --- Task 14.1 run gating and the deletion state machine ------------------
    Mutation(
        'the-disabled-gate-is-removed',
        RETENTION_SERVICE,
        '        if not self._config.enabled:',
        '        if False:',
        'a_disabled_run_mutates_nothing or disabled_retention_does_not_recover',
        'A deployment that never enabled retention starts deleting media.',
        suite=RETENTION_SERVICE_SUITE,
    ),
    Mutation(
        'an-overlapping-run-stops-reporting-busy',
        RETENTION_SERVICE,
        '                error_category=RetentionErrorCategory.BUSY,',
        '                error_category=None,',
        'a_busy_run_deletes_nothing_and_records_nothing',
        'A refused run is indistinguishable from one that found nothing to do.',
        suite=RETENTION_SERVICE_SUITE,
    ),
    Mutation(
        'the-run-lock-becomes-reentrant',
        RETENTION_SERVICE,
        '        self._run_lock = threading.Lock()',
        '        self._run_lock = threading.RLock()',
        'an_overlapping_run_is_refused_as_busy',
        'Two runs delete against the same plan from one thread.',
        suite=RETENTION_SERVICE_SUITE,
    ),
    Mutation(
        'a-destructive-failure-no-longer-stops-the-run',
        RETENTION_SERVICE,
        '            error = self._delete_candidate(candidate, capture_root)',
        '            self._delete_candidate(candidate, capture_root); error = None',
        'a_run_stops_at_the_first_destructive_failure',
        'A run keeps deleting after the first safety refusal.',
        suite=RETENTION_SERVICE_SUITE,
    ),
    Mutation(
        'pending-intents-are-never-recovered',
        RETENTION_SERVICE,
        '        for record in self._pending_records(records):',
        '        for record in []:',
        'the_next_run_recovers_an_interrupted_deletion',
        'An interrupted deletion is stranded pending forever.',
        suite=RETENTION_SERVICE_SUITE,
    ),
    Mutation(
        'pending-recovery-loses-the-missing-file-distinction',
        RETENTION_SERVICE,
        '        if _path_exists(Path(record.absolute_path)):',
        '        if True:',
        'recovering_a_missing_file_finalises_exactly_one_success',
        'A completed deletion awaiting its record is called an inconsistency.',
        suite=RETENTION_SERVICE_SUITE,
    ),
    Mutation(
        'pending-recovery-skips-its-file-revalidation',
        RETENTION_SERVICE,
        '            if file_error is not None:',
        '            if False:',
        'a_pending_intent_whose_file_changed_size_is_not_touched',
        'A file that no longer matches its record is deleted anyway.',
        suite=RETENTION_SERVICE_SUITE,
    ),
    Mutation(
        'pending-recovery-ignores-an-unsafe-path',
        RETENTION_SERVICE,
        '            return path_error, 0',
        '            pass',
        'an_unsafe_pending_intent_is_not_touched',
        'A durable intent with a tampered path deletes outside the root.',
        suite=RETENTION_SERVICE_SUITE,
    ),
    Mutation(
        'a-failed-finalisation-is-reported-as-success',
        RETENTION_SERVICE,
        '        if not finalized:',
        '        if False:',
        'a_finalisation_that_matches_no_intent_is_not_a_success',
        'A deletion nothing recorded is counted as reclaimed.',
        suite=RETENTION_SERVICE_SUITE,
    ),
    Mutation(
        'the-dry-run-acquires-a-side-effect',
        RETENTION_SERVICE,
        '        catalogue = self._repository.read_lifecycle_records()',
        '        self._state.mark_running()\n'
        '        catalogue = self._repository.read_lifecycle_records()',
        'a_dry_run_moves_no_counter',
        'A read-only preview mutates the state an operator is watching.',
        suite=RETENTION_SERVICE_SUITE,
    ),
    # --- Task 14.1 lifecycle transitions -------------------------------------
    Mutation(
        'the-deletion-claim-becomes-unconditional',
        RETENTION_REPOSITORY,
        '                claimed = cursor.rowcount == 1',
        '                claimed = True',
        'a_second_claim_on_the_same_capture_is_refused',
        'Two executions both believe they own the same deletion.',
        suite=RETENTION_DATABASE_SUITE,
    ),
    Mutation(
        'finalisation-stops-being-conditional',
        RETENTION_REPOSITORY,
        '                advanced = cursor.rowcount == 1',
        '                advanced = True',
        'repeated_finalisation_cannot_duplicate_the_success_observation',
        'One deletion produces two success observations.',
        suite=RETENTION_DATABASE_SUITE,
    ),
    Mutation(
        'cancellation-stops-being-conditional',
        RETENTION_REPOSITORY,
        '                removed = cursor.rowcount == 1',
        '                removed = True',
        'cancelling_a_deleted_capture_is_refused',
        'Reclaimed media is quietly restored to present.',
        suite=RETENTION_DATABASE_SUITE,
    ),
    # --- Task 14.1 shared observation engine ---------------------------------
    Mutation(
        'the-shared-observation-validation-is-bypassed',
        OBSERVATIONS,
        '    if not summary.strip():',
        '    if False:',
        'an_invalid_observation_rolls_back_the_lifecycle_transition',
        'Retention writes observations under rules the timeline does not have.',
        suite=RETENTION_DATABASE_SUITE,
    ),
    Mutation(
        'the-shared-observation-insert-drops-its-correlation',
        OBSERVATIONS,
        '            observation.correlation_id,',
        '            None,',
        'finalising_transitions_and_records_together',
        'A retention observation stops naming the capture it describes.',
        suite=RETENTION_DATABASE_SUITE,
    ),
    # --- Task 14.1 runtime state and the inert status endpoint ---------------
    Mutation(
        'a-run-in-progress-is-not-reported',
        RETENTION_MODELS,
        '            self._state = RetentionState.RUNNING',
        '            self._state = RetentionState.IDLE',
        'a_run_in_progress_is_reported_as_running',
        'A destructive run in flight reads as an idle subsystem.',
        suite=RETENTION_API_SUITE,
    ),
    Mutation(
        'completed-runs-stop-being-counted',
        RETENTION_MODELS,
        '            self._total_runs += 1',
        '            self._total_runs += 0',
        'counters_reflect_completed_runs',
        'An operator cannot tell whether retention has ever run.',
        suite=RETENTION_API_SUITE,
    ),
    Mutation(
        'a-failed-run-reports-itself-as-idle',
        RETENTION_MODELS,
        '                RetentionState.ERROR',
        '                RetentionState.IDLE',
        'a_failed_run_is_reported_as_error_with_http_200',
        'A run that stopped on a safety refusal reads as healthy.',
        suite=RETENTION_API_SUITE,
    ),
    Mutation(
        'the-status-endpoint-executes-retention',
        APPLICATION,
        '    snapshot = _retention_state(request.app).snapshot()',
        '    request.app.state.retention_service.run_once()\n'
        '    snapshot = _retention_state(request.app).snapshot()',
        'the_endpoint_touches_no_other_subsystem',
        'Reading a status endpoint deletes media.',
        suite=RETENTION_API_SUITE,
    ),
    # --- Task 14.1 correction round 1 ---------------------------------------
    #
    # Four safety defects survived the first implementation, every automated
    # test and the whole 222-mutation register. Each one is registered here
    # against the property it broke, because each was invisible from every
    # status endpoint and every functional test until it was reproduced.
    Mutation(
        'an-unexpected-exception-escapes-the-run',
        RETENTION_SERVICE,
        '            return self._execute_tracked(tally)',
        '            return self._execute_tracked(tally)  # boundary removed\n'
        '        except _NeverRaised:',
        'an_unexpected_exception_becomes_a_bounded_result',
        'A destructive run raises, stranding runtime state in "running".',
        suite=RETENTION_SERVICE_SUITE,
    ),
    Mutation(
        'the-unexpected-boundary-discards-completed-deletions',
        RETENTION_SERVICE,
        'self._result(tally, RetentionErrorCategory.UNEXPECTED)',
        'self._result(_RunTally(), RetentionErrorCategory.UNEXPECTED)',
        'an_unexpected_failure_preserves_an_earlier_completed_deletion',
        'A run reports zero reclaimed after it had already deleted media.',
        suite=RETENTION_SERVICE_SUITE,
    ),
    Mutation(
        'the-completed-run-is-recorded-outside-the-lock',
        RETENTION_SERVICE,
        '            self._state.record_run(',
        '            pass\n'
        '        if False:\n'
        '            self._state.record_run(',
        'an_unexpected_exception_leaves_the_state_in_error_not_running',
        'A finished run leaves the holder claiming it is still running.',
        suite=RETENTION_SERVICE_SUITE,
    ),
    Mutation(
        'a-corrupt-catalogue-filesize-is-coerced',
        RETENTION_REPOSITORY,
        '    if isinstance(raw_value, bool) or not isinstance(raw_value, int):',
        '    if False:',
        'an_unusable_catalogue_filesize_fails_closed',
        'A raw conversion error escapes catalogue decoding and strands the run.',
        suite=RETENTION_DATABASE_SUITE,
    ),
    Mutation(
        'a-non-positive-catalogue-filesize-is-accepted',
        RETENTION_REPOSITORY,
        '    if raw_value <= 0:',
        '    if False:',
        'an_unusable_catalogue_filesize_fails_closed',
        'A corrupt zero-byte record is treated as an ordinary size mismatch.',
        suite=RETENTION_DATABASE_SUITE,
    ),
    Mutation(
        'a-required-text-column-is-coerced-with-str',
        RETENTION_REPOSITORY,
        '    if not isinstance(raw_value, str) or not raw_value:',
        '    if False:',
        'a_required_text_column_fails_closed',
        'A missing filename becomes the manufactured string "None".',
        suite=RETENTION_DATABASE_SUITE,
    ),
    Mutation(
        'version-three-adoption-checks-only-column-names',
        DATABASE,
        '            _verify_table_semantics(connection, table, shape)',
        '            pass',
        'an_unconstrained_unversioned_version_three_table_is_rejected',
        'A lifecycle table with no constraints is adopted as version 3.',
        suite=MIGRATIONS_SUITE,
    ),
    Mutation(
        'version-three-adoption-stops-requiring-its-primary-key',
        DATABASE,
        '        if actual_key != shape.primary_key:',
        '        if False:',
        'an_unconstrained_unversioned_version_three_table_is_rejected',
        'One capture may hold two conflicting deletion intents.',
        suite=MIGRATIONS_SUITE,
    ),
    Mutation(
        'version-three-adoption-stops-requiring-its-foreign-key',
        DATABASE,
        '        missing_keys = sorted(set(shape.foreign_keys) - actual_keys)',
        '        missing_keys = []',
        'an_unconstrained_unversioned_version_three_table_is_rejected',
        'A lifecycle row may reference a capture that does not exist.',
        suite=MIGRATIONS_SUITE,
    ),
    Mutation(
        'version-three-adoption-stops-requiring-its-check-constraints',
        DATABASE,
        '        if absent:',
        '        if False:',
        'an_unconstrained_unversioned_version_three_table_is_rejected',
        'The state and reason vocabularies stop being enforced by the database.',
        suite=MIGRATIONS_SUITE,
    ),
    Mutation(
        'migration-003-silently-accepts-a-pre-existing-table',
        MIGRATION_003,
        'CREATE TABLE capture_media_lifecycle (',
        'CREATE TABLE IF NOT EXISTS capture_media_lifecycle (',
        'a_pre_existing_lifecycle_table_fails_migration_003',
        'A database records version 3 over a table the migration never created.',
        suite=MIGRATIONS_SUITE,
    ),
    Mutation(
        'a-failure-observation-persists-the-untrusted-filename',
        RETENTION_SERVICE,
        '                "error_category": category.value,',
        '                "filename": "unused",\n'
        '                "error_category": category.value,',
        'a_failure_observation_never_persists_an_untrusted_filename',
        'A rejected path is written into the immutable observation timeline.',
        suite=RETENTION_SERVICE_SUITE,
    ),
    # --- Task 14.1 final correction: lifecycle capture identity -------------
    #
    # Two genuinely distinct sites. The first is what the schema enforces for a
    # database this build creates; the second is what adoption demands of a
    # database it did not create. Weakening either one alone reopens the
    # invariant, from a different direction.
    Mutation(
        'the-lifecycle-identity-becomes-nullable',
        MIGRATION_003,
        '    capture_id TEXT NOT NULL PRIMARY KEY',
        '    capture_id TEXT PRIMARY KEY',
        'a_null_lifecycle_capture_id_is_rejected or multiple_null_lifecycle',
        'Lifecycle rows bound to no capture become storable, and multiply.',
        suite=MIGRATIONS_SUITE,
    ),
    Mutation(
        'adoption-stops-requiring-a-non-null-lifecycle-identity',
        DATABASE,
        '            {"capture_id", "state", "requested_at_utc", "reason"}',
        '            {"state", "requested_at_utc", "reason"}',
        'a_nullable_capture_id_unversioned_schema_is_rejected',
        'A foreign database with a nullable identity is adopted as version 3.',
        suite=MIGRATIONS_SUITE,
    ),
    # --- Task 14.2 controlled retention operator interface -------------------
    Mutation(
        'the-preview-reads-through-the-read-write-path',
        RETENTION_SERVICE,
        '        catalogue = self._repository.read_lifecycle_records()',
        '        catalogue = self._repository.list_lifecycle_records()',
        'creates_no_missing_parent_directory or does_not_change_the_journal_mode',
        'A read-only preview creates a database, a directory and a WAL mode.',
        suite=RETENTION_READONLY_SUITE,
    ),
    Mutation(
        'the-read-only-projection-opens-read-write',
        RETENTION_REPOSITORY,
        '            connection = connect_readonly(self._database_path)',
        '            connection = database_connection(self._database_path).__enter__()',
        'against_a_missing_database_creates_nothing',
        'The mode=ro boundary is abandoned and the preview can mutate again.',
        suite=RETENTION_READONLY_SUITE,
    ),
    Mutation(
        'run-once-stops-requiring-the-execute-flag',
        RETENTION_CLI,
        '    if not arguments.execute:',
        '    if False:',
        'without_execute_refuses_before_any_mutation',
        'A bare run-once deletes media with no explicit confirmation.',
        suite=RETENTION_CLI_SUITE,
    ),
    Mutation(
        'run-once-stops-requiring-retention-to-be-enabled',
        RETENTION_CLI,
        '    if not config.retention.enabled:',
        '    if False:',
        'with_retention_disabled_refuses_before_any_mutation',
        'A deployment that never enabled retention is deleted from anyway.',
        suite=RETENTION_CLI_SUITE,
    ),
    Mutation(
        'run-once-stops-requiring-an-explicit-configuration',
        RETENTION_CLI,
        '    _require_explicit_configuration()',
        '    pass',
        'without_an_explicit_configuration_refuses',
        'A destructive run resolves whichever development config is default.',
        suite=RETENTION_CLI_SUITE,
    ),
    Mutation(
        'a-blank-configuration-variable-counts-as-explicit',
        RETENTION_CLI,
        '    if raw is None or not raw.strip():',
        '    if raw is None:',
        'an_empty_configuration_variable_is_not_an_explicit_choice',
        'An empty environment variable passes as a deliberate selection.',
        suite=RETENTION_CLI_SUITE,
    ),
    Mutation(
        'the-schema-gate-accepts-any-version',
        RETENTION_CLI,
        '    if version != CURRENT_SCHEMA_VERSION:',
        '    if False:',
        'lower_schema_is_refused_and_not_migrated or higher_schema_is_refused',
        'An older or newer database is acted on instead of refused.',
        suite=RETENTION_CLI_SUITE,
    ),
    Mutation(
        'the-command-repeats-while-work-remains',
        RETENTION_CLI,
        '    result = _build_service(config).run_once()',
        '    service = _build_service(config)\n'
        '    result = service.run_once()\n'
        '    while result.more_work_remains:\n'
        '        result = service.run_once()',
        'more_work_remaining_does_not_trigger_a_second_run',
        'A one-shot operator command becomes an unreviewed deletion loop.',
        suite=RETENTION_CLI_SUITE,
    ),
    Mutation(
        'a-missing-subcommand-is-tolerated',
        RETENTION_CLI,
        '    subcommands.required = True',
        '    subcommands.required = False',
        'an_unsupported_invocation_is_refused',
        'An incomplete invocation is accepted instead of refused.',
        suite=RETENTION_CLI_SUITE,
    ),
    Mutation(
        'the-preview-publishes-the-media-path',
        RETENTION_CLI,
        '    payload = plan.as_dict()',
        '    payload = plan.as_dict()\n'
        '    for _entry, _candidate in zip(\n'
        '        payload["candidates"], plan.candidates, strict=True\n'
        '    ):\n'
        '        _entry["absolute_path"] = _candidate.absolute_path',
        'plan_leaks_no_path_of_any_kind or plan_output_has_a_deterministic_shape',
        'Absolute media paths enter the operator-facing JSON.',
        suite=RETENTION_CLI_SUITE,
    ),
)
