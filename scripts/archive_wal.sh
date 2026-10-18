#!/usr/bin/env bash
set -euo pipefail
[[ "$2" =~ ^[0-9A-Za-z.]+$ ]] && [[ "$2" != . && "$2" != .. ]]
destination="/archive/$2"
if [ -f "$destination" ]; then
    cmp -s "$1" "$destination"
    sync "$destination"
    sync /archive
    exit 0
fi
temporary=$(mktemp /archive/.wal.XXXXXX)
trap 'rm -f "$temporary"' EXIT
cp "$1" "$temporary"
sync "$temporary"
ln "$temporary" "$destination"
sync /archive
