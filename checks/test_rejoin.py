from copy import deepcopy

import pytest

from ledger.rejoin import validate_layout


def layout():
    return {'services': {'primary': {'volumes': [{'target': '/data', 'source': 'primary_stage5'}],
                                    'entrypoint': ['bash', '/etc/lab/standby_entrypoint.sh']},
                         'runner': {'environment': {'PGHOST': 'standby'}}},
            'volumes': {'primary_stage5': {'name': 'lab_primary_stage5'}}}


def test_separate_volume_with_standby_guard():
    validate_layout(layout(), 'lab_primary_stage1')


@pytest.mark.parametrize('change', ['old_source', 'same_volume', 'primary_entrypoint', 'duplicate_mount', 'old_route'])
def test_refuse_unsafe_rejoin_layout(change):
    config = deepcopy(layout())
    if change == 'old_source':
        config['services']['primary']['volumes'][0]['source'] = 'primary_stage1'
    elif change == 'same_volume':
        config['volumes']['primary_stage5']['name'] = 'lab_primary_stage1'
    elif change == 'primary_entrypoint':
        config['services']['primary']['entrypoint'] = ['docker-entrypoint.sh']
    elif change == 'duplicate_mount':
        config['services']['primary']['volumes'].append({'target': '/data', 'source': 'primary_stage1'})
    else:
        config['services']['runner']['environment']['PGHOST'] = 'primary'
    with pytest.raises(RuntimeError):
        validate_layout(config, 'lab_primary_stage1')
