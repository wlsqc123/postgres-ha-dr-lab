"""JSON RPC for a few DB operations; runs only in the runner container."""
import json
import sys

from .journal import append
from .model import PayloadMismatch, connect, snapshot, transfer
import psycopg


def execute(args):
    host = args['host']
    action = args['action']
    if action == 'transfer':
        with connect(host) as conn:
            return {'outcome': transfer(conn, *args['request'])}
    if action == 'retry':
        outcomes = []
        with connect(host) as conn:
            for request in args['requests']:
                append(args['log'], event='intent', request=request, host=host)
                try:
                    outcome = transfer(conn, *request)
                except (PayloadMismatch, psycopg.IntegrityError) as exc:
                    append(args['log'], event='result', state='rejected', request=request, host=host, error=str(exc))
                    raise
                except (psycopg.OperationalError, psycopg.InterfaceError) as exc:
                    append(args['log'], event='result', state='uncertain', request=request, host=host, error=str(exc))
                    raise
                except psycopg.Error as exc:
                    append(args['log'], event='result', state='rejected', request=request, host=host, error=str(exc))
                    raise
                append(args['log'], event='result', state='acknowledged', request=request, host=host, outcome=outcome)
                outcomes.append({'request_id': request[0], 'outcome': outcome})
        return outcomes
    with connect(host, admin=True) as conn:
        if action == 'readonly':
            if not conn.execute('SELECT pg_is_in_recovery() AS recovery').fetchone()['recovery']:
                raise RuntimeError('Write rejection check requires a Standby')
            try:
                with conn.transaction():
                    conn.execute('INSERT INTO accounts VALUES (0,0)')
                    raise RuntimeError('Unexpected write accepted; transaction rolled back')
            except psycopg.errors.ReadOnlySqlTransaction as exc:
                return dict(sqlstate=exc.sqlstate, message=str(exc))
        if action == 'snapshot':
            return snapshot(conn, args['accounts'])
        if action == 'seed':
            with conn.transaction():
                for account, balance in args['initial']:
                    conn.execute('INSERT INTO accounts VALUES (%s,%s)', (account, balance))
            return {'seeded': True}
        if action == 'promote':
            return conn.execute('SELECT pg_promote(false) AS requested').fetchone()
        if action == 'checkpoint':
            conn.execute('CHECKPOINT')
            return {'checkpoint': True}
        if action != 'state':
            raise ValueError(action)
        state = conn.execute("SELECT pg_is_in_recovery() AS recovery, version() AS version, "
                             "current_setting('synchronous_standby_names') AS synchronous_standby_names, "
                             "current_setting('synchronous_commit') AS synchronous_commit").fetchone()
        state['control'] = conn.execute('SELECT * FROM pg_control_checkpoint()').fetchone()
        state['system'] = conn.execute('SELECT system_identifier FROM pg_control_system()').fetchone()
        state['senders'] = conn.execute('SELECT * FROM pg_stat_replication').fetchall()
        state['receiver'] = conn.execute('SELECT status,sender_host,written_lsn,flushed_lsn,latest_end_lsn '
                                         'FROM pg_stat_wal_receiver').fetchall()
        state['slots'] = conn.execute('SELECT * FROM pg_replication_slots').fetchall()
        state['lsn'] = conn.execute('SELECT pg_last_wal_receive_lsn() AS receive,pg_last_wal_replay_lsn() AS replay'
                                   if state['recovery'] else 'SELECT pg_current_wal_lsn() AS current').fetchone()
        return state


if __name__ == '__main__':
    print(json.dumps(execute(json.load(sys.stdin)), default=str))
