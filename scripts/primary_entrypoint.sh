#!/usr/bin/env bash
set -euo pipefail

if [ -e "${PGDATA:?}/lab.fenced" ]; then
    echo 'Primary is fenced after failover. Rejoin as a Standby before removing this marker.' >&2
    exit 1
fi
exec docker-entrypoint.sh "$@"
