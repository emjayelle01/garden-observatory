#!/usr/bin/env bash
#
# update-main.sh — align the Pi checkout to origin/main and restart the service.
#
# Run as your normal user from anywhere:
#   bash scripts/deploy/update-main.sh
#
# Non-destructive: it refuses to run with a dirty working tree, only
# fast-forwards main (never merges or rebases), runs `uv sync`, restarts
# mgo.service, and prints the service status plus a health probe. It makes no
# SSH, camera, or application-code changes.

set -euo pipefail

# Resolve the repository root from this script's location.
script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd "${script_dir}/../.." && pwd)"
cd "${repo_root}"

service="mgo.service"

printf 'Deploying MGO from %s\n\n' "${repo_root}"

# --- refuse on a dirty tree ------------------------------------------------
if [[ -n "$(git status --porcelain)" ]]; then
  printf 'REFUSING: working tree is not clean. Commit or stash first:\n' >&2
  git status --short >&2
  exit 1
fi

# --- fast-forward main -----------------------------------------------------
git fetch origin
git checkout main
before="$(git rev-parse HEAD)"
git pull --ff-only origin main
after="$(git rev-parse HEAD)"

if [[ "${before}" == "${after}" ]]; then
  printf 'Already up to date at %s\n' "${after}"
else
  printf 'Updated %s -> %s\n' "${before}" "${after}"
fi

# --- dependencies ----------------------------------------------------------
if command -v uv >/dev/null 2>&1; then
  uv sync
else
  printf 'WARNING: uv not found on PATH; skipping "uv sync".\n' >&2
fi

# --- restart the service ---------------------------------------------------
printf '\nRestarting %s ...\n' "${service}"
sudo systemctl restart "${service}"
sleep 1
systemctl --no-pager --full status "${service}" || true

# --- health probe (best effort) -------------------------------------------
printf '\nHealth probe:\n'
if command -v curl >/dev/null 2>&1; then
  curl -fsS "http://127.0.0.1:8080/health" \
    && printf '\n' \
    || printf '(health endpoint not reachable on :8080 — check the service and config)\n'
else
  printf '(curl not installed; skip health probe)\n'
fi

printf '\nDeployment step complete. Current HEAD: %s\n' "${after}"
