#!/usr/bin/env bash
#
# create-support-bundle.sh — operator wrapper for the MGO diagnostic bundle CLI.
#
# Contains NO business logic. Collection, bounding, redaction and archive
# assembly all live in src/mgo/operations/support_bundle.py, where they are
# typed and unit-tested.
#
# Usage:
#   scripts/operations/create-support-bundle.sh
#   scripts/operations/create-support-bundle.sh --output-directory /tmp
#
# Exit status is preserved from Python and is meaningful:
#   0  complete bundle — every source answered
#   1  partial bundle  — the file exists and is useful, something was
#                        unreachable (often the API, which may be the fault)
#   2  no bundle was created
#
# The bundle contains no database, no WAL/shm sidecar, no media, no media
# filenames, no raw configuration, no environment dump, no SSH material, no Git
# credentials and no secrets. INSPECT IT BEFORE SENDING IT ANYWHERE:
#
#   tar -tzf mgo-support-<timestamp>.tar.gz
#   tar -xzOf mgo-support-<timestamp>.tar.gz configuration-summary.json
#
# It never uses sudo, never uploads anything and never modifies a tracked file.

set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
app_root="$(cd "${script_dir}/../.." && pwd)"
python_bin="${app_root}/.venv/bin/python"

usage() {
  cat <<'USAGE'
Usage: scripts/operations/create-support-bundle.sh [options]

Options:
  --config PATH             Configuration file to summarise
  --output-directory PATH   Where to write the bundle (default: current dir)
  --api-base-url URL        Loopback API base URL (default: http://127.0.0.1:8080)
  --unit NAME               systemd unit to inspect (default: mgo.service)
  --backup-directory PATH   Backup directory to summarise
  -h, --help                Show this help

Exit status: 0 complete, 1 partial, 2 no bundle created.

Always inspect a bundle before sending it:
  tar -tzf mgo-support-<timestamp>.tar.gz
USAGE
}

case "${1:-}" in
  -h|--help)
    usage
    exit 0
    ;;
esac

if [[ ! -x "${python_bin}" ]]; then
  printf 'ERROR: no Python interpreter at %s\n' "${python_bin}" >&2
  printf '       Create the virtual environment as the administrative user:\n' >&2
  printf '         cd %s && uv sync\n' "${app_root}" >&2
  exit 2
fi

exec "${python_bin}" -m mgo.operations.support_bundle_cli "$@"
