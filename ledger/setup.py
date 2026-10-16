"""Create the ledger schema without deleting existing data."""
import json
import os
from pathlib import Path

from psycopg import sql
from .model import connect


with connect(admin=True) as conn:
    with conn.transaction():
        if conn.execute('SELECT pg_is_in_recovery() AS recovery').fetchone()['recovery']:
            raise RuntimeError('Ledger setup requires the Primary')
        role = conn.execute("SELECT rolcanlogin,rolsuper,rolreplication FROM pg_roles WHERE rolname='ledger_app'").fetchone()
        if role is None:
            conn.execute(sql.SQL('CREATE ROLE ledger_app LOGIN PASSWORD {}').format(sql.Literal(os.environ['APP_PASSWORD'])))
        elif not role['rolcanlogin'] or role['rolsuper'] or role['rolreplication']:
            raise RuntimeError('Existing ledger_app has unexpected privileges')
        conn.execute((Path(__file__).resolve().parents[1] / 'sql' / '010-ledger.sql').read_text())
        conn.execute('GRANT SELECT, UPDATE ON accounts TO ledger_app')
        conn.execute('GRANT SELECT, INSERT ON request_history, ledger_entries TO ledger_app')
print(json.dumps({'success': True, 'schema': ['accounts', 'request_history', 'ledger_entries'], 'role': 'ledger_app'}))
