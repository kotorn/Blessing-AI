"""Downloader tests use a fake gh (subprocess.run monkeypatched); no network, no real gh."""
import json
import os
from pathlib import Path
import subprocess

import pytest

from scripts import download_local_pilot_track_c as dl

CLASSES = dl.CLASSES
FAKE_GH = 'fake-gh-executable'


def run_git(root: Path, *args: str) -> str:
    return subprocess.run(['git', '-c', 'user.name=T', '-c', 'user.email=t@example.invalid',
                           '-c', 'core.autocrlf=false', '-c', 'commit.gpgsign=false', *args],
                          cwd=root, check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / 'repo'
    root.mkdir()
    run_git(root, 'init', '-q')
    (root / '.gitignore').write_text('artifacts/\n')
    (root / 'a.txt').write_text('a\n')
    run_git(root, 'add', '.')
    run_git(root, 'commit', '-qm', 'fixture')
    return root


def head(root: Path) -> str:
    return run_git(root, 'rev-parse', 'HEAD')


class FakeGh:
    """Records every argv and serves run list / run download from an in-memory table."""

    def __init__(self, sha: str, layout: str = 'flat'):
        self.sha, self.layout, self.calls = sha, layout, []
        self.runs = {'ci.yml': [], 'local-pilot-track-c.yml': []}
        self.artifacts: dict[tuple[int, str], dict[str, bytes]] = {}
        self.next_id = 1000
        self.other_programs: list[str] = []
        self.after_download = None

    def add(self, cls: str, *, subject=None, bundle=b'bundle', run_id=None, **run_fields):
        run_id = run_id or self.next_id
        self.next_id += 1
        workflow = 'ci.yml' if cls == 'CHECKS' else 'local-pilot-track-c.yml'
        run = {'databaseId': run_id, 'headSha': self.sha, 'headBranch': 'main', 'status': 'completed',
               'conclusion': 'success', 'event': 'push' if cls == 'CHECKS' else 'workflow_dispatch'}
        run.update(run_fields)
        self.runs[workflow].append(run)
        name = dl.artifact_name(cls, self.sha)
        statement = subject if subject is not None else json.dumps(
            {'evidenceClass': cls, 'gitSha': self.sha, 'runId': str(run_id)}).encode()
        self.artifacts[(run_id, name)] = {f'{cls}.json': statement, f'{cls}.bundle.json': bundle}
        return run_id

    def populate_all(self):
        for cls in CLASSES:
            self.add(cls)

    def __call__(self, argv, **kwargs):
        if argv[0] != FAKE_GH:
            self.other_programs.append(argv[0])
            return real_run(argv, **kwargs)
        assert kwargs.get('shell') is False and isinstance(argv, list)
        self.calls.append(list(argv))
        assert argv[1:3] in (['run', 'list'], ['run', 'download']), argv
        if argv[1:3] == ['run', 'list']:
            workflow = argv[argv.index('--workflow') + 1]
            return subprocess.CompletedProcess(argv, 0, json.dumps(self.runs[workflow]), '')
        run_id, name = int(argv[3]), argv[argv.index('--name') + 1]
        target = Path(argv[argv.index('--dir') + 1])
        files = self.artifacts.get((run_id, name))
        if files is None:
            return subprocess.CompletedProcess(argv, 1, '', 'no artifact matches')
        for filename, content in files.items():
            base = {'flat': target, 'nested': target / 'local-pilot-attestations',
                    'dir': target / name}[self.layout]
            (base / filename).parent.mkdir(parents=True, exist_ok=True)
            (base / filename).write_bytes(content)
        if self.after_download:
            self.after_download(argv, target)
        return subprocess.CompletedProcess(argv, 0, '', '')

    def downloads(self):
        return [c for c in self.calls if c[2] == 'download']


real_run = subprocess.run


@pytest.fixture
def fake(repo, monkeypatch):
    gh = FakeGh(head(repo))
    monkeypatch.setattr(subprocess, 'run', gh)
    monkeypatch.setattr(dl.shutil, 'which', lambda name: FAKE_GH if name == 'gh' else None)
    monkeypatch.setattr(dl, '_trusted_gh_digest', lambda executable: 'd' * 64)
    monkeypatch.setattr(dl, '_gh_digest', lambda executable: 'd' * 64)
    return gh


def destination(repo: Path) -> Path:
    return repo / 'artifacts/local-pilot-attestations'


@pytest.mark.parametrize('layout', ['flat', 'nested', 'dir'])
def test_downloads_all_five_pairs_to_flattened_paths(repo, fake, layout):
    fake.layout = layout
    fake.populate_all()
    result = dl.download_all(repo, fake.sha)
    assert result['status'] == 'DOWNLOADED'
    names = sorted(p.name for p in destination(repo).iterdir())
    assert names == sorted([f'{c}.json' for c in CLASSES] + [f'{c}.bundle.json' for c in CLASSES])
    for cls in CLASSES:
        assert json.loads((destination(repo) / f'{cls}.json').read_bytes())['evidenceClass'] == cls
        assert (destination(repo) / f'{cls}.bundle.json').read_bytes() == b'bundle'
    assert set(fake.other_programs) <= {'git'}
    assert all(call[0] == FAKE_GH and '--repo' in call and dl.REPOSITORY in call for call in fake.calls)


def test_picks_newest_successful_run_and_ignores_unqualified_runs(repo, fake):
    fake.populate_all()
    fake.add('CHECKS', run_id=2000)
    fake.add('CHECKS', run_id=9001, conclusion='failure')
    fake.add('CHECKS', run_id=9002, headSha='f' * 40)
    fake.add('CHECKS', run_id=9003, headBranch='feature')
    fake.add('CHECKS', run_id=9004, event='workflow_dispatch')
    fake.add('CHECKS', run_id=9005, status='in_progress')
    dl.download_all(repo, fake.sha)
    assert json.loads((destination(repo) / 'CHECKS.json').read_bytes())['runId'] == '2000'


def test_refuses_dirty_tree_before_calling_gh(repo, fake):
    fake.populate_all()
    (repo / 'untracked.txt').write_text('dirty')
    with pytest.raises(dl.DownloadError, match='DOWNLOAD_TREE_NOT_CLEAN'):
        dl.download_all(repo, fake.sha)
    assert fake.calls == [] and not destination(repo).exists()


def test_refuses_sha_that_is_not_head_or_malformed(repo, fake):
    fake.populate_all()
    for bad in ('a' * 40, 'HEAD', head(repo)[:12], head(repo).upper(), ''):
        with pytest.raises(dl.DownloadError, match='DOWNLOAD_SHA'):
            dl.download_all(repo, bad)
    assert fake.calls == []


def test_refuses_to_overwrite_any_existing_file(repo, fake):
    fake.populate_all()
    destination(repo).mkdir(parents=True)
    existing = destination(repo) / 'REVIEW_ORDER_RISK.bundle.json'
    existing.write_bytes(b'precious')
    with pytest.raises(dl.DownloadError, match='DOWNLOAD_WOULD_OVERWRITE'):
        dl.download_all(repo, fake.sha)
    assert existing.read_bytes() == b'precious'
    assert sorted(p.name for p in destination(repo).iterdir()) == ['REVIEW_ORDER_RISK.bundle.json']
    assert fake.downloads() == []


def test_all_or_nothing_when_one_artifact_is_missing(repo, fake):
    for cls in CLASSES[:-1]:
        fake.add(cls)
    with pytest.raises(dl.DownloadError, match='DOWNLOAD_ARTIFACT_NOT_FOUND'):
        dl.download_all(repo, fake.sha)
    assert not destination(repo).exists() or list(destination(repo).iterdir()) == []


@pytest.mark.parametrize('mutation, code', [
    ('big_subject', 'DOWNLOAD_FILE_SIZE'),
    ('empty_bundle', 'DOWNLOAD_FILE_SIZE'),
    ('big_bundle', 'DOWNLOAD_FILE_SIZE'),
    ('wrong_class', 'DOWNLOAD_SUBJECT_MISMATCH'),
    ('wrong_sha', 'DOWNLOAD_SUBJECT_MISMATCH'),
    ('not_json', 'DOWNLOAD_SUBJECT_MISMATCH'),
])
def test_size_caps_and_subject_sanity_reject_everything(repo, fake, mutation, code):
    fake.populate_all()
    run_id = fake.add('REVIEW_AUTH_RELEASE')
    files = fake.artifacts[(run_id, dl.artifact_name('REVIEW_AUTH_RELEASE', fake.sha))]
    good = json.loads(files['REVIEW_AUTH_RELEASE.json'])
    if mutation == 'big_subject': files['REVIEW_AUTH_RELEASE.json'] = b'{' + b' ' * 70000 + b'}'
    if mutation == 'empty_bundle': files['REVIEW_AUTH_RELEASE.bundle.json'] = b''
    if mutation == 'big_bundle': files['REVIEW_AUTH_RELEASE.bundle.json'] = b'x' * (4 * 1024 * 1024 + 1)
    if mutation == 'wrong_class': files['REVIEW_AUTH_RELEASE.json'] = json.dumps({**good, 'evidenceClass': 'CHECKS'}).encode()
    if mutation == 'wrong_sha': files['REVIEW_AUTH_RELEASE.json'] = json.dumps({**good, 'gitSha': 'b' * 40}).encode()
    if mutation == 'not_json': files['REVIEW_AUTH_RELEASE.json'] = b'not json'
    with pytest.raises(dl.DownloadError, match=code):
        dl.download_all(repo, fake.sha)
    assert not destination(repo).exists() or list(destination(repo).iterdir()) == []


@pytest.mark.parametrize('files', [
    {'other/CHECKS.json': b'{}', 'other/CHECKS.bundle.json': b'x'},
    {'CHECKS.json': b'{}', 'local-pilot-attestations/CHECKS.json': b'{}', 'CHECKS.bundle.json': b'x'},
    {'CHECKS.json': b'{}'},
    {'CHECKS.json.bak': b'{}', 'CHECKS.bundle.json': b'x'},
])
def test_unexpected_locations_duplicates_and_missing_files_are_not_accepted(repo, fake, files):
    fake.populate_all()
    key = next(k for k in fake.artifacts if k[1] == dl.artifact_name('CHECKS', fake.sha))
    fake.artifacts[key] = files
    with pytest.raises(dl.DownloadError, match='DOWNLOAD_ARTIFACT_LAYOUT'):
        dl.download_all(repo, fake.sha)
    assert not destination(repo).exists() or list(destination(repo).iterdir()) == []


def test_symlink_in_download_is_refused(repo, fake, tmp_path):
    fake.populate_all()
    secret = tmp_path / 'outside.txt'
    secret.write_text('outside')

    def plant(argv, target):
        try:
            (target / 'link.json').symlink_to(secret)
        except (OSError, NotImplementedError):
            pytest.skip('symlinks unavailable')

    fake.after_download = plant
    with pytest.raises(dl.DownloadError, match='DOWNLOAD_UNSAFE_TREE'):
        dl.download_all(repo, fake.sha)
    assert not destination(repo).exists() or list(destination(repo).iterdir()) == []


def test_untrusted_gh_is_refused_before_any_gh_call(repo, fake, monkeypatch):
    fake.populate_all()
    monkeypatch.setattr(dl, '_trusted_gh_digest', lambda executable: None)
    with pytest.raises(dl.DownloadError, match='DOWNLOAD_GH_UNTRUSTED'):
        dl.download_all(repo, fake.sha)
    assert fake.calls == []
    monkeypatch.setattr(dl.shutil, 'which', lambda name: None)
    with pytest.raises(dl.DownloadError, match='DOWNLOAD_GH_UNTRUSTED'):
        dl.download_all(repo, fake.sha)


def test_gh_binary_change_during_download_is_refused(repo, fake, monkeypatch):
    fake.populate_all()
    digests = iter(['e' * 64])
    monkeypatch.setattr(dl, '_gh_digest', lambda executable: next(digests))
    with pytest.raises(dl.DownloadError, match='DOWNLOAD_GH_CHANGED'):
        dl.download_all(repo, fake.sha)
    assert not destination(repo).exists() or list(destination(repo).iterdir()) == []


def test_tree_dirtied_during_download_is_refused(repo, fake):
    fake.populate_all()
    fake.after_download = lambda argv, target: (repo / 'a.txt').write_text('changed during download')
    with pytest.raises(dl.DownloadError, match='DOWNLOAD_TREE_NOT_CLEAN'):
        dl.download_all(repo, fake.sha)
    assert not destination(repo).exists() or list(destination(repo).iterdir()) == []


def test_duplicate_review_runs_are_refused(repo, fake):
    fake.populate_all()
    fake.add('REVIEW_ORDER_RISK', run_id=9000)
    fake.add('REVIEW_PERSISTENCE', run_id=9000)
    with pytest.raises(dl.DownloadError, match='DOWNLOAD_REVIEW_RUNS_NOT_DISTINCT'):
        dl.download_all(repo, fake.sha)


def test_downloaded_content_is_never_executed_or_imported(repo, fake, monkeypatch):
    fake.populate_all()
    key = next(k for k in fake.artifacts if k[1] == dl.artifact_name('TESTNET_ETHUSDC', fake.sha))
    fake.artifacts[key]['TESTNET_ETHUSDC.bundle.json'] = b'import os; os.system("calc")'
    fake.artifacts[key]['evil.py'] = b'raise SystemExit("executed")'
    dl.download_all(repo, fake.sha)
    assert not (destination(repo) / 'evil.py').exists()
    assert set(fake.other_programs) <= {'git'}
    assert os.environ.get('SHELL_INJECTION_MARKER') is None
