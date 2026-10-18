"""Rejoin the fenced old Primary using a new Base Backup volume; host stdlib only."""
import argparse
import json
import sys
import traceback
import uuid

from .failover import Experiment, ROOT, wait
from .journal import assert_fenced, save
from .oracle import verify


def validate_layout(config, original_volume):
    target = [v for v in config['services']['primary']['volumes'] if v['target'] == '/data']
    if len(target) != 1 or target[0]['source'] != 'primary_stage5':
        raise RuntimeError('Rejoin must use only the new primary_stage5 data volume')
    if config['volumes']['primary_stage5']['name'] == original_volume:
        raise RuntimeError('Refusing to overwrite the old Primary data volume')
    if config['services']['primary']['entrypoint'] != ['bash', '/etc/lab/standby_entrypoint.sh']:
        raise RuntimeError('Rejoined node must start through the Standby guard')
    if config['services']['runner']['environment']['PGHOST'] != 'standby':
        raise RuntimeError('Runner must connect to the promoted Primary by default')


def run(exp):
    exp.primary_id = exp.compose('ps', '-a', '-q', 'primary')
    if not exp.primary_id or '\n' in exp.primary_id:
        raise RuntimeError('Exactly one old Primary is required')
    old = exp.inspect()
    config = json.loads(exp.compose('config', '--format', 'json'))
    if old['Project'] != config['name'] or old['Service'] != 'primary':
        raise RuntimeError('Old Primary does not belong to this Compose project')
    assert_fenced(old)
    mounts = json.loads(exp.command(['docker', 'inspect', '--format', '{{json .Mounts}}', exp.primary_id]))
    original = [m['Name'] for m in mounts if m['Destination'] == '/data' and m['Type'] == 'volume']
    if original != [config['volumes']['primary_stage1']['name']]:
        raise RuntimeError('This one-shot procedure requires the original stage 1 volume')
    marker = exp.compose('run', '--rm', '--no-deps', '--entrypoint', 'bash', 'primary', '-euc',
                         'test -f "$PGDATA/PG_VERSION"; test -f "$PGDATA/lab.fenced"; cat "$PGDATA/lab.fenced"')
    save(exp.out / 'old-primary.json', dict(container=old, volume=original[0], marker=marker))
    exp.compose_files = ['-f', 'compose.yaml', '-f', 'compose.rejoin.yaml']
    merged = json.loads(exp.compose('config', '--format', 'json'))
    validate_layout(merged, original[0])
    save(exp.out / 'layout.json', dict(original_volume=original[0],
                                     new_volume=merged['volumes']['primary_stage5']['name']))
    # The promoted source can restart without the original Standby-only entrypoint.
    exp.compose('up', '-d', '--wait', 'runner', 'standby')
    source = exp.db('state', 'standby')
    prior = json.loads((ROOT / 'experiments/stage4-failover/promoted-state.json').read_text(encoding='utf-8'))
    if (source['recovery'] or source['system'] != prior['system'] or
            source['control']['timeline_id'] != prior['control']['timeline_id']):
        raise RuntimeError('Expected the promoted stage 4 Primary as the backup source')
    if any(slot['slot_name'] == 'primary_stage5' for slot in source['slots']):
        raise RuntimeError('Rejoin slot already exists; preserve the previous run and inspect it first')
    inputs = json.loads((ROOT / 'experiments/stage4-failover/inputs.json').read_text(encoding='utf-8'))
    initial = {int(key): value for key, value in inputs['initial'].items()}
    requests = json.loads((ROOT / 'experiments/stage4-failover/expected-requests.json').read_text(encoding='utf-8'))
    baseline = exp.db('snapshot', 'standby', accounts=list(initial))
    verify(baseline, initial, requests)
    save(exp.out / 'before.json', dict(source=source, snapshot=baseline))
    exp.event('basebackup_started')
    exp.compose('up', '-d', '--no-deps', '--wait', 'primary')
    exp.event('standby_healthy')
    target = wait(lambda: ready(exp, source), 'Rejoined Standby streaming', 60)
    wait(lambda: exp.db('snapshot', 'primary', accounts=list(initial)) == baseline,
         'Previous approved ledger replicated', 60)
    probe = [str(uuid.uuid4()), *list(initial), 17]
    save(exp.out / 'inputs.json', dict(initial=initial, accounts=list(initial), previous_requests=requests, probe=probe))
    if exp.db('transfer', 'standby', request=probe)['outcome'] != 'new':
        raise AssertionError('Probe was not a new committed transaction')
    if exp.db('transfer', 'standby', request=probe)['outcome'] != 'existing':
        raise AssertionError('Probe retry was not idempotent')
    expected = requests + [probe]
    live = exp.db('snapshot', 'standby', accounts=list(initial))
    validation = verify(live, initial, expected)
    replica = wait(lambda: matching_snapshot(exp, list(initial), live), 'New transaction replicated', 60)
    save(exp.out / 'source-snapshot.json', live)
    save(exp.out / 'replica-snapshot.json', replica)
    readonly = exp.db('readonly', 'primary')
    save(exp.out / 'readonly.json', readonly)
    if readonly['sqlstate'] != '25006':
        raise AssertionError('Rejoined Standby did not reject writes as read-only')
    target = ready(exp, source)
    if not target:
        raise AssertionError('Replication stopped before final verification')
    save(exp.out / 'after.json', target)
    # Restart only the rebuilt Standby and prove that its data and role persist.
    exp.compose('restart', 'primary')
    restarted = wait(lambda: ready(exp, source), 'Standby streaming after restart', 60)
    restarted_snapshot = wait(lambda: matching_snapshot(exp, list(initial), live), 'Ledger after Standby restart', 60)
    save(exp.out / 'after-restart.json', dict(state=restarted, snapshot=restarted_snapshot))
    exp.event('verified')
    result = dict(success=True, method='new-base-backup', run_id=exp.run_id,
                  old_volume_preserved=original[0], new_volume=merged['volumes']['primary_stage5']['name'],
                  primary_service='standby', standby_service='primary',
                  basebackup_to_healthy_seconds=exp.events['standby_healthy']['elapsed_seconds'] -
                                              exp.events['basebackup_started']['elapsed_seconds'],
                  new_transaction_replicated=True, standby_write_rejected=True,
                  standby_restart_verified=True, validation=validation)
    save(exp.out / 'result.json', result)
    return result


