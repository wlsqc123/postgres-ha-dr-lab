"""Small runner-side operations for the replication comparison."""
import argparse
import json
from pathlib import Path
import time

import psycopg
from psycopg import sql

from .journal import append, save
from .model import connect, transfer


def execute(args):
    if args['action'] == 'configure':
        with connect(args['host'], admin=True) as conn:
            conn.execute(sql.SQL('ALTER SYSTEM SET synchronous_standby_names = {}').format(
                sql.Literal('FIRST 1 (standby_stage2)' if args['mode'] == 'sync' else '')))
            conn.execute('SELECT pg_reload_conf()')
        return {'reload_requested': True}
    if args['action'] == 'waits':
        with connect(args['host'], admin=True) as conn:
            return conn.execute('SELECT pid,state,wait_event_type,wait_event FROM pg_stat_activity '
                                'WHERE application_name=%s', (args['application_name'],)).fetchall()
    if args['action'] != 'batch':
        raise ValueError(args['action'])
    samples = []
    conn = connect(args['host'])
    try:
        conn.execute("SELECT set_config('application_name',%s,false)", (args['application_name'],))
        conn.execute('SET synchronous_commit=on')
        conn.execute('SET statement_timeout=0')
        settings = conn.execute("SELECT current_setting('synchronous_commit') AS synchronous_commit, "
                                "current_setting('synchronous_standby_names') AS synchronous_standby_names").fetchone()
        for request in args['requests']:
            append(args['log'], event='intent', request=request, host=args['host'], phase=args['phase'])
            start = time.perf_counter()
            try:
                outcome = transfer(conn, *request)
            except (psycopg.OperationalError, psycopg.InterfaceError) as exc:
                append(args['log'], event='result', state='uncertain', request=request, host=args['host'],
                       phase=args['phase'], error=str(exc))
                return dict(settings=settings, samples=samples, state='uncertain')
            latency = (time.perf_counter() - start) * 1000
            append(args['log'], event='result', state='acknowledged', request=request, host=args['host'],
                   phase=args['phase'], latency_ms=latency, outcome=outcome)
            samples.append(latency)
    finally:
        conn.close()
    return dict(settings=settings, samples=samples, state='acknowledged')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input', type=Path)
    args = parser.parse_args()
    if args.input:
        payload = json.loads(args.input.read_text(encoding='utf-8'))
    else:
        import sys
        payload = json.load(sys.stdin)
    result = execute(payload)
    if 'output' in payload:
        save(payload['output'], result)
    print(json.dumps(result, default=str))


if __name__ == '__main__':
    main()
