"""Repo-deterministic release-gate tier for Blessing AI.

Runs every offline CI check as a subprocess and emits a structured evidence
dict.  The dict is intentionally stable so a later combiner script can merge
it with results from the contract and soak tiers without breaking the schema.

Return shape (same keys, always present)::

    {
        "checks": [
            {
                "id":      str,          # machine-stable identifier
                "name":    str,          # human-readable label
                "status":  "PASS"|"FAIL",
                "message": str,          # brief description or tail of output
            },
            ...
        ],
        "overall_passed": bool,          # True iff every check is PASS
        "generated_at":   str,           # UTC ISO-8601 timestamp
    }

CLI usage::

    python -m apps.release_gate.repo_gate [--repo-root PATH] [--json]

Exit code is 0 when overall_passed is True, 1 otherwise.
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import pathlib
import subprocess
import sys
from typing import Any

# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

_TAIL_LINES = 40  # how many lines of stdout/stderr to keep in the message


def _tail(text: str, n: int = _TAIL_LINES) -> str:
    """Return the last *n* non-empty lines of *text*."""
    lines = [l for l in text.splitlines() if l.strip()]
    return "\n".join(lines[-n:]) if lines else ""


def _run(
    cmd: list[str],
    *,
    cwd: pathlib.Path,
    timeout: int,
    env: dict[str, str] | None = None,
) -> tuple[bool, str]:
    """Run *cmd* in *cwd* and return (passed, message).

    *message* is a brief tail of combined stdout+stderr on failure, or
    "OK" on success.
    """
    merged_env = {**os.environ, **(env or {})}
    try:
        result = subprocess.run(
            cmd,
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=timeout,
            env=merged_env,
        )
    except subprocess.TimeoutExpired:
        return False, f"Timed out after {timeout}s"
    except FileNotFoundError as exc:
        return False, f"Executable not found: {exc}"

    if result.returncode == 0:
        return True, "OK"

    combined = (result.stdout or "") + "\n" + (result.stderr or "")
    return False, _tail(combined) or f"exit code {result.returncode}"


def _check(
    check_id: str,
    name: str,
    cmd: list[str],
    *,
    cwd: pathlib.Path,
    timeout: int,
    env: dict[str, str] | None = None,
) -> dict[str, Any]:
    passed, msg = _run(cmd, cwd=cwd, timeout=timeout, env=env)
    return {
        "id": check_id,
        "name": name,
        "status": "PASS" if passed else "FAIL",
        "message": msg,
    }


# ---------------------------------------------------------------------------
# VITE_DATA_CONNECT_CUTOVER check (the one new gate not enforced elsewhere)
# ---------------------------------------------------------------------------

def _is_cutover_true(value: str | None) -> bool:
    """Mirror src/dataconnect/client.ts:16 evaluation: truthy iff == 'true'."""
    return str(value or "").lower() == "true"


def _check_vite_cutover(repo_root: pathlib.Path) -> dict[str, Any]:
    """Fail if VITE_DATA_CONNECT_CUTOVER resolves to 'true'.

    Checks (in order of precedence):
    1. Current process environment (highest priority – would affect a live run).
    2. The repo's .env.example default (documents the committed default).

    The check passes only when *both* sources agree the flag is not 'true'.
    """
    check_id = "vite_data_connect_cutover"
    name = "VITE_DATA_CONNECT_CUTOVER must be falsy"

    # 1. Process environment takes precedence (as Vite does at build time).
    env_value = os.environ.get("VITE_DATA_CONNECT_CUTOVER")
    if _is_cutover_true(env_value):
        return {
            "id": check_id,
            "name": name,
            "status": "FAIL",
            "message": (
                f"VITE_DATA_CONNECT_CUTOVER is set to {env_value!r} in the "
                "process environment. SQL Connect cutover must be disabled "
                "before a release gate can pass."
            ),
        }

    # 2. Check .env.example committed default.
    env_example = repo_root / ".env.example"
    if env_example.exists():
        for raw_line in env_example.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            if key.strip() == "VITE_DATA_CONNECT_CUTOVER":
                val = val.strip().strip('"').strip("'")
                if _is_cutover_true(val):
                    return {
                        "id": check_id,
                        "name": name,
                        "status": "FAIL",
                        "message": (
                            f".env.example sets VITE_DATA_CONNECT_CUTOVER={val!r}. "
                            "The committed default must remain falsy."
                        ),
                    }
                break

    return {
        "id": check_id,
        "name": name,
        "status": "PASS",
        "message": "OK",
    }


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def run_repo_gate(repo_root: pathlib.Path) -> dict[str, Any]:
    """Run every offline CI check and return a structured evidence dict.

    Parameters
    ----------
    repo_root:
        Absolute path to the repository root (the directory that contains
        ``package.json``, ``pyproject.toml``, ``.env.example``, etc.).

    Returns
    -------
    dict
        Structured evidence dict; see module docstring for the exact schema.
    """
    checks: list[dict[str, Any]] = []

    # ------------------------------------------------------------------
    # Node / TypeScript checks
    # ------------------------------------------------------------------
    checks.append(
        _check(
            "npm_ci",
            "npm ci (install dependencies)",
            ["npm", "ci"],
            cwd=repo_root,
            timeout=300,
        )
    )
    checks.append(
        _check(
            "npm_lint",
            "npm run lint (tsc --noEmit)",
            ["npm", "run", "lint"],
            cwd=repo_root,
            timeout=120,
        )
    )
    checks.append(
        _check(
            "npm_test",
            "npm run test (vitest run)",
            ["npm", "run", "test"],
            cwd=repo_root,
            timeout=600,
        )
    )
    checks.append(
        _check(
            "npm_build",
            "npm run build",
            ["npm", "run", "build"],
            cwd=repo_root,
            timeout=600,
        )
    )

    # ------------------------------------------------------------------
    # Python checks
    # ------------------------------------------------------------------
    python = sys.executable  # use the same interpreter that is running us

    checks.append(
        _check(
            "py_compile_trading_worker",
            "python -m py_compile apps/trading_worker/main.py",
            [python, "-m", "py_compile", "apps/trading_worker/main.py"],
            cwd=repo_root,
            timeout=30,
        )
    )
    checks.append(
        _check(
            "py_import_trading_worker",
            "python -c 'import apps.trading_worker.main'",
            [python, "-c", "import apps.trading_worker.main"],
            cwd=repo_root,
            timeout=60,
            env={"PYTHONPATH": str(repo_root)},
        )
    )
    checks.append(
        _check(
            "pytest_unit",
            "pytest tests/python/ -m 'not contract_readonly and not contract_mutating and not contract_soak'",
            [
                python,
                "-m",
                "pytest",
                "tests/python/",
                "-m",
                "not contract_readonly and not contract_mutating and not contract_soak",
                "-q",
            ],
            cwd=repo_root,
            timeout=600,
            env={"PYTHONPATH": str(repo_root)},
        )
    )

    # ------------------------------------------------------------------
    # Git hygiene
    # ------------------------------------------------------------------
    checks.append(
        _check(
            "git_diff_check",
            "git diff --check (whitespace errors)",
            ["git", "diff", "--check"],
            cwd=repo_root,
            timeout=30,
        )
    )

    # ------------------------------------------------------------------
    # New gate: VITE_DATA_CONNECT_CUTOVER must be falsy
    # ------------------------------------------------------------------
    checks.append(_check_vite_cutover(repo_root))

    overall_passed = all(c["status"] == "PASS" for c in checks)

    return {
        "checks": checks,
        "overall_passed": overall_passed,
        "generated_at": datetime.datetime.now(datetime.UTC).isoformat(),
    }


# ---------------------------------------------------------------------------
# CLI entrypoint (also invoked when run as a module: python -m apps.release_gate.repo_gate)
# ---------------------------------------------------------------------------

def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m apps.release_gate.repo_gate",
        description="Run offline repo-deterministic release-gate checks.",
    )
    parser.add_argument(
        "--repo-root",
        type=pathlib.Path,
        default=None,
        help=(
            "Path to the repository root. Defaults to the directory that "
            "contains this file's package (i.e. the repo root when installed "
            "with 'pip install -e .')."
        ),
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print the evidence dict as pretty-printed JSON (always done; flag kept for explicitness).",
    )
    return parser


def main(argv: list[str] | None = None) -> int:  # noqa: D401
    """CLI entrypoint; returns the process exit code."""
    args = _build_parser().parse_args(argv)

    if args.repo_root is None:
        # Default: two levels up from this file (apps/release_gate/repo_gate.py → repo root)
        args.repo_root = pathlib.Path(__file__).resolve().parent.parent.parent

    evidence = run_repo_gate(args.repo_root)
    print(json.dumps(evidence, indent=2))
    return 0 if evidence["overall_passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
