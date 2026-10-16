"""Idempotent transfers and an independent Python oracle for the lab."""
import os
import uuid

import psycopg
from psycopg import pq
from psycopg.rows import dict_row


class PayloadMismatch(ValueError):
    pass


def connect(host=None, admin=False):
    return psycopg.connect(host=host or os.environ['PGHOST'], dbname=os.environ['PGDATABASE'],
                           user=os.environ['PGUSER'] if admin else 'ledger_app',
                           password=os.environ['PGPASSWORD'] if admin else os.environ['APP_PASSWORD'],
                           autocommit=True, row_factory=dict_row, connect_timeout=5,
                           options='-c statement_timeout=10000 -c lock_timeout=5000')


def transfer(conn, request_id, source, destination, amount):
    rid = uuid.UUID(str(request_id))
    if any(type(n) is not int or n <= 0 for n in (source, destination, amount)) or source == destination:
        raise ValueError('Expected distinct positive account IDs and a positive integer amount')
    # Returning success must mean our own transaction committed, not an outer savepoint.
    if conn.info.transaction_status != pq.TransactionStatus.IDLE:
        raise RuntimeError('transfer requires an idle connection and owns its COMMIT')
    with conn.transaction():
        conn.execute('SET TRANSACTION ISOLATION LEVEL READ COMMITTED')
        inserted = conn.execute('INSERT INTO request_history VALUES (%s,%s,%s,%s) '
                                'ON CONFLICT DO NOTHING RETURNING request_id',
                                (rid, source, destination, amount)).fetchone()
        if inserted is None:
            old = conn.execute('SELECT source,destination,amount FROM request_history WHERE request_id=%s', (rid,)).fetchone()
            if old != dict(source=source, destination=destination, amount=amount):
                raise PayloadMismatch('Same request_id with different payload')
            outcome = 'existing'
        else:
            # Balance changes do not change referenced keys; avoid conflicting with FK key-share locks.
            conn.execute('SELECT id FROM accounts WHERE id IN (%s,%s) ORDER BY id FOR NO KEY UPDATE',
                         (source, destination)).fetchall()
            conn.execute('UPDATE accounts SET balance=balance-%s WHERE id=%s', (amount, source))
            conn.execute('UPDATE accounts SET balance=balance+%s WHERE id=%s', (amount, destination))
            conn.execute('INSERT INTO ledger_entries VALUES (%s,%s,%s),(%s,%s,%s)',
                         (rid, source, -amount, rid, destination, amount))
            outcome = 'new'
    return outcome


def snapshot(conn, accounts):
    with conn.transaction():
        conn.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY')
        return {
            'accounts': conn.execute('SELECT * FROM accounts WHERE id=ANY(%s) ORDER BY id', (list(accounts),)).fetchall(),
            'request_history': conn.execute('SELECT * FROM request_history WHERE source=ANY(%s) OR destination=ANY(%s) '
                                            'ORDER BY request_id', (list(accounts), list(accounts))).fetchall(),
            'ledger_entries': conn.execute('SELECT * FROM ledger_entries WHERE account_id=ANY(%s) '
                                           'ORDER BY request_id,account_id', (list(accounts),)).fetchall(),
        }


def verify(actual, initial_balances, requests):
    expected = {}
    for rid, source, destination, amount in requests:
        payload = (source, destination, amount)
        if str(rid) in expected and expected[str(rid)] != payload:
            raise ValueError('Conflicting expected payloads')
        expected[str(rid)] = payload
    balances = dict(initial_balances)
    entries = {}
    for rid, (source, destination, amount) in expected.items():
        balances[source] -= amount
        balances[destination] += amount
        entries[(rid, source)] = -amount
        entries[(rid, destination)] = amount
    observed_requests = {str(r['request_id']): (r['source'], r['destination'], r['amount']) for r in actual['request_history']}
    observed_entries = {(str(r['request_id']), r['account_id']): r['delta'] for r in actual['ledger_entries']}
    if observed_requests != expected or len(observed_requests) != len(actual['request_history']):
        raise AssertionError('Request history mismatch')
    if observed_entries != entries or len(observed_entries) != len(actual['ledger_entries']):
        raise AssertionError('Ledger entry mismatch or duplicate')
    if {r['id']: r['balance'] for r in actual['accounts']} != balances or len(actual['accounts']) != len(balances):
        raise AssertionError('Account balance mismatch')
    return dict(valid=True, requests=len(expected), ledger_entries=len(entries), balances=balances)
