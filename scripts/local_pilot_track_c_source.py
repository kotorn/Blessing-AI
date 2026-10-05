"""Read-only clean checkout binding; never reads environment/secret files."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import subprocess

SOURCE_PATHS = ('server.ts', 'Dockerfile.worker', 'src/backend', 'apps/trading_worker', 'domain',
                'scripts/start-local.ps1', 'scripts/apply_local_postgres_migrations.py',
                'config/risk/mainnet_local_policy.json', 'config/risk/live_research_pilot.json',
                'scripts/local_pilot_track_c.py', 'scripts/local_pilot_track_c_source.py',
                'scripts/verify_local_pilot_track_c.py', 'scripts/produce_local_pilot_track_c.py',
                'scripts/run_local_pilot_ci_regressions.py',
                '.github/workflows/ci.yml', '.github/workflows/local-pilot-track-c.yml')
DEPENDENCY_PATHS = ('package.json', 'package-lock.json', 'pyproject.toml', 'requirements-worker.txt',
                    'requirements-worker.lock')
ALL_BOUND_PATHS = (*SOURCE_PATHS, *DEPENDENCY_PATHS, 'infra/postgres/migrations', 'config/risk/live_research_pilot.json')


def git(root: Path, *args: str) -> str:
    environment = {k: v for k, v in os.environ.items()
                   if k.upper() in {'PATH', 'PATHEXT', 'SYSTEMROOT', 'WINDIR', 'SYSTEMDRIVE', 'TEMP', 'TMP'}}
    environment.update(GIT_CONFIG_NOSYSTEM='1', GIT_NO_REPLACE_OBJECTS='1',
                       GIT_CONFIG_GLOBAL='NUL' if os.name == 'nt' else '/dev/null')
    return subprocess.run(['git', *args], cwd=root, env=environment, capture_output=True,
                          text=True, timeout=10, check=True, shell=False).stdout.strip()


def hash_files(root: Path, paths: tuple[str, ...]) -> str:
    files = sorted(set(git(root, 'ls-files', '-z', '--', *paths).split('\0')) - {''})
    if not files:
        raise ValueError('TRACK_C_SOURCE_INPUT_MISSING')
    digest = hashlib.sha256()
    for name in files:
        file = root / name
        if file.is_symlink() or not file.resolve().is_relative_to(root.resolve()):
            raise ValueError('TRACK_C_SOURCE_PATH_INVALID')
        digest.update(name.encode())
        digest.update(b'\0')
        digest.update(file.read_bytes())
        digest.update(b'\0')
    return digest.hexdigest()


def source_binding(root: Path) -> dict:
    if git(root, 'status', '--porcelain', '--untracked-files=all'):
        raise ValueError('LOCAL_PILOT_REVIEWED_COMMIT_NOT_CLEAN')
    eol_info = git(root, 'ls-files', '--eol', '-z', '--', *ALL_BOUND_PATHS)
    if any('w/crlf' in entry for entry in eol_info.split('\0') if entry):
        raise ValueError('TRACK_C_SOURCE_EOL_MISMATCH')
    result = {'gitSha': git(root, 'rev-parse', '--verify', 'HEAD'),
              'sourceSha256': hash_files(root, SOURCE_PATHS),
              'dependencySha256': hash_files(root, DEPENDENCY_PATHS),
              'migrationSha256': hash_files(root, ('infra/postgres/migrations',)),
              'pilotPolicySha256': hashlib.sha256((root/'config/risk/live_research_pilot.json').read_bytes()).hexdigest()}
    if git(root, 'status', '--porcelain', '--untracked-files=all') or git(root, 'rev-parse', 'HEAD') != result['gitSha']:
        raise ValueError('TRACK_C_SOURCE_CHANGED')
    return result
