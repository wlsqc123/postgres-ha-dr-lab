"""One-shot isolated async/sync comparison; preserves stage 5 and all volumes."""
import argparse
import json
import subprocess
import sys
import time
import traceback
import uuid

from .failover import Experiment, ROOT, wait
from .journal import assert_fenced, assess_survival, read, save
from .oracle import verify


MODES = ('async', 'sync')


def percentile(samples, q):
    if not samples or not 0 <= q <= 1:
        raise ValueError('Nonempty samples and 0..1 quantile required')
    return sorted(samples)[int((len(samples) - 1) * q)]


class Comparison:
    def __init__(self, args):
        self.exp = Experiment(args, kind='compare')
        self.exp.compose_files = ['-f', 'compose.yaml', '-f', 'compose.rejoin.yaml', '-f', 'compose.compare.yaml']
        self.workers = []
        self.unavailable = set()
        self.owns_nodes = False
        self.services = [f'compare-{mode}-{role}' for mode in MODES for role in ('primary', 'standby')]
        self.requests = {mode: [] for mode in MODES}
        self.samples = {mode: [] for mode in MODES}
        self.results = {mode: {} for mode in MODES}

    def node(self, mode, role):
        return f'compare-{mode}-{role}'

    def rpc(self, **payload):
        return json.loads(self.exp.compose('exec', '-T', 'runner', 'python', '-m', 'ledger.compare_worker',
                                          input=json.dumps(payload)))

    def batch(self, mode, phase, count, background=False):
        directory = self.exp.out / mode
        requests = [[str(uuid.uuid4()), 1001, 1002, 1] for _ in range(count)]
        self.requests[mode].extend(requests)
        save(directory / 'expected-requests.json', self.requests[mode])
        application = 'compare_' + mode + '_' + phase
        payload = dict(action='batch', host=self.node(mode, 'primary'), requests=requests, phase=phase,
                       application_name=application,
                       log=(directory / 'client.jsonl').relative_to(ROOT).as_posix(),
                       output=(directory / (phase + '-output.json')).relative_to(ROOT).as_posix())
        path = directory / (phase + '-input.json')
        save(path, payload)
        if not background:
            result = self.rpc(**payload)
            if result['state'] != 'acknowledged' or len(result['samples']) != count:
                raise AssertionError('Normal batch did not receive all COMMIT responses')
            return result
        stdout = (directory / (phase + '.stdout')).open('w', encoding='utf-8')
        stderr = (directory / (phase + '.stderr')).open('w', encoding='utf-8')
        worker = subprocess.Popen(['docker', 'compose', *self.exp.compose_files, 'exec', '-T', 'runner',
                                   'python', '-m', 'ledger.compare_worker', '--input', path.relative_to(ROOT).as_posix()],
                                  cwd=ROOT, stdout=stdout, stderr=stderr)
        self.workers.append((worker, stdout, stderr))
        return worker, application

    def joined(self, mode):
        state = self.exp.db('state', self.node(mode, 'primary'))
        target = self.exp.db('state', self.node(mode, 'standby'))
        expected = 'sync' if mode == 'sync' else 'async'
        if (not state['recovery'] and target['recovery'] and state['system'] == target['system'] and
                state['synchronous_commit'] == 'on' and len(state['senders']) == 1 and
                state['senders'][0]['state'] == 'streaming' and state['senders'][0]['sync_state'] == expected and
                target['receiver'] and target['receiver'][0]['status'] == 'streaming'):
            return dict(source=state, target=target)
        return None

    def stop_standby(self, service):
        self.exp.compose('stop', service)
        self.unavailable.add(service)

    def resume(self, service):
        self.exp.compose('start', service)
        wait(lambda: self.exp.db('state', service)['recovery'], 'Standby ready after restart')
        self.unavailable.remove(service)

    def pending(self, mode, application):
        rows = self.rpc(action='waits', host=self.node(mode, 'primary'), application_name=application)
        return rows if len(rows) == 1 and rows[0]['wait_event'] == 'SyncRep' else None

    def finish_worker(self, worker):
        worker.wait(timeout=30)
        if worker.returncode:
            raise RuntimeError('Comparison worker failed; inspect its stderr')

    def run(self):
        exp = self.exp
        config = json.loads(exp.compose('config', '--format', 'json'))
        if exp.compose('ps', '-a', '-q', *self.services):
            raise RuntimeError('Comparison containers already exist; this is a one-shot experiment')
        existing = set(exp.command(['docker', 'volume', 'ls', '--format', '{{.Name}}']).splitlines())
        for name, volume in config['volumes'].items():
            if name.startswith('stage6_') and volume['name'] in existing:
                raise RuntimeError('Comparison volume already exists; refusing to reuse or erase it')
        preserved = dict(source=exp.db('state', 'standby'), target=exp.db('state', 'primary'))
        if preserved['source']['recovery'] or not preserved['target']['recovery']:
            raise RuntimeError('Stage 5 pair must be running in its expected roles')
        scope = json.loads((ROOT / 'experiments/stage5-rejoin/replica-snapshot.json').read_text(encoding='utf-8'))
        accounts = [row['id'] for row in scope['accounts']]
        preserved['snapshot'] = exp.db('snapshot', 'standby', accounts=accounts)
        save(exp.out / 'stage5-before.json', preserved)
        self.owns_nodes = True
        for mode in MODES:
            (exp.out / mode).mkdir()
            source = self.node(mode, 'primary')
            exp.compose('up', '-d', '--no-deps', '--wait', source)
            exp.compose('exec', '-T', '-e', 'PGHOST=' + source, 'runner', 'python', '-m', 'ledger.setup')
            exp.compose('exec', '-T', '-e', 'PGHOST=' + source, 'runner', 'python', 'scripts/prepare_replication.py')
            exp.compose('up', '-d', '--no-deps', '--wait', self.node(mode, 'standby'))
            self.rpc(action='configure', host=source, mode=mode)
            ready = wait(lambda: self.joined(mode), mode + ' replication settings')
            save(exp.out / mode / 'preflight.json', ready)
            exp.db('seed', source, initial=[[1001, 1000000], [1002, 1000000]])
            self.batch(mode, 'warmup', 10)
        for round_number in range(3):
            order = MODES if round_number % 2 == 0 else tuple(reversed(MODES))
            for mode in order:
                result = self.batch(mode, 'normal-' + str(round_number), 100)
                self.samples[mode].extend(result['samples'])
        for mode in MODES:
            self.outage(mode)
        for mode in MODES:
            self.failover(mode)
        after = dict(source=exp.db('state', 'standby'), target=exp.db('state', 'primary'))
        after['snapshot'] = exp.db('snapshot', 'standby', accounts=accounts)
        if (after['source']['recovery'] or not after['target']['recovery'] or
                after['source']['system'] != preserved['source']['system'] or
                after['target']['system'] != preserved['target']['system'] or
                after['snapshot'] != preserved['snapshot']):
            raise AssertionError('Stage 5 roles, system IDs or ledger changed')
        save(exp.out / 'stage5-after.json', after)
        for mode in MODES:
            self.results[mode]['normal_latency_ms'] = {
                'count': len(self.samples[mode]),
                **{f'p{int(q*100)}': percentile(self.samples[mode], q) for q in (.5, .95, .99)}}
        save(exp.out / 'result.json', dict(success=True, modes=self.results, stage5_preserved=True))
        return self.results

    def outage(self, mode):
        exp = self.exp
        source, target = self.node(mode, 'primary'), self.node(mode, 'standby')
        # Catch up first, so only requests sent during the outage form the fault window.
        expected = exp.db('snapshot', source, accounts=[1001, 1002])
        wait(lambda: exp.db('snapshot', target, accounts=[1001, 1002]) == expected, 'Baseline replication')
        self.stop_standby(target)
        if mode == 'async':
            result = self.batch(mode, 'outage', 1)
            self.results[mode]['standby_unavailable'] = dict(acknowledged=True, latency_ms=result['samples'][0])
        else:
            worker, application = self.batch(mode, 'outage', 1, background=True)
            pending = wait(lambda: self.pending(mode, application), 'Real synchronous COMMIT wait')
            save(exp.out / mode / 'outage-wait.json', pending)
            time.sleep(2)
            if worker.poll() is not None:
                raise AssertionError('Synchronous request completed while Standby was stopped')
            self.results[mode]['standby_unavailable'] = dict(acknowledged_while_stopped=False, wait_event='SyncRep',
                                                            observed_hold_seconds=2)
        self.resume(target)
        if mode == 'sync':
            self.finish_worker(worker)
            output = json.loads((exp.out / mode / 'outage-output.json').read_text(encoding='utf-8'))
            if output['state'] != 'acknowledged':
                raise AssertionError('Synchronous request did not finish after Standby restarted')
            self.results[mode]['standby_unavailable']['completed_latency_ms'] = output['samples'][0]
        wait(lambda: self.joined(mode), 'Streaming after restart')

    def failover(self, mode):
        exp = self.exp
        source, target = self.node(mode, 'primary'), self.node(mode, 'standby')
        expected = exp.db('snapshot', source, accounts=[1001, 1002])
        wait(lambda: exp.db('snapshot', target, accounts=[1001, 1002]) == expected, 'Pre-fault replication')
        save(exp.out / mode / 'before-fault.json', self.joined(mode))
        self.stop_standby(target)
        if mode == 'async':
            self.batch(mode, 'fault', 10)
            worker = None
        else:
            worker, application = self.batch(mode, 'fault', 1, background=True)
            save(exp.out / mode / 'fault-wait.json',
                 wait(lambda: self.pending(mode, application), 'SyncRep before Primary kill'))
            if worker.poll() is not None:
                raise AssertionError('Expected an unacknowledged synchronous request')
        exp.event(mode + '_fault_requested')
        exp.primary_id = exp.compose('ps', '-a', '-q', source)
        exp.compose('kill', '-s', 'SIGKILL', source)
        proof = exp.inspect()
        if proof['Project'] != json.loads(exp.compose('config', '--format', 'json'))['name'] or proof['Service'] != source:
            raise RuntimeError('Wrong container identity for fencing')
        assert_fenced(proof)
        exp.compose('run', '--rm', '--no-deps', '--entrypoint', 'bash', source, '-euc',
                    'test -f "$PGDATA/PG_VERSION"; printf fenced > "$PGDATA/lab.fenced"; sync "$PGDATA/lab.fenced"')
        assert_fenced(exp.inspect())
        save(exp.out / mode / 'fencing.json', proof)
        if worker is not None:
            self.finish_worker(worker)
            output = json.loads((exp.out / mode / 'fault-output.json').read_text(encoding='utf-8'))
            if output['state'] != 'uncertain':
                raise AssertionError('Killed synchronous COMMIT must be classified uncertain')
        # Restart the Standby only after the source is stopped and fenced.
        self.resume(target)
        assert_fenced(exp.inspect())
        if not exp.db('promote', target)['requested']:
            raise AssertionError('Promotion request failed')
        wait(lambda: not exp.db('state', target)['recovery'], 'Comparison Standby promotion')
        exp.db('checkpoint', target)
        promoted = exp.db('state', target)
        save(exp.out / mode / 'promoted-state.json', promoted)
        if promoted['control']['timeline_id'] != 2:
            raise AssertionError('Expected fresh pair Timeline 1 -> 2')
        actual = exp.db('snapshot', target, accounts=[1001, 1002])
        save(exp.out / mode / 'immediate-snapshot.json', actual)
        records = read(exp.out / mode / 'client.jsonl', complete=True)
        survival = assess_survival({1001: 1000000, 1002: 1000000}, records, actual)
        save(exp.out / mode / 'pre-retry-survival.json', survival)
        if mode == 'async' and len(survival['lost_acknowledged']) != 10:
            raise AssertionError('Expected the 10 acknowledged transactions after Standby stopped to be missing')
        if mode == 'sync' and survival['lost_acknowledged']:
            raise AssertionError('Synchronous acknowledged transactions were lost')
        self.results[mode]['failover'] = dict(acknowledged=survival['acknowledged'],
                                             lost_acknowledged=len(survival['lost_acknowledged']),
                                             uncertain=len(survival['uncertain']))
        exp.db('retry', target, requests=self.requests[mode],
               log=(exp.out / mode / 'retries.jsonl').relative_to(ROOT).as_posix())
        final = exp.db('snapshot', target, accounts=[1001, 1002])
        save(exp.out / mode / 'final-snapshot.json', final)
        self.results[mode]['final'] = verify(final, {1001: 1000000, 1002: 1000000}, self.requests[mode])

    def cleanup(self):
        if not self.owns_nodes:
            return
        try:
            for service in list(self.unavailable):
                self.resume(service)
            for worker, stdout, stderr in self.workers:
                self.finish_worker(worker)
        finally:
            for worker, stdout, stderr in self.workers:
                stdout.close()
                stderr.close()
            # Never restart a killed Primary or delete a volume.
            self.exp.compose('stop', *self.services)
            logs = self.exp.compose('logs', '--no-color', *self.services)
            (self.exp.out / 'postgres.log').write_text(logs, encoding='utf-8')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--confirm-comparison-faults', action='store_true', required=True)
    comparison = Comparison(parser.parse_args())
    code = 0
    try:
        print(json.dumps(comparison.run(), indent=2))
    except (Exception, KeyboardInterrupt):
        save(comparison.exp.out / 'failure.json', dict(traceback=traceback.format_exc()))
        save(comparison.exp.out / 'result.json', dict(success=False))
        traceback.print_exc()
        code = 1
    finally:
        try:
            comparison.cleanup()
        except Exception:
            save(comparison.exp.out / 'cleanup-failure.json', dict(traceback=traceback.format_exc()))
            save(comparison.exp.out / 'result.json', dict(success=False, status='cleanup_failed'))
            code = 1
    return code


if __name__ == '__main__':
    sys.exit(main())
