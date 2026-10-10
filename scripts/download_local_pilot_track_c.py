"""Fetch the five Track C subject+bundle pairs for the checked-out main commit.

This is a convenience downloader, not an authority. It only copies bytes that
GitHub Actions stored as workflow artifacts; scripts/verify_local_pilot_track_c.py
still verifies every signature, certificate claim, binding and freshness window,
and nothing downloaded here is ever executed, imported or trusted.

Usage (operator, after the protected workflows ran):
    python scripts/download_local_pilot_track_c.py --sha <40-hex main HEAD>

Safety rules: refuses a dirty tree or a SHA that is not HEAD; uses only an argv
list with shell=False and the same trusted-gh rules as the verifier; refuses to
overwrite any existing file; enforces the verifier's size caps; validates all
five pairs in a scratch directory before creating any destination file.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import tempfile

# -I execution still imports only the script's explicitly resolved directory.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from local_pilot_track_c import CLASSES, REF, REPOSITORY  # noqa: E402
from local_pilot_track_c_source import git  # noqa: E402
from verify_local_pilot_ci_attestation import _gh_digest, _trusted_gh_digest  # noqa: E402

SUBJECT_MAX_BYTES = 65536          # same cap as verify_local_pilot_track_c.verify_class
BUNDLE_MAX_BYTES = 4194304         # same cap as verify_local_pilot_track_c.verify_class
DOWNLOAD_MAX_FILES = 32
DOWNLOAD_MAX_BYTES = 16 * 1024 * 1024
RUN_LIST_LIMIT = 30
LIST_OUTPUT_MAX = 1024 * 1024
DESTINATION = Path('artifacts/local-pilot-attestations')
# gh needs its own config/credentials; nothing else is forwarded and nothing is printed.
GH_ENVIRONMENT_KEYS = {'PATH', 'PATHEXT', 'SYSTEMROOT', 'WINDIR', 'SYSTEMDRIVE', 'TEMP', 'TMP', 'USERPROFILE',
                       'APPDATA', 'LOCALAPPDATA', 'HOME', 'XDG_CONFIG_HOME', 'GH_CONFIG_DIR', 'GH_TOKEN',
                       'GITHUB_TOKEN', 'HTTPS_PROXY', 'HTTP_PROXY', 'NO_PROXY'}


class DownloadError(Exception):
    """str(error) is a stable machine-readable reason code."""


def artifact_name(evidence_class: str, sha: str) -> str:
    """Names produced by upload-artifact steps in ci.yml and local-pilot-track-c.yml."""
    if evidence_class == 'CHECKS':
        return f'local-pilot-track-c-checks-{sha}'
    if evidence_class == 'TESTNET_ETHUSDC':
        return f'local-pilot-track-c-testnet-{sha}'
    return f'local-pilot-track-c-review-{evidence_class}-{sha}'


def _workflow_and_event(evidence_class: str) -> tuple[str, str]:
    if evidence_class == 'CHECKS':
        return 'ci.yml', 'push'
    return 'local-pilot-track-c.yml', 'workflow_dispatch'


def _assert_clean_head(root: Path, sha: str) -> None:
    try:
        if Path(git(root, 'rev-parse', '--show-toplevel')).resolve() != root.resolve():
            raise DownloadError('DOWNLOAD_ROOT_INVALID')
        if git(root, 'status', '--porcelain', '--untracked-files=all'):
            raise DownloadError('DOWNLOAD_TREE_NOT_CLEAN')
        if git(root, 'rev-parse', '--verify', 'HEAD') != sha:
            raise DownloadError('DOWNLOAD_SHA_NOT_HEAD')
    except (OSError, subprocess.SubprocessError) as error:
        raise DownloadError('DOWNLOAD_GIT_UNAVAILABLE') from error


def _gh_environment() -> dict[str, str]:
    environment = {key: value for key, value in os.environ.items() if key.upper() in GH_ENVIRONMENT_KEYS}
    environment.update(GH_PROMPT_DISABLED='1', GH_NO_UPDATE_NOTIFIER='1', NO_COLOR='1')
    return environment


def _gh(gh: str, args: list[str], timeout: int) -> subprocess.CompletedProcess:
    try:
        return subprocess.run([gh, *args], env=_gh_environment(), capture_output=True, text=True,
                              timeout=timeout, check=False, shell=False)
    except (OSError, subprocess.SubprocessError) as error:
        raise DownloadError('DOWNLOAD_GH_FAILED') from error


def _candidate_runs(gh: str, evidence_class: str, sha: str) -> list[int]:
    workflow, event = _workflow_and_event(evidence_class)
    result = _gh(gh, ['run', 'list', '--repo', REPOSITORY, '--workflow', workflow, '--commit', sha,
                      '--json', 'databaseId,headSha,headBranch,status,conclusion,event',
                      '--limit', str(RUN_LIST_LIMIT)], timeout=60)
    if result.returncode != 0 or len(result.stdout) > LIST_OUTPUT_MAX:
        raise DownloadError('DOWNLOAD_GH_FAILED')
    try:
        runs = json.loads(result.stdout)
    except ValueError as error:
        raise DownloadError('DOWNLOAD_GH_FAILED') from error
    if not isinstance(runs, list):
        raise DownloadError('DOWNLOAD_GH_FAILED')
    branch = REF.removeprefix('refs/heads/')
    qualified = [run['databaseId'] for run in runs
                 if isinstance(run, dict) and type(run.get('databaseId')) is int and run['databaseId'] > 0
                 and run.get('headSha') == sha and run.get('headBranch') == branch
                 and run.get('event') == event and run.get('status') == 'completed'
                 and run.get('conclusion') == 'success']
    return sorted(set(qualified), reverse=True)  # newest first; the verifier enforces the 24h window


def _is_link(path: Path) -> bool:
    return path.is_symlink() or bool(getattr(os.path, 'isjunction', lambda _p: False)(path))


def _read_pair(directory: Path, evidence_class: str, artifact: str) -> tuple[bytes, bytes]:
    """Return (subject, bundle) from exactly the expected relative locations; never follows links."""
    allowed_parents = {'.', 'local-pilot-attestations', artifact, f'{artifact}/local-pilot-attestations'}
    wanted = {f'{evidence_class}.json': [], f'{evidence_class}.bundle.json': []}
    count = total = 0
    for current, directories, filenames in os.walk(directory, followlinks=False):
        for name in [*directories, *filenames]:
            path = Path(current) / name
            if _is_link(path):
                raise DownloadError('DOWNLOAD_UNSAFE_TREE')
        for name in filenames:
            path = Path(current) / name
            mode = os.lstat(path)
            if not stat.S_ISREG(mode.st_mode):
                raise DownloadError('DOWNLOAD_UNSAFE_TREE')
            count, total = count + 1, total + mode.st_size
            if count > DOWNLOAD_MAX_FILES or total > DOWNLOAD_MAX_BYTES:
                raise DownloadError('DOWNLOAD_TREE_TOO_LARGE')
            parent = path.parent.relative_to(directory).as_posix()
            if name in wanted and parent in allowed_parents:
                wanted[name].append(path)
    if any(len(paths) != 1 for paths in wanted.values()):
        raise DownloadError('DOWNLOAD_ARTIFACT_LAYOUT')
    subject_path, bundle_path = wanted[f'{evidence_class}.json'][0], wanted[f'{evidence_class}.bundle.json'][0]
    subject_size, bundle_size = subject_path.stat().st_size, bundle_path.stat().st_size
    if not 0 < subject_size <= SUBJECT_MAX_BYTES or not 0 < bundle_size <= BUNDLE_MAX_BYTES:
        raise DownloadError('DOWNLOAD_FILE_SIZE')
    return subject_path.read_bytes(), bundle_path.read_bytes()


def _check_subject(raw: bytes, evidence_class: str, sha: str, run_id: int) -> None:
    """Sanity only (wrong download early); the signature verifier remains the authority."""
    try:
        statement = json.loads(raw)
        ok = (isinstance(statement, dict) and statement.get('evidenceClass') == evidence_class
              and statement.get('gitSha') == sha and statement.get('runId') == str(run_id))
    except ValueError:
        ok = False
    if not ok:
        raise DownloadError('DOWNLOAD_SUBJECT_MISMATCH')


def _fetch_class(gh: str, evidence_class: str, sha: str, scratch: Path) -> tuple[int, bytes, bytes]:
    artifact = artifact_name(evidence_class, sha)
    for run_id in _candidate_runs(gh, evidence_class, sha):
        target = scratch / f'{evidence_class}-{run_id}'
        target.mkdir()
        result = _gh(gh, ['run', 'download', str(run_id), '--repo', REPOSITORY, '--name', artifact,
                          '--dir', str(target)], timeout=120)
        if result.returncode != 0:
            continue  # this run does not carry the class artifact
        subject, bundle = _read_pair(target, evidence_class, artifact)
        _check_subject(subject, evidence_class, sha, run_id)
        return run_id, subject, bundle
    raise DownloadError('DOWNLOAD_ARTIFACT_NOT_FOUND')


def _destination_names() -> list[str]:
    return [name for c in CLASSES for name in (f'{c}.json', f'{c}.bundle.json')]


def _assert_no_existing_files(root: Path) -> None:
    for parent in (root / 'artifacts', root / DESTINATION):
        if _is_link(parent):
            raise DownloadError('DOWNLOAD_UNSAFE_DESTINATION')
    for name in _destination_names():
        if os.path.lexists(root / DESTINATION / name):
            raise DownloadError('DOWNLOAD_WOULD_OVERWRITE')


def _install(root: Path, files: dict[str, bytes]) -> None:
    directory = root / DESTINATION
    directory.mkdir(parents=True, exist_ok=True)
    if _is_link(root / 'artifacts') or _is_link(directory):
        raise DownloadError('DOWNLOAD_UNSAFE_DESTINATION')
    created: list[Path] = []
    try:
        for name, content in files.items():
            with open(directory / name, 'xb') as handle:  # exclusive create: never overwrites
                created.append(directory / name)
                handle.write(content)
    except FileExistsError as error:
        for path in created:
            path.unlink(missing_ok=True)
        raise DownloadError('DOWNLOAD_WOULD_OVERWRITE') from error
    except OSError as error:
        for path in created:
            path.unlink(missing_ok=True)
        raise DownloadError('DOWNLOAD_WRITE_FAILED') from error


def download_all(root: Path, sha: str) -> dict:
    root = Path(root).resolve()
    if not isinstance(sha, str) or re.fullmatch(r'[0-9a-f]{40}', sha) is None:
        raise DownloadError('DOWNLOAD_SHA_INVALID')
    _assert_clean_head(root, sha)
    gh = shutil.which('gh')
    digest = _trusted_gh_digest(gh) if gh else None
    if gh is None or digest is None:
        raise DownloadError('DOWNLOAD_GH_UNTRUSTED')
    _assert_no_existing_files(root)
    scratch = Path(tempfile.mkdtemp(prefix='blessing-track-c-download-'))
    try:
        fetched = {c: _fetch_class(gh, c, sha, scratch) for c in CLASSES}
        review_runs = [fetched[c][0] for c in CLASSES if c.startswith('REVIEW_')]
        if len(set(review_runs)) != len(review_runs):
            raise DownloadError('DOWNLOAD_REVIEW_RUNS_NOT_DISTINCT')
        if _gh_digest(gh) != digest:
            raise DownloadError('DOWNLOAD_GH_CHANGED')
        _assert_clean_head(root, sha)  # nothing may change between verification inputs and install
        _assert_no_existing_files(root)
        files: dict[str, bytes] = {}
        for c in CLASSES:
            files[f'{c}.json'], files[f'{c}.bundle.json'] = fetched[c][1], fetched[c][2]
        _install(root, files)
        return {'status': 'DOWNLOADED', 'gitSha': sha, 'runs': {c: fetched[c][0] for c in CLASSES},
                'files': sorted(files), 'note': 'NOT VERIFIED: run scripts/verify_local_pilot_track_c.py'}
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--sha', required=True, help='40-hex lowercase main commit; must equal HEAD')
    parser.add_argument('--root', type=Path, default=Path(__file__).resolve().parent.parent)
    args = parser.parse_args(argv)
    try:
        result = download_all(args.root, args.sha)
    except DownloadError as error:
        print(json.dumps({'status': 'REFUSED', 'reason': str(error)}, sort_keys=True))
        return 1
    except Exception:  # noqa: BLE001 - never leak tool output; fail closed
        print(json.dumps({'status': 'REFUSED', 'reason': 'DOWNLOAD_UNEXPECTED_ERROR'}, sort_keys=True))
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == '__main__':
    sys.exit(main())
