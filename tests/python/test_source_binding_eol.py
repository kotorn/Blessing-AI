"""Tests for source binding line ending enforcement."""
from __future__ import annotations

import json
from pathlib import Path
import subprocess
import pytest

from scripts.local_pilot_track_c_source import (
    DEPENDENCY_PATHS,
    SOURCE_PATHS,
    source_binding,
)


def _init_repo(root: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "config", "core.autocrlf", "false"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=root, check=True)


def _populate_minimum_bound_files(root: Path, eol: bytes = b"\n") -> None:
    for rel in SOURCE_PATHS:
        p = root / rel
        if p.suffix or rel == "Dockerfile.worker" or rel.startswith("config/"):
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(b"content" + eol)
        else:
            # directory
            p.mkdir(parents=True, exist_ok=True)
            (p / "sample.py").write_bytes(b"# python module" + eol)

    for rel in DEPENDENCY_PATHS:
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"{}" + eol if rel.endswith(".json") else b"content" + eol)

    mig = root / "infra" / "postgres" / "migrations" / "001.sql"
    mig.parent.mkdir(parents=True, exist_ok=True)
    mig.write_bytes(b"-- migration" + eol)

    # pilot policy
    policy = root / "config" / "risk" / "live_research_pilot.json"
    policy.parent.mkdir(parents=True, exist_ok=True)
    policy.write_bytes(b'{"version": 1}' + eol)


def test_source_binding_accepts_clean_lf_repository(tmp_path: Path):
    root = tmp_path / "repo"
    root.mkdir()
    _init_repo(root)
    _populate_minimum_bound_files(root, eol=b"\n")
    (root / ".gitattributes").write_bytes(b"* text=auto eol=lf\n")

    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    subprocess.run(["git", "commit", "-qm", "initial"], cwd=root, check=True)

    binding = source_binding(root)
    assert len(binding["gitSha"]) == 40
    assert len(binding["sourceSha256"]) == 64


def test_source_binding_rejects_crlf_in_bound_file(tmp_path: Path):
    root = tmp_path / "repo"
    root.mkdir()
    _init_repo(root)
    _populate_minimum_bound_files(root, eol=b"\n")
    (root / ".gitattributes").write_bytes(b"* text=auto eol=lf\n")

    # Introduce CRLF into one bound file
    (root / "server.ts").write_bytes(b"import express from 'express';\r\n")

    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    subprocess.run(["git", "commit", "-qm", "commit with crlf"], cwd=root, check=True)

    with pytest.raises(ValueError) as excinfo:
        source_binding(root)
    assert "TRACK_C_SOURCE_EOL_MISMATCH" in str(excinfo.value)
