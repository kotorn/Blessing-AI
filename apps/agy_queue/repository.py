"""Repository identity and local change evidence for queue jobs."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

from .security import redact_text


class RepositoryError(ValueError):
    """Raised when a job points at an unsafe or changed repository."""


@dataclass(frozen=True, slots=True)
class RepositorySnapshot:
    root: str
    branch: str
    sha: str


def _git(repo: Path, *args: str, timeout: int = 15) -> str:
    try:
        completed = subprocess.run(
            ["git", *args],
            cwd=repo,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RepositoryError(f"git command unavailable or timed out: {args[0]}") from exc
    if completed.returncode != 0:
        detail = (redact_text(completed.stderr or "") or "").strip()
        raise RepositoryError(f"git {args[0]} failed{': ' + detail if detail else ''}")
    return (completed.stdout or "").strip()


def resolve_repository(repo: str | Path) -> Path:
    path = Path(repo)
    if not path.is_absolute():
        raise RepositoryError("Repository path must be absolute")
    try:
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise RepositoryError("Repository path does not exist") from exc
    reported_root = Path(_git(resolved, "rev-parse", "--show-toplevel"))
    try:
        reported_root = reported_root.resolve(strict=True)
    except OSError as exc:
        raise RepositoryError("Git reported an invalid repository root") from exc
    if reported_root != resolved:
        raise RepositoryError(
            f"Repository path must be the git root: expected {reported_root}, got {resolved}"
        )
    return resolved


def snapshot_repository(repo: str | Path) -> RepositorySnapshot:
    root = resolve_repository(repo)
    return RepositorySnapshot(
        root=str(root),
        branch=_git(root, "rev-parse", "--abbrev-ref", "HEAD"),
        sha=_git(root, "rev-parse", "HEAD"),
    )


def verify_snapshot(
    repo: str | Path,
    *,
    expected_branch: str | None,
    expected_sha: str | None,
    require_writable_branch: bool = False,
) -> RepositorySnapshot:
    current = snapshot_repository(repo)
    if expected_branch and current.branch != expected_branch:
        raise RepositoryError(
            f"Repository branch changed: expected {expected_branch}, got {current.branch}"
        )
    if expected_sha and current.sha != expected_sha:
        raise RepositoryError(
            f"Repository base SHA changed: expected {expected_sha}, got {current.sha}"
        )
    if require_writable_branch and current.branch in {"main", "master"}:
        raise RepositoryError("Repository-changing AGY jobs cannot run on main/master")
    return current


def diff_evidence(repo: str | Path) -> dict[str, object]:
    root = resolve_repository(repo)
    try:
        diff_check = subprocess.run(
            ["git", "diff", "--check"],
            cwd=root,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RepositoryError("git diff --check was unavailable or timed out") from exc
    status = _git(root, "status", "--short")
    head_sha = _git(root, "rev-parse", "HEAD")
    diff_error = redact_text(diff_check.stderr or diff_check.stdout or "") or ""
    return {
        "diff_check_passed": diff_check.returncode == 0,
        "diff_check_error": diff_error.strip() or None,
        "status_porcelain": status,
        "head_sha": head_sha,
    }
