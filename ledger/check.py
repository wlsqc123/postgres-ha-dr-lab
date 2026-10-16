"""Real DB checks with isolated accounts, durable client records and a Python oracle."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import time
import traceback
import uuid
from threading import Barrier

import psycopg
from .model import PayloadMismatch, connect, snapshot, transfer, verify


def run():
    run_id = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ') + '-' + uuid.uuid4().hex[:8]
    out = Path('results') / ('ledger-' + run_id)
    out.mkdir(parents=True)

    def save(name, data):
        (out / name).write_text(json.dumps(data, default=str, indent=2), encoding='utf-8')

    def record(data):
        with (out / 'client.jsonl').open('a', encoding='utf-8') as log:
            log.write(json.dumps(dict(time_ns=time.time_ns(), **data), default=str) + '\n')
            log.flush()
            os.fsync(log.fileno())

    try:
        source = 1000 + uuid.uuid4().int % 1000000000
        destination = source + 1
        initial = {source: 10000, destination: 10000}
        first = (uuid.uuid4(), source, destination, 250)
        simultaneous = (uuid.uuid4(), source, destination, 125)
        opposite = [(uuid.uuid4(), source, destination, 50), (uuid.uuid4(), destination, source, 50)]
        committed = [first, simultaneous, *opposite]
        rejected = (uuid.uuid4(), source, destination, 10001)
        save('inputs.json', dict(initial=initial, committed_requests=committed, insufficient_funds=rejected))
        with connect(admin=True) as conn:
            with conn.transaction():
                conn.execute('INSERT INTO accounts VALUES (%s,10000),(%s,10000)', (source, destination))

        with connect() as conn:
            record(dict(state='intent', request=first))
            if transfer(conn, *first) != 'new':
                raise AssertionError('First request was not new')
            record(dict(state='acknowledged', request=first, outcome='new'))
            record(dict(state='intent', request=first))
            if transfer(conn, *first) != 'existing':
                raise AssertionError('Retry was applied again')
            record(dict(state='acknowledged', request=first, outcome='existing'))
            conflict = (first[0], source, destination, 251)
            record(dict(state='intent', request=conflict))
            try:
                transfer(conn, *conflict)
            except PayloadMismatch:
                record(dict(state='rejected', request=conflict, reason='payload_mismatch'))
            else:
                raise AssertionError('Conflicting payload accepted')
            record(dict(state='intent', request=rejected))
            try:
                transfer(conn, *rejected)
            except psycopg.errors.CheckViolation:
                record(dict(state='rejected', request=rejected, reason='insufficient_funds'))
            else:
                raise AssertionError('Insufficient balance accepted')
            rollback_check = verify(snapshot(conn, initial), initial, [first])
            save('after-rejections.json', snapshot(conn, initial))

        barrier = Barrier(2)

        def submit():
            with connect() as conn:
                barrier.wait(timeout=10)
                return transfer(conn, *simultaneous)

        # Each intent is recorded before either client submits its request.
        for _ in range(2):
            record(dict(state='intent', request=simultaneous))
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(submit) for _ in range(2)]
            outcomes = [f.result(timeout=20) for f in futures]
        for outcome in outcomes:
            record(dict(state='acknowledged', request=simultaneous, outcome=outcome))
        if sorted(outcomes) != ['existing', 'new']:
            raise AssertionError(f'Concurrent duplicates applied incorrectly: {outcomes}')

        barrier = Barrier(2)

        def submit_opposite(request):
            with connect() as conn:
                barrier.wait(timeout=10)
                return transfer(conn, *request)

        for request in opposite:
            record(dict(state='intent', request=request))
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(submit_opposite, request) for request in opposite]
            opposite_outcomes = [f.result(timeout=20) for f in futures]
        if opposite_outcomes != ['new', 'new']:
            raise AssertionError(f'Opposite-direction transfers failed: {opposite_outcomes}')
        for request, outcome in zip(opposite, opposite_outcomes):
            record(dict(state='acknowledged', request=request, outcome=outcome))

        with connect() as conn:
            primary = snapshot(conn, initial)
        validation = verify(primary, initial, committed)
        save('primary.json', primary)
        deadline = time.monotonic() + 30
        last = None
        while time.monotonic() < deadline:
            try:
                with connect('standby') as conn:
                    standby = snapshot(conn, initial)
                verify(standby, initial, committed)
                break
            except (AssertionError, psycopg.OperationalError, psycopg.errors.UndefinedTable) as exc:
                last = str(exc)
                time.sleep(.2)
        else:
            raise TimeoutError(f'Standby ledger did not catch up: {last}')
        save('standby.json', standby)
        result = dict(success=True, run_id=run_id, scope_accounts=list(initial),
                      initial=initial, expected_requests=committed,
                      retry='existing', payload_mismatch_rejected=True, rollback=rollback_check,
                      concurrent_outcomes=outcomes, opposite_direction_outcomes=opposite_outcomes, primary=validation,
                      standby=verify(standby, initial, committed),
                      client_records=str(out / 'client.jsonl'))
        save('result.json', result)
        print(json.dumps(result, default=str, indent=2))
    except Exception:
        save('failure.json', dict(success=False, traceback=traceback.format_exc()))
        raise


if __name__ == '__main__':
    run()
