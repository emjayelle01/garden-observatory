#!/bin/bash
#
# update-main.sh — deploy origin/main to this Pi through the approved gateway.
#
# Run as your normal user on the Pi:
#   bash scripts/deploy/update-main.sh
#
# This script is a wrapper and nothing else. Every decision — which commit may
# be deployed, whether the checkout is fit to move, whether the move is a
# fast-forward, when the service is restarted, whether the preview is restored,
# and what happens when any of that fails — belongs to the gateway at
# /usr/local/sbin/mgo-validate. See docs/Deployment-Gateway.md.
#
# It used to do the work itself: fetch, pull, a resolving `uv sync`, a recursive
# sudo chgrp/chmod over the whole checkout, a direct `sudo systemctl restart`,
# and best-effort endpoint probes that reported success even when the API never
# came back. None of that consulted the approved SHA, and none of it could undo
# a half-applied deployment. That is why the logic moved behind the gateway and
# why nothing may be re-added here: a second, weaker deployment path is exactly
# the defect this replaced.
#
# There is deliberately no fallback. If the gateway is not installed, the answer
# is to install it (sudo ./scripts/deploy/install-mgo-validate.sh), not to
# deploy around it.

set -Eeuo pipefail

readonly GATEWAY="/usr/local/sbin/mgo-validate"

if [[ "${EUID}" -eq 0 ]]; then
    printf 'update-main: run this as your normal account, not as root.\n' >&2
    printf 'update-main: the gateway raises its own privilege through sudo.\n' >&2
    exit 1
fi

if [[ ! -x "$GATEWAY" ]]; then
    printf 'update-main: the deployment gateway is not installed at %s\n' \
        "$GATEWAY" >&2
    printf 'update-main: install it with:\n' >&2
    printf '  sudo ./scripts/deploy/install-mgo-validate.sh\n' >&2
    exit 1
fi

# exec so the gateway's exit code and output reach the caller unchanged: a
# wrapper that summarised them could report a success the gateway did not.
exec sudo -n "$GATEWAY" deploy-main
