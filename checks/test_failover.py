import pytest

from ledger.journal import assert_fenced, assess_survival, read


@pytest.mark.parametrize('running,paused,policy', [(True, False, 'no'), (False, True, 'no'), (False, False, 'always')])
def test_reject_unsafe_fencing(running, paused, policy):
    with pytest.raises(RuntimeError):
        assert_fenced({'State': {'Running': running, 'Paused': paused}, 'HostConfig': {'RestartPolicy': {'Name': policy}}})


def test_stopped_primary_with_restart_disabled():
    assert_fenced({'State': {'Running': False}, 'HostConfig': {'RestartPolicy': {'Name': 'no'}}})


def test_partial_live_journal_is_not_a_lost_final_record(tmp_path):
    path = tmp_path / 'client.jsonl'
    path.write_text('{"event":"intent"}\n{"event":', encoding='utf-8')
    assert read(path) == [{'event': 'intent'}]
    with pytest.raises(ValueError):
        read(path, complete=True)


def test_loss_measured_before_retries_even_when_effects_can_be_recreated():
    first = ['a', 1, 2, 10]
    pending = ['b', 1, 2, 20]
    records = [{'event': 'intent', 'request': first},
               {'event': 'result', 'request': first, 'state': 'acknowledged'},
               {'event': 'intent', 'request': pending}]
    empty = {'accounts': [{'id': 1, 'balance': 100}, {'id': 2, 'balance': 100}],
             'request_history': [], 'ledger_entries': []}
    result = assess_survival({1: 100, 2: 100}, records, empty)
    assert result['lost_acknowledged'] == ['a']
    assert result['uncertain'] == ['b']
    assert result['surviving_acknowledged'] == 0


def test_unknown_promoted_request_is_not_treated_as_expected():
    actual = {'request_history': [{'request_id': 'unknown'}]}
    with pytest.raises(AssertionError, match='unknown request'):
        assess_survival({1: 100, 2: 100}, [], actual)
