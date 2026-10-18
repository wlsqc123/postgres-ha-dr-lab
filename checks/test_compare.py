from types import SimpleNamespace
from io import StringIO

import pytest

from ledger.compare import Comparison, percentile


def test_percentile_uses_sorted_observations_without_inventing_samples():
    assert percentile([40, 10, 30, 20], .5) == 20
    assert percentile([40, 10, 30, 20], .95) == 30


@pytest.mark.parametrize('samples,q', [([], .5), ([1], -1), ([1], 2)])
def test_invalid_percentiles(samples, q):
    with pytest.raises(ValueError):
        percentile(samples, q)


def test_cleanup_does_not_stop_existing_nodes_when_preflight_rejects():
    comparison = Comparison.__new__(Comparison)
    comparison.owns_nodes = False
    comparison.exp = SimpleNamespace(compose=lambda *args: pytest.fail('Preflight rejection must not mutate nodes'))
    comparison.cleanup()


def test_worker_failure_still_stops_owned_nodes_and_preserves_logs(tmp_path):
    comparison = Comparison.__new__(Comparison)
    comparison.owns_nodes = True
    comparison.unavailable = set()
    comparison.services = ['compare-sync-primary']
    stdout, stderr = StringIO(), StringIO()
    comparison.workers = [(SimpleNamespace(wait=lambda **kwargs: None, returncode=1), stdout, stderr)]
    calls = []

    def compose(*args):
        calls.append(args)
        return 'preserved log'

    comparison.exp = SimpleNamespace(compose=compose, out=tmp_path)
    with pytest.raises(RuntimeError, match='worker failed'):
        comparison.cleanup()
    assert calls[0] == ('stop', 'compare-sync-primary')
    assert (tmp_path / 'postgres.log').read_text() == 'preserved log'
    assert stdout.closed and stderr.closed
