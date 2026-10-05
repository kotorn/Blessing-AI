"""Track C hashes working-tree bytes: CRLF in a tracked source file changes the binding."""
from pathlib import Path
import subprocess

from scripts.check_tracked_eol import HASHED_PATHS, eol_violations

ROOT = Path(__file__).resolve().parents[2]


def _git(root: Path, *args: str) -> None:
    subprocess.run(['git', '-c', 'user.email=t@example.invalid', '-c', 'user.name=t',
                    '-c', 'core.autocrlf=false', '-c', 'commit.gpgsign=false', *args],
                   cwd=root, check=True, capture_output=True, shell=False)


def _repo(tmp_path: Path) -> Path:
    _git(tmp_path, 'init', '-q')
    return tmp_path


def test_real_repository_has_no_crlf_in_index_or_working_tree():
    assert eol_violations(ROOT) == []


def test_hashed_source_set_includes_the_files_that_previously_drifted():
    for name in ('apps/trading_worker', 'domain', 'requirements-worker.lock'):
        assert name in HASHED_PATHS
    tracked = subprocess.run(['git', 'ls-files', '-z', '--', *HASHED_PATHS], cwd=ROOT, capture_output=True,
                             check=True, shell=False).stdout.decode().split('\0')
    for name in ('apps/trading_worker/api.py', 'domain/events.py', 'domain/interfaces.py'):
        assert name in tracked


def test_crlf_in_working_tree_is_reported(tmp_path):
    root = _repo(tmp_path)
    (root / 'domain').mkdir()
    (root / 'domain/a.py').write_bytes(b'x = 1\n')
    (root / '.gitattributes').write_bytes(b'* text eol=lf\n')
    _git(root, 'add', '.')
    _git(root, 'commit', '-qm', 'lf')
    assert eol_violations(root) == []
    (root / 'domain/a.py').write_bytes(b'x = 1\r\n')
    found = eol_violations(root)
    assert 'hashed-source-crlf domain/a.py' in found
    assert any(line.startswith('w/crlf ') for line in found)


def test_crlf_in_index_is_reported(tmp_path):
    root = _repo(tmp_path)
    (root / 'apps/trading_worker').mkdir(parents=True)
    (root / 'apps/trading_worker/b.py').write_bytes(b'y = 2\r\n')
    (root / '.gitattributes').write_bytes(b'* -text\n')
    _git(root, 'add', '.')
    _git(root, 'commit', '-qm', 'crlf blob')
    (root / '.gitattributes').write_bytes(b'* text eol=lf\n')
    found = eol_violations(root)
    assert any(line.startswith('i/crlf ') and line.endswith('apps/trading_worker/b.py') for line in found)


def test_crlf_in_untracked_file_is_not_a_tracked_violation(tmp_path):
    root = _repo(tmp_path)
    (root / 'a.txt').write_bytes(b'a\n')
    _git(root, 'add', '.')
    _git(root, 'commit', '-qm', 'lf')
    (root / 'untracked.txt').write_bytes(b'u\r\n')
    assert eol_violations(root) == []
