#!/usr/bin/env bash
#
# backup-database.sh — operator wrapper for the MGO database backup CLI.
#
# This script contains NO business logic. Every decision — consistency, atomic
# publication, validation, checksums, retention, locking — lives in
# src/mgo/operations/, where it is typed, unit-tested and exercised on the
# development machine. The wrapper exists only so an operator on the Pi can type
# a short command instead of remembering a module path.
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

exec "${python_bin}" -m mgo.operations.backup_cli "$@"
