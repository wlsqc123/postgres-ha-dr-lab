from types import SimpleNamespace

import pytest

from ledger.pitr import PITR, validate_missing_failure, validate_target
from ledger.pitr_node import validate_damage_scope


def test_target_uses_db_timezone_and_exact_wal_name():
    assert validate_target({'target_time': '2026-10-18 00:00:00+00:00', 'wal': '000000010000000000000004'}).tzinfo


@pytest.mark.parametrize('timestamp,wal', [('2026-10-18 00:00:00', '0' * 24),
                                        ('2026-10-18 00:00:00+00:00', '../archive'),
                                        ('invalid', '0' * 24)])
def test_refuse_ambiguous_target(timestamp, wal):
    with pytest.raises(ValueError):
        validate_target({'target_time': timestamp, 'wal': wal})


def test_missing_wal_requires_both_failed_exit_and_explicit_target_failure():
    logs = 'Required WAL deliberately withheld: segment\nrecovery ended before configured recovery target was reached'
    validate_missing_failure({'Running': False, 'ExitCode': 1}, logs)
    for state, output in [({'Running': True, 'ExitCode': 1}, logs),
                          ({'Running': False, 'ExitCode': 0}, logs),
                          ({'Running': False, 'ExitCode': 1}, 'unrelated error')]:
        with pytest.raises(AssertionError):
            validate_missing_failure(state, output)


def test_preflight_rejection_preserves_existing_nodes():
    pitr = PITR.__new__(PITR)
    pitr.owns_nodes = False
    pitr.exp = SimpleNamespace(compose=lambda *args: pytest.fail('Do not mutate rejected existing resources'))
    pitr.cleanup()


def test_delete_scope_is_limited_to_isolated_pitr_accounts():
    validate_damage_scope({'host': 'pitr-source', 'accounts': [1001, 1002]})


@pytest.mark.parametrize('host,accounts', [('standby', [1001, 1002]), ('pitr-source', [1, 2])])
def test_refuse_delete_outside_isolated_scope(host, accounts):
    with pytest.raises(ValueError):
        validate_damage_scope({'host': host, 'accounts': accounts})
