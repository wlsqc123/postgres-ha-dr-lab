#!/usr/bin/env bash
set -euo pipefail
mkdir -p "$PGDATA"
if [ ! -f "$PGDATA/PG_VERSION" ]; then
    test -z "$(find "$PGDATA" -mindepth 1 -maxdepth 1 -print -quit)"
    cp -a /backup/base/. "$PGDATA/"
    cat /backup/target.conf >> "$PGDATA/postgresql.auto.conf"
    cat >> "$PGDATA/postgresql.auto.conf" <<'CONFIG'
restore_command = 'bash /etc/lab/restore_wal.sh %f %p'
recovery_target_inclusive = on
recovery_target_timeline = '1'
recovery_target_action = 'pause'
CONFIG
    touch "$PGDATA/recovery.signal"
    chown -R postgres:postgres "$PGDATA"
    chmod 700 "$PGDATA"
fi
test -f "$PGDATA/recovery.signal"
test ! -f "$PGDATA/standby.signal"
exec docker-entrypoint.sh "$@"
