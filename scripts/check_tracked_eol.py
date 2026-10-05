"""Fail-closed line-ending check for tracked files; read-only, never touches the network.

Track C hashes working-tree bytes, so a CRLF working copy of an LF blob silently
changes the source binding relative to CI. This reports every tracked text file
whose index or working-tree EOL is not plain LF, and (independently of git's
text/binary heuristic) every file in the Track C hashed set whose raw
working-tree bytes contain CRLF.
"""
from __future__ import annotations

import argparse
from pathlib import Path
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
from local_pilot_track_c_source import DEPENDENCY_PATHS, SOURCE_PATHS  # noqa: E402

HASHED_PATHS = (*SOURCE_PATHS, *DEPENDENCY_PATHS, 'infra/postgres/migrations')
BAD_EOL = {'crlf', 'mixed'}


def _tracked(root: Path, *paths: str) -> list[str]:
    output = subprocess.run(['git', 'ls-files', '-z', *(('--', *paths) if paths else ())], cwd=root,
                            capture_output=True, check=True, shell=False, timeout=30).stdout
    return sorted({p.decode() for p in output.split(b'\0') if p})


def eol_violations(root: Path) -> list[str]:
    """Return sorted human-readable violations; an empty list means clean."""
    violations: set[str] = set()
    listing = subprocess.run(['git', 'ls-files', '--eol', '-z'], cwd=root, capture_output=True,
                             check=True, shell=False, timeout=30).stdout
    for record in listing.split(b'\0'):
        if not record:
            continue
        info, _, name = record.decode().partition('\t')
        index_eol, worktree_eol = (field.partition('/')[2] for field in info.split()[:2])
        if index_eol in BAD_EOL:
            violations.add(f'i/{index_eol} {name}')
        if worktree_eol in BAD_EOL:
            violations.add(f'w/{worktree_eol} {name}')
    for name in _tracked(root, *HASHED_PATHS):
        file = root / name
        if file.is_file() and not file.is_symlink() and b'\r\n' in file.read_bytes():
            violations.add(f'hashed-source-crlf {name}')
    return sorted(violations)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', type=Path, default=Path(__file__).resolve().parent.parent)
    found = eol_violations(parser.parse_args().root.resolve())
    for line in found:
        print(line)
    if found:
        print(f'EOL_CHECK_FAILED: {len(found)} tracked file(s) are not LF', file=sys.stderr)
    sys.exit(1 if found else 0)
