"""Run in runner against the Primary before starting the Standby."""
import json
import os

import psycopg
from psycopg import sql
from psycopg.rows import dict_row


with psycopg.connect(autocommit=True, row_factory=dict_row, connect_timeout=5) as conn:
    if conn.execute('SELECT pg_is_in_recovery() AS recovery').fetchone()['recovery']:
        raise RuntimeError('Replication must be prepared on the Primary')
    role = conn.execute("SELECT rolcanlogin, rolreplication FROM pg_roles WHERE rolname='replicator'").fetchone()
    if role is None:
        conn.execute(sql.SQL('CREATE ROLE replicator LOGIN REPLICATION PASSWORD {}').format(
            sql.Literal(os.environ['REPLICATION_PASSWORD'])))
    elif not role['rolcanlogin'] or not role['rolreplication']:
        raise RuntimeError('Existing replicator role has unexpected permissions; refusing to change it')
    slot = conn.execute("SELECT slot_type, wal_status FROM pg_replication_slots WHERE slot_name='standby_stage2'").fetchone()
    if slot is None:
        conn.execute("SELECT pg_create_physical_replication_slot('standby_stage2')")
    elif slot['slot_type'] != 'physical' or slot['wal_status'] == 'lost':
        raise RuntimeError(f'Existing slot cannot be reused: {slot}')
    result = conn.execute("SELECT slot_name, slot_type, active, restart_lsn, wal_status, safe_wal_size "
                          "FROM pg_replication_slots WHERE slot_name='standby_stage2'").fetchone()
    print(json.dumps(dict(slot=result, success=True), default=str, indent=2))
