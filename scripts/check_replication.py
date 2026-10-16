"""Verify actual streaming replication and a real read-only SQL rejection."""
import json
import os
import time
import uuid
from datetime import datetime, timezone

import psycopg
from psycopg.rows import dict_row


def connect(host):
    return psycopg.connect(host=host, dbname=os.environ['PGDATABASE'],
                           user=os.environ['PGUSER'], password=os.environ['PGPASSWORD'],
                           connect_timeout=5, row_factory=dict_row)


rid = uuid.uuid4()
expected = {'request_id': rid, 'value': 'streaming-replication-ready'}
with connect('primary') as conn:
    settings = conn.execute("SELECT version() AS version, current_setting('wal_level') AS wal_level, "
                            "current_setting('synchronous_standby_names') AS synchronous_standby_names, "
                            "current_setting('max_slot_wal_keep_size') AS max_slot_wal_keep_size").fetchone()
    if conn.execute('SELECT pg_is_in_recovery() AS recovery').fetchone()['recovery']:
        raise RuntimeError('primary is in recovery')
    conn.execute('CREATE TABLE IF NOT EXISTS replication_probe '
                 '(request_id uuid PRIMARY KEY, value text NOT NULL)')
    conn.execute('INSERT INTO replication_probe VALUES (%s,%s)', (rid, expected['value']))
    system_id = conn.execute('SELECT system_identifier FROM pg_control_system()').fetchone()

deadline = time.monotonic() + 30
last = None
while time.monotonic() < deadline:
    try:
        with connect('standby') as conn:
            state = conn.execute("SELECT pg_is_in_recovery() AS recovery, "
                                 "current_setting('transaction_read_only') AS read_only, "
                                 'pg_last_wal_receive_lsn() AS receive_lsn, '
                                 'pg_last_wal_replay_lsn() AS replay_lsn').fetchone()
            if not state['recovery'] or state['read_only'] != 'on':
                raise RuntimeError(f'Expected read-only Standby: {state}')
            if conn.execute('SELECT system_identifier FROM pg_control_system()').fetchone() != system_id:
                raise RuntimeError('Primary and Standby belong to different clusters')
            row = conn.execute('SELECT request_id,value FROM replication_probe WHERE request_id=%s', (rid,)).fetchone()
            if row == expected:
                receiver = conn.execute('SELECT status, sender_host, written_lsn, flushed_lsn '
                                        'FROM pg_stat_wal_receiver').fetchall()
                if receiver and receiver[0]['status'] == 'streaming':
                    break
    except (psycopg.OperationalError, psycopg.errors.UndefinedTable) as exc:
        last = str(exc)
    time.sleep(.2)
else:
    raise TimeoutError(f'Committed row did not arrive on the streaming Standby: {last}')

try:
    with connect('standby') as conn:
        conn.execute('INSERT INTO replication_probe VALUES (%s,%s)', (uuid.uuid4(), 'must-be-rejected'))
except psycopg.errors.ReadOnlySqlTransaction as exc:
    rejection = {'sqlstate': exc.sqlstate, 'message': str(exc)}
else:
    raise RuntimeError('Standby accepted an INSERT')

with connect('primary') as conn:
    senders = conn.execute("SELECT application_name,state,sync_state,sent_lsn,write_lsn,flush_lsn,replay_lsn "
                           "FROM pg_stat_replication WHERE application_name='standby_stage2'").fetchall()
    if len(senders) != 1 or senders[0]['state'] != 'streaming' or senders[0]['sync_state'] != 'async':
        raise RuntimeError(f'Expected one asynchronous streaming Standby: {senders}')
    slot = conn.execute("SELECT slot_name,active,restart_lsn,wal_status,safe_wal_size "
                        "FROM pg_replication_slots WHERE slot_name='standby_stage2'").fetchone()
    if not slot or not slot['active'] or slot['wal_status'] == 'lost':
        raise RuntimeError(f'Replication slot is not usable: {slot}')
print(json.dumps(dict(observed_at=datetime.now(timezone.utc).isoformat(), primary_settings=settings,
                      system_id=system_id, expected=expected, actual=row, standby=state, receiver=receiver,
                      senders=senders, slot=slot, rejected_write=rejection, success=True), default=str, indent=2))
