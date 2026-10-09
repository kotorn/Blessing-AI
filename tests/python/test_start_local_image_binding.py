"""The local Worker image must be bound to the reviewed git HEAD before the control plane can use it.

Static only (no docker, no PowerShell execution): the launcher is read as text and the order of the
dirty-tree refusal, the build labels, the post-build verification and the worker start is asserted.
"""
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[2]
LAUNCHER = ROOT / 'scripts' / 'start-local.ps1'


def _text() -> str:
    return LAUNCHER.read_text(encoding='utf-8')


def _index(text: str, needle: str) -> int:
    position = text.find(needle)
    assert position >= 0, f'launcher must contain: {needle!r}'
    return position


def _build_line(text: str) -> str:
    lines = [line for line in text.splitlines() if '--file Dockerfile.worker' in line and 'build' in line]
    assert len(lines) == 1, 'exactly one docker build of Dockerfile.worker is expected'
    return lines[0]


def test_launcher_is_lf_only():
    assert b'\r' not in LAUNCHER.read_bytes()


def test_dirty_tree_is_refused_before_any_docker_or_postgres_step():
    text = _text()
    status = _index(text, 'status --porcelain --untracked-files=all')
    refusal = text.index('throw "The working tree is not clean', status)
    assert refusal - status < 600, 'the dirty-tree refusal must throw right after the status check'
    assert status < _index(text, 'compose --profile local up -d postgres-local')
    assert status < _index(text, '$dockerVersion = Get-DockerEngineVersion')
    assert status < _index(text, 'build --file Dockerfile.worker')


def test_build_carries_exact_head_and_source_labels_and_short_sha_tag():
    build = _build_line(_text())
    assert '--label "org.blessing.git.sha=$launchHeadSha"' in build
    assert '--label "org.blessing.source.sha256=$($launchBinding.sourceSha256)"' in build
    assert '--tag $workerCommitTag' in build
    assert '--tag $workerImageTag' in build
    assert '$workerCommitTag = "blessing-worker:" + $launchHeadSha.Substring(0, 12)' in _text()


def test_head_is_read_from_git_rev_parse_and_must_be_a_full_sha():
    text = _text()
    assert '$launchHeadSha = (& $gitCommand.Source rev-parse --verify HEAD' in text
    assert re.search(r"\$launchHeadSha -notmatch '\^\[0-9a-f\]\{40\}\$'", text)


def test_source_fingerprint_comes_from_the_existing_helper_not_a_new_algorithm():
    text = _text()
    assert 'from scripts.local_pilot_track_c_source import source_binding' in text
    assert "[string]$launchBinding.gitSha -ne $launchHeadSha" in text


def test_post_build_verification_is_between_build_and_worker_start_in_order():
    text = _text()
    build = _index(text, 'build --file Dockerfile.worker')
    label_read = _index(text, 'image inspect --format "{{json .Config.Labels}}" $workerImageTag')
    label_check = _index(text, "$builtLabelTable['org.blessing.git.sha'] -ne $launchHeadSha")
    source_check = _index(text, "$builtLabelTable['org.blessing.source.sha256'] -ne")
    head_recheck = _index(text, '$currentHeadSha -ne $launchHeadSha')
    tree_recheck = _index(text, '$currentDirtyEntries -or')
    refusal = text.index('is not bound to the current clean HEAD', label_check)
    image_id = _index(text, 'image inspect --format "{{.Id}}" $workerImageTag')
    worker_image_env = _index(text, '$env:LOCAL_WORKER_IMAGE_ID = ')
    supervisor_start = _index(text, '& $npm.Source run dev')

    # The HEAD, tree and label comparisons share one fail-closed if-condition, so they precede the refusal.
    assert build < label_read < tree_recheck < head_recheck < label_check < source_check < refusal
    assert refusal < image_id < worker_image_env < supervisor_start


def test_existing_build_fragment_used_by_the_typescript_launcher_test_is_unchanged():
    assert '--file Dockerfile.worker --tag $workerImageTag' in _build_line(_text())
