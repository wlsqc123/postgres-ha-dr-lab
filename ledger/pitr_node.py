"""Restricted runner-side operations for the isolated PITR experiment."""
import json
import os
import sys

from psycopg import sql

from .model import connect


def validate_damage_scope(args):
    if args['host'] != 'pitr-source' or sorted(args['accounts']) != [1001, 1002]:
        raise ValueError('Deletion is restricted to the isolated PITR source and its two test accounts')


def execute(args):
    if args['action'] == 'damage':
        validate_damage_scope(args)
    with connect(args['host'], admin=True) as conn:
        action = args['action']
        if action == 'prepare':
            conn.execute(sql.SQL('CREATE ROLE replicator LOGIN REPLICATION PASSWORD {}').format(
                sql.Literal(os.environ['REPLICATION_PASSWORD'])))
            return conn.execute("SELECT current_setting('archive_mode') AS archive_mode, "
                                "current_setting('archive_command') AS archive_command, "
                                "current_setting('data_checksums') AS data_checksums").fetchone()
        if action == 'switch':
            return conn.execute('SELECT pg_switch_wal() AS switched_lsn').fetchone()
        if action == 'target':
            return conn.execute('SELECT clock_timestamp() AS target_time, pg_current_wal_insert_lsn() AS lsn, '
                                'pg_walfile_name(pg_current_wal_insert_lsn()) AS wal').fetchone()
        if action == 'damage':
            accounts = args['accounts']
            with conn.transaction():
                entries = conn.execute('DELETE FROM ledger_entries WHERE account_id=ANY(%s)', (accounts,)).rowcount
                requests = conn.execute('DELETE FROM request_history WHERE source=ANY(%s) OR destination=ANY(%s)',
                                        (accounts, accounts)).rowcount
                balances = conn.execute('DELETE FROM accounts WHERE id=ANY(%s)', (accounts,)).rowcount
            return dict(deleted_entries=entries, deleted_requests=requests, deleted_accounts=balances)
        if action == 'archive':
            return conn.execute('SELECT * FROM pg_stat_archiver').fetchone()
        if action == 'recovery':
            return conn.execute("SELECT pg_is_in_recovery() AS recovery, pg_get_wal_replay_pause_state() AS pause_state, "
                                "pg_last_xact_replay_timestamp() AS last_commit, "
                                "current_setting('recovery_target_time') AS target_time, "
                                "current_setting('recovery_target_action') AS target_action").fetchone()
        if action == 'resume':
            state = conn.execute('SELECT pg_is_in_recovery() AS recovery, '
                                 'pg_get_wal_replay_pause_state() AS pause_state').fetchone()
            if not state['recovery'] or state['pause_state'] != 'paused':
                raise RuntimeError('Recovery must be paused at the verified target before resuming')
            conn.execute('SELECT pg_wal_replay_resume()')
            return {'resumed': True}
        raise ValueError(action)


if __name__ == '__main__':
    print(json.dumps(execute(json.load(sys.stdin)), default=str))
