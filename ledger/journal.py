"""Durable JSON records shared by the runner and the host control script."""
import json
import os
from pathlib import Path
import time

from .oracle import verify


def save(path, data):
    with Path(path).open('w', encoding='utf-8') as file:
        json.dump(data, file, default=str, indent=2)
        file.flush()
        os.fsync(file.fileno())


def append(path, **data):
    with Path(path).open('a', encoding='utf-8') as file:
        file.write(json.dumps(dict(time_ns=time.time_ns(), **data), default=str) + '\n')
        file.flush()
        os.fsync(file.fileno())


def read(path, complete=False):
    path = Path(path)
    if not path.exists():
        if complete:
            raise FileNotFoundError(path)
        return []
    raw = path.read_bytes()
    if raw and not raw.endswith(b'\n'):
        if complete:
            raise ValueError('Incomplete final client journal')
        raw = raw.rsplit(b'\n', 1)[0] + b'\n' if b'\n' in raw else b''
    return [json.loads(line) for line in raw.splitlines()]


def assess_survival(initial, records, actual):
    intents = {r['request'][0]: r['request'] for r in records if r['event'] == 'intent'}
    acknowledged = {r['request'][0] for r in records if r.get('state') == 'acknowledged'}
    live = {str(r['request_id']) for r in actual['request_history']}
    if not live <= intents.keys():
        raise AssertionError('Promoted database contains an unknown request in this account scope')
    validation = verify(actual, initial, [intents[rid] for rid in live])
    uncertain = {r['request'][0] for r in records if r.get('state') == 'uncertain'}
    results = {r['request'][0] for r in records if r['event'] == 'result'}
    uncertain |= intents.keys() - results
    return dict(acknowledged=len(acknowledged), surviving_acknowledged=len(acknowledged & live),
                lost_acknowledged=sorted(acknowledged - live), uncertain=sorted(uncertain),
                uncertain_surviving=sorted(uncertain & live), actual_requests=len(live), validation=validation)


def assert_fenced(info):
    if info['State']['Running'] or info['State'].get('Paused') or info['State'].get('Restarting'):
        raise RuntimeError('Fencing failed: old Primary must be stopped')
    if info['HostConfig']['RestartPolicy']['Name'] != 'no':
        raise RuntimeError('Fencing failed: restart policy must be no')
