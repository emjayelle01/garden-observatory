#!/usr/bin/env bash
#
# backup-database.sh — operator wrapper for the MGO database backup CLI.
#
# This script makes NO backup decisions. Consistency, atomic publication,
# validation, checksums, retention, redaction and locking all live in
# src/mgo/operations/, where they are typed, unit-tested and exercised on the
# development machine. The wrapper exists so an operator on the Pi can type a
# short command instead of remembering a module path.
#
# It makes exactly one execution-environment decision, and it is not business
# logic: WHICH CONFIGURATION AN OPERATOR MEANS. When MGO_CONFIG_PATH is unset
# the wrapper defaults it to /etc/garden-observatory/mgo.toml before Python
# runs. Without that, a bare "backup-database.sh backup" on the Pi fell through
# to the tracked *development* configuration — selecting a development database
# path and snapshotting the wrong file. mgo-backup.service was always explicit,
# so the scheduled backup was correct and only manual operation was not.
#
#   explicit --config          wins over everything
#   caller-provided value      preserved exactly, including empty or whitespace
#   unset                      becomes /etc/garden-observatory/mgo.toml
#
# A set-but-empty or whitespace-only MGO_CONFIG_PATH is a configuration error in
# the application, and the wrapper preserves it so the CLI still reports it. It
# is not the wrapper's business to repair a value an operator deliberately set.
#
# Usage:
#   scripts/operations/backup-database.sh backup
#   scripts/operations/backup-database.sh list
#   scripts/operations/backup-database.sh verify      /var/backups/garden-observatory/<backup>.db
#   scripts/operations/backup-database.sh restore-test /var/backups/garden-observatory/<backup>.db
#
# Every argument is forwarded unchanged and the Python exit status is preserved,
# so the wrapper is transparent to a caller and to systemd.
#
# It never uses sudo, never modifies a tracked file, never uploads anything and
# never stops the service: a backup is taken while the API keeps serving.

set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
app_root="$(cd "${script_dir}/../.." && pwd)"
python_bin="${app_root}/.venv/bin/python"

usage() {
  cat <<'USAGE'
Usage: scripts/operations/backup-database.sh <command> [options]

Commands:
  backup         Take, validate and publish a backup, then apply retention.
  verify         Verify a backup against its manifest (read-only).
  restore-test   Restore a backup into an isolated directory and verify it.
  list           List the complete backup sets in the backup directory.

Common options:
  --config PATH             Configuration file to read the database path from
                            (default: MGO_CONFIG_PATH, which this wrapper sets
                            to /etc/garden-observatory/mgo.toml when unset)
  --database PATH           Database to back up (overrides the configuration)
  --output-directory PATH   Backup directory
                            (default: /var/backups/garden-observatory)
  --keep N                  Complete backup sets to retain (default: 14)
  -h, --help                Show this help

There is deliberately no "restore" command. restore-test proves a backup can be
recovered; restoring over the live production database is an explicit operator
disaster-recovery procedure documented in docs/Operations.md.

Run "<command> --help" for the full options of a single command.
USAGE
}

if [[ $# -eq 0 ]]; then
  usage >&2
  exit 2
fi

case "$1" in
  -h|--help)
    usage
    exit 0
    ;;
esac

if [[ ! -x "${python_bin}" ]]; then
  printf 'ERROR: no Python interpreter at %s\n' "${python_bin}" >&2
  printf '       Create the virtual environment as the administrative user:\n' >&2
  printf '         cd %s && uv sync\n' "${app_root}" >&2
  exit 1
fi

# -v tests whether the NAME is set, not whether its VALUE is non-empty. The
# usual ": ${VAR:=default}" idiom would also replace a deliberately empty value,
# which the application treats as an error and must keep treating as one.
if [[ ! -v MGO_CONFIG_PATH ]]; then
  export MGO_CONFIG_PATH="/etc/garden-observatory/mgo.toml"
fi

exec "${python_bin}" -m mgo.operations.backup_cli "$@"
