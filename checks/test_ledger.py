import uuid
import pytest
from ledger.model import transfer, verify


def sample():
    return {'accounts': [{'id': 1, 'balance': 90}, {'id': 2, 'balance': 110}],
            'request_history': [{'request_id': 'a', 'source': 1, 'destination': 2, 'amount': 10}],
            'ledger_entries': [{'request_id': 'a', 'account_id': 1, 'delta': -10},
                               {'request_id': 'a', 'account_id': 2, 'delta': 10}]}


def test_oracle():
    assert verify(sample(), {1: 100, 2: 100}, [('a', 1, 2, 10)])['valid']


@pytest.mark.parametrize('table,field', [('accounts', 'balance'), ('ledger_entries', 'delta'), ('request_history', 'amount')])
def test_oracle_detects_corruption(table, field):
    actual = sample()
    actual[table][0][field] += 1
    with pytest.raises(AssertionError):
        verify(actual, {1: 100, 2: 100}, [('a', 1, 2, 10)])


def test_oracle_rejects_conflicting_expectations():
    with pytest.raises(ValueError):
        verify(sample(), {1: 100, 2: 100}, [('a', 1, 2, 10), ('a', 1, 2, 11)])


@pytest.mark.parametrize('source,destination,amount', [(1, 2, 0), (1, 2, -1), (1, 2, True), (1, 1, 10)])
def test_invalid_request_rejected_before_sql(source, destination, amount):
    with pytest.raises(ValueError):
        transfer(None, uuid.uuid4(), source, destination, amount)
