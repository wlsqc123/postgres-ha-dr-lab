"""Host-side manual failover experiment. Standard library only; no host psycopg."""
import argparse
import csv
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
import sys
import time
import traceback
import uuid

from .journal import assert_fenced, assess_survival, read, save
from .oracle import verify


ROOT = Path(__file__).resolve().parents[1]


def wait(predicate, description, timeout=40):
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        try:
            value = predicate()
            if value:
                return value
        except RuntimeError as exc:
            last = str(exc)
        time.sleep(.1)
    raise TimeoutError(f'{description}: {last or "condition not met"}')


class Experiment:
    def __init__(self, args):
        self.args = args
        self.started = time.monotonic()
        run_id = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ') + '-' + uuid.uuid4().hex[:8]
        self.out = ROOT / 'results' / ('failover-' + run_id)
        self.out.mkdir(parents=True)
        self.relative = self.out.relative_to(ROOT).as_posix()
        self.events = {}
        self.worker = None
        self.streams = []
        self.run_id = run_id
        save(self.out / 'invocation.json', vars(args))

    def event(self, name):
        self.events[name] = dict(time_ns=time.time_ns(), elapsed_seconds=time.monotonic() - self.started)
        save(self.out / 'events.json', self.events)

    def command(self, args, input=None):
        result = subprocess.run(args, cwd=ROOT, input=input, text=True, encoding='utf-8', errors='replace',
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        logged_stdout = result.stdout
        if args[:3] == ['docker', 'compose', 'config']:
            logged_stdout = '[Compose configuration omitted: contains environment secrets]'
        with (self.out / 'commands.jsonl').open('a', encoding='utf-8') as file:
            file.write(json.dumps(dict(args=args, returncode=result.returncode,
                                       stdout=logged_stdout, stderr=result.stderr)) + '\n')
        if result.returncode:
            raise RuntimeError(f'Command failed ({result.returncode}): {args}\n{result.stderr}\n{result.stdout}')
        return result.stdout.strip()

    def compose(self, *args, input=None):
        return self.command(['docker', 'compose', *args], input)

    def db(self, action, host='primary', **kwargs):
        payload = dict(action=action, host=host, **kwargs)
        with (self.out / 'database-requests.jsonl').open('a', encoding='utf-8') as file:
            file.write(json.dumps(payload) + '\n')
        return json.loads(self.compose('exec', '-T', 'runner', 'python', '-m', 'ledger.node', input=json.dumps(payload)))

    def inspect(self):
        # Explicit fields avoid recording Config.Env and its passwords.
        template = ('{"Id":{{json .Id}},"State":{{json .State}},'
                    '"HostConfig":{"RestartPolicy":{{json .HostConfig.RestartPolicy}}},'
                    '"Project":{{json (index .Config.Labels "com.docker.compose.project")}},'
                    '"Service":{{json (index .Config.Labels "com.docker.compose.service")}},'
                    '"Entrypoint":{{json .Config.Entrypoint}}}')
        return json.loads(self.command(['docker', 'inspect', '--format', template, self.primary_id]))

    def fence(self):
        current = self.compose('ps', '-a', '-q', 'primary')
        if current != self.primary_id:
            raise RuntimeError('Primary container identity changed during the experiment')
        assert_fenced(self.inspect())
        marker = self.compose('run', '--rm', '--no-deps', '--entrypoint', 'bash', 'primary', '-euc',
                              'test -f "$PGDATA/PG_VERSION"; '
                              'printf "%s\\n" "Fenced by the failover lab; rejoin before restarting." > "$PGDATA/lab.fenced"; '
                              'sync "$PGDATA/lab.fenced"; cat "$PGDATA/lab.fenced"')
        proof = self.inspect()
        assert_fenced(proof)
        save(self.out / 'fencing.json', dict(container=proof, marker=marker, marker_path='/data/pgdata/lab.fenced'))
        self.event('fenced')

    def records(self, complete=False):
        return read(self.out / 'client.jsonl', complete)

    def change_route(self, host):
        temporary = self.out / 'route.tmp'
        save(temporary, {'host': host})
        deadline = time.monotonic() + 5
        while True:
            try:
                temporary.replace(self.out / 'route.json')
                return
            except PermissionError:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(.05)

    def stop_worker(self):
        (self.out / 'stop').touch()
        if self.worker is None:
            return
        try:
            self.worker.wait(timeout=30)
        except subprocess.TimeoutExpired:
            # Never kill an arbitrary PID if the worker did not stop cooperatively.
            info = json.loads((self.out / 'worker.json').read_text(encoding='utf-8'))
            script = ('import os,sys; from pathlib import Path; pid=int(sys.argv[1]); '
                      'cmd=Path(f"/proc/{pid}/cmdline").read_bytes(); '
                      'assert b"ledger.workload" in cmd and sys.argv[2].encode() in cmd; os.kill(pid,15)')
            self.compose('exec', '-T', 'runner', 'python', '-c', script, str(info['pid']), self.relative)
            self.worker.wait(timeout=10)
            raise RuntimeError('Worker needed forced termination; this run is invalid')
        for file in self.streams:
            file.close()
        if self.worker.returncode:
            raise RuntimeError('Worker failed; inspect worker.stderr and worker-failure.json')
        done = json.loads((self.out / 'worker-done.json').read_text(encoding='utf-8'))
        if not done['success']:
            raise RuntimeError('Worker did not complete normally')

    def run(self):
        config = json.loads(self.compose('config', '--format', 'json'))
        ids = self.compose('ps', '-a', '-q', 'primary').splitlines()
        if len(ids) != 1:
            raise RuntimeError('Exactly one existing Primary container is required')
        self.primary_id = ids[0]
        info = self.inspect()
        if (not info['State']['Running'] or info['State'].get('Paused') or
                info['Project'] != config['name'] or info['Service'] != 'primary' or
                info['Entrypoint'] != ['bash', '/etc/lab/primary_entrypoint.sh'] or
                info['HostConfig']['RestartPolicy']['Name'] != 'no'):
            raise RuntimeError('Prepare the running Primary with the fenced entrypoint and restart=no first')
        primary = self.db('state')
        standby = self.db('state', 'standby')
        matching = [r for r in primary['senders'] if r['application_name'] == 'standby_stage2']
        if (primary['recovery'] or not standby['recovery'] or
                primary['system'] != standby['system'] or primary['synchronous_standby_names'] or
                len(matching) != 1 or matching[0]['state'] != 'streaming' or matching[0]['sync_state'] != 'async' or
                not standby['receiver'] or standby['receiver'][0]['status'] != 'streaming'):
            raise RuntimeError('This experiment requires the original asynchronous Primary/Standby pair')
        save(self.out / 'preflight.json', dict(primary=primary, standby=standby, container=info))
        source = 1000 + uuid.uuid4().int % 1000000000
        accounts = [source, source + 1]
        initial = {account: 1000000 for account in accounts}
        save(self.out / 'inputs.json', dict(run_id=self.run_id, accounts=accounts, initial=initial, rate=self.args.rate))
        self.db('seed', initial=list(initial.items()))
        wait(lambda: self.db('snapshot', 'standby', accounts=accounts)['accounts'] ==
             [{'id': account, 'balance': balance} for account, balance in initial.items()], 'Initial account replication')
        self.change_route('primary')
        self.streams = [(self.out / name).open('w', encoding='utf-8') for name in ('worker.stdout', 'worker.stderr')]
        self.event('workload_started')
        self.worker = subprocess.Popen(['docker', 'compose', 'exec', '-T', 'runner', 'python', '-m', 'ledger.workload',
                                        '--run', self.relative, '--rate', str(self.args.rate)], cwd=ROOT,
                                       stdout=self.streams[0], stderr=self.streams[1])
        wait(lambda: any(r.get('state') == 'acknowledged' for r in self.records()), 'First normal workload success', 20)
        time.sleep(self.args.warmup_seconds)
        save(self.out / 'before-fault.json', dict(primary=self.db('state'), standby=self.db('state', 'standby')))
        before_count = len(self.records())
        self.event('fault_requested')
        self.compose('kill', '-s', 'SIGKILL', 'primary')
        self.event('kill_completed')
        failure = wait(lambda: next((r for r in self.records()[before_count:]
                                    if r.get('state') in ('failed', 'uncertain')), None), 'Actual client failure', 40)
        save(self.out / 'first-client-failure.json', failure)
        self.event('failure_observed')
        self.fence()
        # There is no promotion path before the stopped-container and marker proof above.
        assert_fenced(self.inspect())
        self.event('promotion_started')
        if not self.db('promote', 'standby')['requested']:
            raise RuntimeError('Promotion request was rejected')
        wait(lambda: not self.db('state', 'standby')['recovery'], 'Standby promotion')
        self.event('promotion_completed')
        self.db('checkpoint', 'standby')
        promoted = self.db('state', 'standby')
        save(self.out / 'promoted-state.json', promoted)
        if promoted['control']['timeline_id'] <= primary['control']['timeline_id']:
            raise AssertionError('Expected a new Timeline after promotion')
        immediate = self.db('snapshot', 'standby', accounts=accounts)
        save(self.out / 'immediate-snapshot.json', immediate)
        before_route = self.records()
        save(self.out / 'pre-retry-client.json', before_route)
        survival = assess_survival(initial, before_route, immediate)
        save(self.out / 'pre-retry-survival.json', survival)
        probe = [str(uuid.uuid4()), *accounts, 1]
        if self.db('transfer', 'standby', request=probe)['outcome'] != 'new':
            raise AssertionError('New Primary did not accept a new business transaction')
        save(self.out / 'write-probe.json', dict(request=probe, acknowledged=True))
        self.change_route('standby')
        self.event('route_changed')
        first_success = wait(lambda: next((r for r in self.records() if r.get('state') == 'acknowledged' and
                                          r['host'] == 'standby' and r['outcome'] == 'new'), None),
                             'First application success on the new Primary', 40)
        self.event('first_application_success_observed')
        save(self.out / 'first-application-success.json', first_success)
        time.sleep(2)
        self.stop_worker()
        records = self.records(complete=True)
        if any(r.get('state') == 'rejected' for r in records):
            raise AssertionError('Business rejection makes this workload sample invalid')
        requests = [r['request'] for r in records if r['event'] == 'intent']
        expected = requests + [probe]
        save(self.out / 'expected-requests.json', expected)
        retry = self.db('retry', 'standby', requests=requests, log=self.relative + '/retries.jsonl')
        save(self.out / 'retries.json', retry)
        final = self.db('snapshot', 'standby', accounts=accounts)
        save(self.out / 'final-snapshot.json', final)
        validation = verify(final, initial, expected)
        assert_fenced(self.inspect())
        save(self.out / 'final-verification.json', validation)
        events = {name: value['elapsed_seconds'] for name, value in self.events.items()}
        samples = sorted(r['latency_ms'] for r in before_route if r.get('state') == 'acknowledged')
        metrics = dict(success=True, run_id=self.run_id, mode='async', requested_rate=self.args.rate,
                       fault_detection_seconds=events['failure_observed'] - events['fault_requested'],
                       promotion_seconds=events['promotion_completed'] - events['promotion_started'],
                       routing_seconds=events['route_changed'] - events['promotion_completed'],
                       application_reconnect_seconds=events['first_application_success_observed'] - events['route_changed'],
                       recovery_observed_seconds=events['first_application_success_observed'] - events['fault_requested'],
                       acknowledged_before_retry=survival['acknowledged'], lost_acknowledged=len(survival['lost_acknowledged']),
                       uncertain_before_retry=len(survival['uncertain']), workload_attempts=len(requests),
                       pre_fault_latency_ms={f'p{int(q*100)}': samples[int((len(samples)-1)*q)] for q in (.5,.95,.99)},
                       retry_new=sum(r['outcome'] == 'new' for r in retry),
                       retry_existing=sum(r['outcome'] == 'existing' for r in retry), final=validation,
                       old_timeline=primary['control']['timeline_id'], new_timeline=promoted['control']['timeline_id'])
        save(self.out / 'metrics.json', metrics)
        with (self.out / 'requests.csv').open('w', encoding='utf-8', newline='') as file:
            writer = csv.DictWriter(file, fieldnames=['request_id','host','state','outcome','latency_ms','time_ns'])
            writer.writeheader()
            for row in records:
                if row['event'] == 'result':
                    writer.writerow(dict(request_id=row['request'][0], **{key: row.get(key, '') for key in writer.fieldnames[1:]}))
        save(self.out / 'result.json', dict(success=True, status='verified'))
        return metrics


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--confirm-primary-kill', action='store_true', required=True)
    parser.add_argument('--rate', type=float, default=20)
    parser.add_argument('--warmup-seconds', type=float, default=5)
    args = parser.parse_args()
    if args.rate <= 0 or not 1 <= args.warmup_seconds <= 30:
        parser.error('rate must be positive; warmup must be 1..30 seconds')
    experiment = Experiment(args)
    code = 0
    try:
        metrics = experiment.run()
        print(json.dumps(dict(results=str(experiment.out), metrics=metrics), indent=2))
    except (Exception, KeyboardInterrupt):
        code = 1
        save(experiment.out / 'failure.json', dict(traceback=traceback.format_exc()))
        save(experiment.out / 'result.json', dict(success=False, status='failed'))
        traceback.print_exc()
    finally:
        try:
            if experiment.worker is not None and experiment.worker.poll() is None:
                experiment.stop_worker()
            logs = experiment.compose('logs', '--no-color', 'primary', 'standby')
            (experiment.out / 'postgres.log').write_text(logs, encoding='utf-8')
        except Exception:
            save(experiment.out / 'cleanup-failure.json', {'traceback': traceback.format_exc()})
            save(experiment.out / 'result.json', dict(success=False, status='cleanup_failed'))
            code = 1
    return code


if __name__ == '__main__':
    sys.exit(main())
