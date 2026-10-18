"""Isolated timestamp PITR and required-WAL failure experiment; host stdlib only."""
import argparse
from datetime import datetime
import json
import re
import subprocess
import sys
import traceback
import uuid

from .failover import Experiment, ROOT, wait
from .journal import assert_fenced, save
from .oracle import verify


def validate_target(target):
    timestamp = datetime.fromisoformat(target['target_time'])
    if timestamp.tzinfo is None or not re.fullmatch(r'[0-9A-F]{24}', target['wal']):
        raise ValueError('Recovery requires a DB timestamp with timezone and a WAL segment name')
    return timestamp


def validate_missing_failure(state, logs):
    if (state['Running'] or state['ExitCode'] == 0 or
            'Required WAL deliberately withheld:' not in logs or
            'before configured recovery target' not in logs):
        raise AssertionError('Missing WAL must cause an explicit failure before the recovery target')


class PITR:
    def __init__(self, args):
        self.exp = Experiment(args, kind='pitr')
        self.exp.compose_files = ['-f', 'compose.yaml', '-f', 'compose.rejoin.yaml', '-f', 'compose.pitr.yaml']
        self.services = ['pitr-source', 'pitr-recovery', 'pitr-missing']
        self.owns_nodes = False
        self.initial = {1001: 10000, 1002: 10000}

    def rpc(self, action, host='pitr-source', **kwargs):
        return json.loads(self.exp.compose('exec', '-T', 'runner', 'python', '-m', 'ledger.pitr_node',
                                          input=json.dumps(dict(action=action, host=host, **kwargs))))

    def helper(self, script, **env):
        arguments = []
        for key, value in env.items():
            arguments.extend(['--env', key + '=' + value])
        return self.exp.compose('run', '--rm', '--no-deps', '--user', 'postgres', '--entrypoint', 'bash',
                                *arguments, 'pitr-source', '-euc', script)

    def preserved(self, accounts):
        return dict(source=self.exp.db('state', 'standby'), target=self.exp.db('state', 'primary'),
                    snapshot=self.exp.db('snapshot', 'standby', accounts=accounts))

    def transfer(self, amount):
        return [str(uuid.uuid4()), 1001, 1002, amount]

    def run(self):
        exp = self.exp
        config = json.loads(exp.compose('config', '--format', 'json'))
        if exp.compose('ps', '-a', '-q', *self.services):
            raise RuntimeError('PITR nodes already exist; preserve the previous experiment')
        volumes = set(exp.command(['docker', 'volume', 'ls', '--format', '{{.Name}}']).splitlines())
        if any(value['name'] in volumes for name, value in config['volumes'].items() if name.startswith('stage7_')):
            raise RuntimeError('PITR volumes already exist; refusing to overwrite or delete them')
        scope = json.loads((ROOT / 'experiments/stage5-rejoin/replica-snapshot.json').read_text(encoding='utf-8'))
        accounts = [row['id'] for row in scope['accounts']]
        before = self.preserved(accounts)
        if before['source']['recovery'] or not before['target']['recovery']:
            raise RuntimeError('Stage 5 pair must remain in its expected roles')
        save(exp.out / 'stage5-before.json', before)
        self.owns_nodes = True
        exp.compose('up', '-d', '--no-deps', '--wait', 'pitr-source')
        exp.compose('exec', '-T', '-e', 'PGHOST=pitr-source', 'runner', 'python', '-m', 'ledger.setup')
        settings = self.rpc('prepare')
        if settings['archive_mode'] != 'on' or settings['data_checksums'] != 'on':
            raise AssertionError('Archiving and checksums must be enabled')
        save(exp.out / 'settings.json', settings)
        exp.db('seed', 'pitr-source', initial=list(self.initial.items()))
        good = [self.transfer(amount) for amount in [100, 200, 50]]
        excluded = self.transfer(777)
        probe = self.transfer(1)
        save(exp.out / 'inputs.json', dict(initial=self.initial, expected_requests=good,
                                          excluded_after_target=excluded, write_probe=probe))
        exp.db('transfer', 'pitr-source', request=good[0])
        baseline = exp.db('snapshot', 'pitr-source', accounts=list(self.initial))
        verify(baseline, self.initial, good[:1])
        save(exp.out / 'backup-snapshot.json', baseline)
        exp.event('backup_started')
        backup = self.helper('test ! -e /backup/base; export PGPASSWORD="$REPLICATION_PASSWORD"; '
                             'pg_basebackup -h pitr-source -U replicator -D /backup/base -X stream '
                             '--checkpoint=fast --no-clean; pg_verifybackup /backup/base; '
                             'cat /backup/base/backup_label; sha256sum /backup/base/backup_manifest')
        save(exp.out / 'backup-verification.json', dict(output=backup))
        exp.event('backup_verified')
        # Put later transactions in a WAL segment absent from the Base Backup's pg_wal.
        self.rpc('switch')
        for request in good[1:]:
            exp.db('transfer', 'pitr-source', request=request)
        target = self.rpc('target')
        validate_target(target)
        save(exp.out / 'target.json', target)
        expected = exp.db('snapshot', 'pitr-source', accounts=list(self.initial))
        validation = verify(expected, self.initial, good)
        save(exp.out / 'target-snapshot.json', expected)
        exp.db('transfer', 'pitr-source', request=excluded)
        damage = self.rpc('damage', accounts=list(self.initial))
        if damage != dict(deleted_entries=8, deleted_requests=4, deleted_accounts=2):
            raise AssertionError('Expected the deliberate deletion to commit in the isolated scope')
        save(exp.out / 'damage.json', damage)
        damaged = exp.db('snapshot', 'pitr-source', accounts=list(self.initial))
        if any(damaged.values()):
            raise AssertionError('Destructive transaction did not remove the experimental scope')
        save(exp.out / 'damaged-snapshot.json', damaged)
        self.rpc('switch')
        wait(lambda: self.helper('test -f "/archive/$TARGET_WAL"; printf archived', TARGET_WAL=target['wal']),
             'Target WAL archived', 60)
        save(exp.out / 'archiver.json', self.rpc('archive'))
        metadata = self.helper('test ! -e "/backup/base/pg_wal/$TARGET_WAL"; '
                               'test ! -e /backup/target.conf; '
                               "printf \"recovery_target_time = '%s'\\n\" \"$TARGET_TIME\" > /backup/target.conf; "
                               'printf "%s\n" "$TARGET_WAL" > /backup/target-wal; '
                               'sync /backup/target.conf /backup/target-wal; '
                               'sha256sum /archive/*; cat /backup/target.conf',
                               TARGET_WAL=target['wal'], TARGET_TIME=target['target_time'])
        save(exp.out / 'archive-and-target.json', dict(output=metadata, target_wal_not_in_backup=True))
        # Recovery may become a new Primary only after the old source is stopped and fenced.
        exp.primary_id = exp.compose('ps', '-a', '-q', 'pitr-source')
        exp.compose('stop', 'pitr-source')
        proof = exp.inspect()
        if proof['Service'] != 'pitr-source' or proof['Project'] != config['name']:
            raise RuntimeError('Wrong PITR source identity')
        assert_fenced(proof)
        self.helper('printf fenced > "$PGDATA/lab.fenced"; sync "$PGDATA/lab.fenced"')
        assert_fenced(exp.inspect())
        save(exp.out / 'fencing.json', proof)
        exp.event('restore_started')
        exp.compose('up', '-d', '--no-deps', 'pitr-recovery')
        paused = wait(lambda: self.paused(), 'Recovery paused at target time', 90)
        save(exp.out / 'paused-at-target.json', paused)
        actual = exp.db('snapshot', 'pitr-recovery', accounts=list(self.initial))
        if actual != expected:
            raise AssertionError('Timestamp PITR did not restore exactly the target snapshot')
        verify(actual, self.initial, good)
        save(exp.out / 'restored-snapshot.json', actual)
        exp.event('target_verified')
        assert_fenced(exp.inspect())
        self.rpc('resume', 'pitr-recovery')
        wait(lambda: not exp.db('state', 'pitr-recovery')['recovery'], 'Recovery finished')
        exp.db('checkpoint', 'pitr-recovery')
        state = exp.db('state', 'pitr-recovery')
        save(exp.out / 'promoted-state.json', state)
        if state['control']['timeline_id'] != 2:
            raise AssertionError('Recovery should create Timeline 2')
        if exp.db('transfer', 'pitr-recovery', request=probe)['outcome'] != 'new':
            raise AssertionError('Recovered DB did not accept a new business transaction')
        final = exp.db('snapshot', 'pitr-recovery', accounts=list(self.initial))
        final_validation = verify(final, self.initial, good + [probe])
        save(exp.out / 'final-snapshot.json', final)
        # Withhold only the required post-backup segment; never delete the real archive.
        exp.compose('up', '-d', '--no-deps', 'pitr-missing')
        cid = exp.compose('ps', '-a', '-q', 'pitr-missing')
        negative = wait(lambda: self.exited(cid), 'Missing-WAL recovery must fail', 90)
        logs = exp.compose('logs', '--no-color', 'pitr-missing')
        validate_missing_failure(negative, logs)
        save(exp.out / 'missing-wal.json', dict(expected_failure=True, omitted_wal=target['wal'], state=negative))
        (exp.out / 'missing-wal.log').write_text(logs, encoding='utf-8')
        after = self.preserved(accounts)
        if (after['snapshot'] != before['snapshot'] or after['source']['recovery'] or not after['target']['recovery'] or
                after['source']['system'] != before['source']['system'] or after['target']['system'] != before['target']['system']):
            raise AssertionError('Existing stage 5 pair changed')
        save(exp.out / 'stage5-after.json', after)
        result = dict(success=True, run_id=exp.run_id, target=target, method='base-backup-and-archived-wal',
                      backup_requests=1, restored=validation, after_write_probe=final_validation,
                      after_target_transaction_excluded=True, deletion_excluded=True,
                      target_wal_not_in_backup=True, missing_required_wal_failed=True,
                      timeline=state['control']['timeline_id'], stage5_preserved=True,
                      restore_to_target_verified_seconds=exp.events['target_verified']['elapsed_seconds'] -
                                                         exp.events['restore_started']['elapsed_seconds'])
        save(exp.out / 'result.json', result)
        return result

    def paused(self):
        state = self.rpc('recovery', 'pitr-recovery')
        return state if state['recovery'] and state['pause_state'] == 'paused' else None

    def exited(self, cid):
        state = json.loads(self.exp.command(['docker', 'inspect', '--format', '{{json .State}}', cid]))
        return state if not state['Running'] else None

    def cleanup(self):
        if self.owns_nodes:
            self.exp.compose('stop', *self.services)
            logs = self.exp.compose('logs', '--no-color', *self.services)
            (self.exp.out / 'postgres.log').write_text(logs, encoding='utf-8')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--confirm-isolated-delete', action='store_true', required=True)
    experiment = PITR(parser.parse_args())
    code = 0
    try:
        print(json.dumps(experiment.run(), indent=2))
    except (Exception, KeyboardInterrupt):
        save(experiment.exp.out / 'failure.json', dict(traceback=traceback.format_exc()))
        save(experiment.exp.out / 'result.json', dict(success=False))
        traceback.print_exc()
        code = 1
    finally:
        try:
            experiment.cleanup()
        except Exception:
            save(experiment.exp.out / 'cleanup-failure.json', dict(traceback=traceback.format_exc()))
            save(experiment.exp.out / 'result.json', dict(success=False, status='cleanup_failed'))
            code = 1
    return code


if __name__ == '__main__':
    sys.exit(main())
