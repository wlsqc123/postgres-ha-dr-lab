"""Sequential fixed-target-rate writer. No Docker control privileges."""
import argparse
import json
from pathlib import Path
import os
import time
import traceback
import uuid

import psycopg
from .journal import append, save
from .model import PayloadMismatch, connect, transfer


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run', type=Path, required=True)
    parser.add_argument('--rate', type=float, required=True)
    args = parser.parse_args()
    if args.rate <= 0:
        parser.error('rate must be positive')
    out = args.run
    inputs = json.loads((out / 'inputs.json').read_text(encoding='utf-8'))
    save(out / 'worker.json', dict(pid=os.getpid(), started_ns=time.time_ns()))
    end = time.monotonic() + 120
    index = 0
    success = False
    try:
        while time.monotonic() < end and not (out / 'stop').exists():
            start = time.monotonic()
            index += 1
            host = json.loads((out / 'route.json').read_text(encoding='utf-8'))['host']
            request = [str(uuid.uuid5(uuid.NAMESPACE_URL, inputs['run_id'] + ':' + str(index))),
                       inputs['accounts'][0], inputs['accounts'][1], 1]
            append(out / 'client.jsonl', event='intent', request=request, host=host)
            try:
                conn = connect(host)
            except psycopg.Error as exc:
                result = dict(state='failed', error=str(exc))
            else:
                try:
                    with conn:
                        outcome = transfer(conn, *request)
                    result = dict(state='acknowledged', outcome=outcome)
                except (PayloadMismatch, psycopg.IntegrityError) as exc:
                    result = dict(state='rejected', error=str(exc))
                except (psycopg.OperationalError, psycopg.InterfaceError) as exc:
                    result = dict(state='uncertain', error=str(exc))
                except psycopg.Error as exc:
                    result = dict(state='rejected', error=str(exc))
            append(out / 'client.jsonl', event='result', request=request, host=host,
                   latency_ms=(time.monotonic() - start) * 1000, **result)
            time.sleep(max(0, 1 / args.rate - (time.monotonic() - start)))
        if not (out / 'stop').exists():
            raise TimeoutError('Worker safety deadline reached before the controller stopped it')
        success = True
    except Exception:
        save(out / 'worker-failure.json', {'traceback': traceback.format_exc()})
        raise
    finally:
        save(out / 'worker-done.json', dict(success=success, attempts=index))


if __name__ == '__main__':
    main()
