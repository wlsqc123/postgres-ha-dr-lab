#!/usr/bin/env bash
set -euo pipefail

if [ "$(id -u)" = 0 ]; then
    mkdir -p "$PGDATA"
    chown postgres:postgres "$PGDATA"
    chmod 700 "$PGDATA"
    exec gosu postgres bash "$0" "$@"
fi

if [ ! -f "$PGDATA/PG_VERSION" ]; then
    if [ -n "$(find "$PGDATA" -mindepth 1 -maxdepth 1 -print -quit)" ]; then
        echo 'Standby data directory is not empty; refusing to overwrite it' >&2
        exit 1
    fi
    export PGPASSWORD="$REPLICATION_PASSWORD"
    pg_basebackup --dbname='host=primary user=replicator application_name=standby_stage2' \
        -D "$PGDATA" -R -X stream -S standby_stage2 --checkpoint=fast
fi

if [ ! -f "$PGDATA/standby.signal" ]; then
    echo 'standby.signal is missing; refusing to start this directory as a Primary' >&2
    exit 1
fi
exec docker-entrypoint.sh "$@"