def ready(exp, source):
    target = exp.db('state', 'primary')
    live = exp.db('state', 'standby')
    matching = [row for row in live['senders'] if row['application_name'] == 'primary_stage5']
    slots = [row for row in live['slots'] if row['slot_name'] == 'primary_stage5']
    if (not live['recovery'] and target['recovery'] and target['system'] == source['system'] and
            target['receiver'] and target['receiver'][0]['status'] == 'streaming' and
            target['receiver'][0]['sender_host'] == 'standby' and len(matching) == 1 and
            matching[0]['state'] == 'streaming' and matching[0]['sync_state'] == 'async' and
            len(slots) == 1 and slots[0]['active'] and slots[0]['wal_status'] != 'lost'):
        return dict(source=live, target=target)
    return None


def matching_snapshot(exp, accounts, expected):
    actual = exp.db('snapshot', 'primary', accounts=accounts)
    return actual if actual == expected else None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--confirm-rejoin', action='store_true', required=True)
    exp = Experiment(parser.parse_args(), kind='rejoin')
    code = 0
    try:
        print(json.dumps(run(exp), indent=2))
    except (Exception, KeyboardInterrupt):
        save(exp.out / 'failure.json', dict(traceback=traceback.format_exc()))
        save(exp.out / 'result.json', dict(success=False))
        traceback.print_exc()
        code = 1
    finally:
        try:
            (exp.out / 'postgres.log').write_text(exp.compose('logs', '--no-color', 'primary', 'standby'), encoding='utf-8')
        except Exception:
            save(exp.out / 'log-failure.json', dict(traceback=traceback.format_exc()))
            save(exp.out / 'result.json', dict(success=False, status='log_capture_failed'))
            code = 1
    return code


if __name__ == '__main__':
    sys.exit(main())
