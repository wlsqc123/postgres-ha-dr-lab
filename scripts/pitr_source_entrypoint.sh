#!/usr/bin/env bash
set -euo pipefail
mkdir -p /archive /backup
chown postgres:postgres /archive /backup
chmod 700 /archive /backup
exec bash /etc/lab/primary_entrypoint.sh "$@"
