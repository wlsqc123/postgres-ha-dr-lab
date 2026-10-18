#!/usr/bin/env bash
set -euo pipefail

if [ -e "${PGDATA:?}/lab.fenced" ]; then
    echo 'Primary is fenced after failover. Rejoin as a Standby before removing this marker.' >&2
    exit 1
fi
if [ "${EXPECT_EXISTING_PRIMARY:-false}" = true ]; then
    test -f "$PGDATA/PG_VERSION"
    test ! -f "$PGDATA/standby.signal"
fi
exec docker-entrypoint.sh "$@"
