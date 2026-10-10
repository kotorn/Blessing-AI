"""Every first-party module the Worker image can import must be COPYed into Dockerfile.worker.

Static only (no docker, no network): parse Dockerfile.worker COPY lines, walk the AST of every
copied Python file (function-level imports included) and follow first-party imports transitively.
scripts/*.py files additionally insert their own directory into sys.path and import siblings by
bare name, so bare names that match scripts/<name>.py are followed too.
"""
import ast
from pathlib import Path
import shlex

ROOT = Path(__file__).resolve().parents[2]
DOCKERFILE = ROOT / 'Dockerfile.worker'
FIRST_PARTY_TOP_LEVEL = {p.name for p in ROOT.iterdir() if p.is_dir() and not p.name.startswith(('.', '__'))
                         and p.name not in {'node_modules', 'tests', 'artifacts', 'docs'}}


def copied_sources() -> set[str]:
    sources: set[str] = set()
    for raw in DOCKERFILE.read_text(encoding='utf-8').splitlines():
        if not raw.lstrip().upper().startswith('COPY '):
            continue
        tokens = shlex.split(raw, comments=True)
        if len(tokens) < 3 or any(t.startswith('--from=') for t in tokens):
            continue
        sources.update(tokens[1:-1])
    return sources


def copied_python_files() -> set[str]:
    files: set[str] = set()
    for source in copied_sources():
        path = ROOT / source
        if path.is_dir():
            files.update(p.relative_to(ROOT).as_posix() for p in path.rglob('*.py') if '__pycache__' not in p.parts)
        elif path.is_file() and path.suffix == '.py':
            files.add(source)
    return files


def module_file(dotted: str) -> str | None:
    parts = dotted.split('.')
    for end in range(len(parts), 0, -1):
        base = ROOT.joinpath(*parts[:end])
        if base.with_suffix('.py').is_file():
            return base.with_suffix('.py').relative_to(ROOT).as_posix()
        if (base / '__init__.py').is_file():
            return (base / '__init__.py').relative_to(ROOT).as_posix()
    return None


def imported_files(relative: str) -> set[str]:
    tree = ast.parse((ROOT / relative).read_text(encoding='utf-8'), filename=relative)
    in_scripts = relative.startswith('scripts/')
    found: set[str] = set()
    for node in ast.walk(tree):
        names: list[str] = []
        if isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            names = [node.module, *(f'{node.module}.{alias.name}' for alias in node.names)]
        for name in names:
            if name.split('.')[0] in FIRST_PARTY_TOP_LEVEL:
                target = module_file(name)
                if target:
                    found.add(target)
            elif in_scripts and (ROOT / 'scripts' / f"{name.split('.')[0]}.py").is_file():
                found.add(f"scripts/{name.split('.')[0]}.py")
    return found


def worker_import_closure() -> dict[str, str]:
    """Map every first-party file reachable from copied code to the file that imports it first."""
    copied = copied_python_files()
    reached: dict[str, str] = {}
    queue = sorted(copied)
    seen = set(queue)
    while queue:
        current = queue.pop()
        for target in imported_files(current):
            reached.setdefault(target, current)
            if target not in seen:
                seen.add(target)
                queue.append(target)
    return reached


def test_worker_image_copies_every_scripts_module_it_can_import():
    copied = copied_python_files()
    missing = {target: importer for target, importer in worker_import_closure().items()
               if target.startswith('scripts/') and target not in copied}
    assert missing == {}, f'Dockerfile.worker must COPY these scripts (imported by): {missing}'


def test_worker_image_copies_every_first_party_module_it_can_import():
    copied = copied_python_files()
    missing = {target: importer for target, importer in worker_import_closure().items() if target not in copied}
    assert missing == {}, f'Dockerfile.worker must COPY these modules (imported by): {missing}'


def test_track_c_gate_modules_are_reachable_and_copied():
    """Guards the walker itself: it must actually discover the Track C verifier chain."""
    reached = worker_import_closure()
    copied = copied_python_files()
    for name in ('scripts/verify_local_pilot_track_c.py', 'scripts/local_pilot_track_c.py',
                 'scripts/local_pilot_track_c_source.py', 'scripts/verify_local_pilot_ci_attestation.py',
                 'scripts/apply_local_postgres_migrations.py'):
        assert name in reached, name
        assert name in copied, name
