"""The Worker's capability-evidence hashes must equal Track C's (and therefore the TypeScript side)."""
from pathlib import Path
import subprocess

from apps.trading_worker.venues.binance import local_pilot_readiness as readiness
from scripts import local_pilot_track_c_source as track_c

ROOT = Path(__file__).resolve().parents[2]


def test_worker_hash_inputs_are_exactly_the_track_c_inputs():
    assert tuple(readiness.SOURCE_PATHS) == tuple(track_c.SOURCE_PATHS)
    assert tuple(readiness.DEPENDENCY_PATHS) == tuple(track_c.DEPENDENCY_PATHS)
    assert 'scripts/run_local_pilot_ci_regressions.py' in readiness.SOURCE_PATHS
    assert 'requirements-worker.lock' in readiness.DEPENDENCY_PATHS


def test_worker_hashes_equal_track_c_hashes_on_the_real_repository():
    for paths in (readiness.SOURCE_PATHS, readiness.DEPENDENCY_PATHS, ('infra/postgres/migrations',)):
        assert readiness._hash_files(ROOT, tuple(paths)) == track_c.hash_files(ROOT, tuple(paths))


def test_worker_hash_ignores_untracked_files_and_fails_closed_without_git(tmp_path):
    subprocess.run(['git', 'init', '-q'], cwd=tmp_path, check=True)
    (tmp_path / 'package.json').write_text('{}\n')
    subprocess.run(['git', 'add', 'package.json'], cwd=tmp_path, check=True)
    before = readiness._hash_files(tmp_path, ('package.json',))
    (tmp_path / 'package-lock.json').write_text('untracked\n')
    assert readiness._hash_files(tmp_path, ('package.json', 'package-lock.json')) == before
    assert readiness._hash_files(tmp_path, ('missing.txt',)) is None
    assert readiness._hash_files(tmp_path / 'no-such-directory', ('package.json',)) is None
