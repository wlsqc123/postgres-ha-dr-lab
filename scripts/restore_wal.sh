#!/usr/bin/env bash
set -euo pipefail
[[ "$1" =~ ^[0-9A-Za-z.]+$ ]] && [[ "$1" != . && "$1" != .. ]]
if [ "${SIMULATE_MISSING_WAL:-false}" = true ] && [ "$1" = "$(cat /backup/target-wal)" ]; then
    echo "Required WAL deliberately withheld: $1" >&2
    exit 1
fi
cp "/archive/$1" "$2"
