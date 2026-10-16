"""Run inside runner. Verify SQL access; --verify-only proves existing data survived."""
import argparse
import json

import psycopg
from psycopg.rows import dict_row


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--verify-only', action='store_true')
    args = parser.parse_args()
    with psycopg.connect(connect_timeout=5, row_factory=dict_row) as conn:
        state = conn.execute(
            "SELECT version() AS version, current_database() AS database, "
            "pg_is_in_recovery() AS in_recovery, "
            "current_setting('transaction_read_only') AS read_only, "
            "current_setting('data_checksums') AS data_checksums, "
            "current_setting('server_version_num')::integer AS version_num"
        ).fetchone()
        if state['database'] != 'lab' or state['in_recovery'] or state['read_only'] != 'off':
            raise RuntimeError(f'Expected writable lab Primary: {state}')
        if not 180000 <= state['version_num'] < 190000 or state['data_checksums'] != 'on':
            raise RuntimeError(f'Unexpected PostgreSQL version or checksum setting: {state}')
        if not args.verify_only:
            conn.execute('CREATE TABLE IF NOT EXISTS environment_probe '
                         '(id integer PRIMARY KEY, value text NOT NULL)')
            conn.execute("INSERT INTO environment_probe VALUES (1, 'primary-ready') "
                         'ON CONFLICT DO NOTHING')
    # A separate connection checks that the transaction actually committed.
    with psycopg.connect(connect_timeout=5, row_factory=dict_row) as conn:
        row = conn.execute('SELECT id, value FROM environment_probe WHERE id = 1').fetchone()
        if row != {'id': 1, 'value': 'primary-ready'}:
            raise RuntimeError(f'Unexpected persisted row: {row}')
    print(json.dumps(dict(state=state, row=row, verify_only=args.verify_only, success=True), indent=2))


if __name__ == '__main__':
    main()
