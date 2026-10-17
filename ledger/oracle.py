"""Pure Python acceptance oracle, also usable by the host without psycopg."""


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
