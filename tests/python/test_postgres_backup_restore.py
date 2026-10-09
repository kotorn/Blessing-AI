"""Tests for safe break-glass Postgres backup and restore via Docker container."""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
from unittest.mock import MagicMock, call, patch
import pytest

from scripts.backup_local_postgres import (
    dump_local_postgres,
    restore_local_postgres,
    main,
)


def test_dump_local_postgres_success(tmp_path: Path):
    output_path = tmp_path / "backup.sql"

    def fake_run(cmd, *args, **kwargs):
        # Simulate docker cp creating the destination file
        if cmd[1] == "cp":
            output_path.write_text("-- fake pg_dump content", encoding="utf-8")
        return MagicMock(returncode=0)

    with patch("subprocess.run", side_effect=fake_run) as mock_run:
        result = dump_local_postgres(
            output_path=output_path,
            container="blessing-postgres-local",
            user="blessing_user",
            db="blessing_ai",
            tables=["mainnet_launch_sessions", "binance_algo_protections"],
        )

    assert result == output_path
    assert output_path.is_file()

    # Verify calls
    assert mock_run.call_count == 3
    calls = mock_run.call_args_list

    # Call 1: docker exec pg_dump ...
    pg_dump_cmd = calls[0][0][0]
    assert pg_dump_cmd[:4] == ["docker", "exec", "blessing-postgres-local", "pg_dump"]
    assert "-U" in pg_dump_cmd and pg_dump_cmd[pg_dump_cmd.index("-U") + 1] == "blessing_user"
    assert "-d" in pg_dump_cmd and pg_dump_cmd[pg_dump_cmd.index("-d") + 1] == "blessing_ai"
    assert "-t" in pg_dump_cmd and "mainnet_launch_sessions" in pg_dump_cmd
    assert "binance_algo_protections" in pg_dump_cmd
    assert "-f" in pg_dump_cmd
    container_tmp = pg_dump_cmd[pg_dump_cmd.index("-f") + 1]
    assert container_tmp.startswith("/tmp/")

    # Call 2: docker cp container:/tmp/... output_path
    cp_cmd = calls[1][0][0]
    assert cp_cmd == ["docker", "cp", f"blessing-postgres-local:{container_tmp}", str(output_path)]

    # Call 3: docker exec rm -f container:/tmp/...
    rm_cmd = calls[2][0][0]
    assert rm_cmd == ["docker", "exec", "blessing-postgres-local", "rm", "-f", container_tmp]


def test_dump_local_postgres_cleans_up_on_cp_error(tmp_path: Path):
    output_path = tmp_path / "backup.sql"

    def fake_run(cmd, *args, **kwargs):
        if cmd[1] == "cp":
            raise subprocess.CalledProcessError(1, cmd, stderr="cp failed")
        return MagicMock(returncode=0)

    with patch("subprocess.run", side_effect=fake_run) as mock_run:
        with pytest.raises(subprocess.CalledProcessError):
            dump_local_postgres(
                output_path=output_path,
                container="blessing-postgres-local",
                user="blessing_user",
                db="blessing_ai",
            )

    # Cleanup must still have occurred via finally block
    assert mock_run.call_count == 3
    rm_cmd = mock_run.call_args_list[2][0][0]
    assert rm_cmd[:4] == ["docker", "exec", "blessing-postgres-local", "rm"]
    assert "-f" in rm_cmd


def test_dump_local_postgres_missing_credentials_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("POSTGRES_USER", raising=False)
    monkeypatch.delenv("POSTGRES_DB", raising=False)
    output_path = tmp_path / "backup.sql"

    with pytest.raises(ValueError, match="POSTGRES_USER and POSTGRES_DB must be set"):
        dump_local_postgres(output_path=output_path, user=None, db=None)


def test_dump_local_postgres_env_credentials(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("POSTGRES_USER", "env_user")
    monkeypatch.setenv("POSTGRES_DB", "env_db")
    output_path = tmp_path / "backup.sql"

    def fake_run(cmd, *args, **kwargs):
        if cmd[1] == "cp":
            output_path.write_text("-- dummy", encoding="utf-8")
        return MagicMock(returncode=0)

    with patch("subprocess.run", side_effect=fake_run) as mock_run:
        dump_local_postgres(output_path=output_path)

    pg_dump_cmd = mock_run.call_args_list[0][0][0]
    assert pg_dump_cmd[pg_dump_cmd.index("-U") + 1] == "env_user"
    assert pg_dump_cmd[pg_dump_cmd.index("-d") + 1] == "env_db"


def test_restore_local_postgres_success(tmp_path: Path):
    backup_path = tmp_path / "backup.sql"
    backup_path.write_text("-- test restore", encoding="utf-8")

    with patch("subprocess.run", return_value=MagicMock(returncode=0)) as mock_run:
        restore_local_postgres(
            input_path=backup_path,
            container="blessing-postgres-local",
            user="blessing_user",
            db="blessing_ai",
        )

    assert mock_run.call_count == 3
    calls = mock_run.call_args_list

    # Call 1: docker cp input_path container:/tmp/...
    cp_cmd = calls[0][0][0]
    assert cp_cmd[:2] == ["docker", "cp"]
    assert cp_cmd[2] == str(backup_path)
    container_tmp = cp_cmd[3].split(":", 1)[1]
    assert container_tmp.startswith("/tmp/")

    # Call 2: docker exec psql -U user -d db -f /tmp/...
    psql_cmd = calls[1][0][0]
    assert psql_cmd[:4] == ["docker", "exec", "blessing-postgres-local", "psql"]
    assert "-U" in psql_cmd and psql_cmd[psql_cmd.index("-U") + 1] == "blessing_user"
    assert "-d" in psql_cmd and psql_cmd[psql_cmd.index("-d") + 1] == "blessing_ai"
    assert "-f" in psql_cmd and psql_cmd[psql_cmd.index("-f") + 1] == container_tmp

    # Call 3: docker exec rm -f /tmp/...
    rm_cmd = calls[2][0][0]
    assert rm_cmd == ["docker", "exec", "blessing-postgres-local", "rm", "-f", container_tmp]


def test_restore_local_postgres_cleans_up_on_psql_error(tmp_path: Path):
    backup_path = tmp_path / "backup.sql"
    backup_path.write_text("-- test restore", encoding="utf-8")

    def fake_run(cmd, *args, **kwargs):
        if "psql" in cmd:
            raise subprocess.CalledProcessError(1, cmd, stderr="psql syntax error")
        return MagicMock(returncode=0)

    with patch("subprocess.run", side_effect=fake_run) as mock_run:
        with pytest.raises(subprocess.CalledProcessError):
            restore_local_postgres(
                input_path=backup_path,
                container="blessing-postgres-local",
                user="blessing_user",
                db="blessing_ai",
            )

    assert mock_run.call_count == 3
    rm_cmd = mock_run.call_args_list[2][0][0]
    assert rm_cmd[:4] == ["docker", "exec", "blessing-postgres-local", "rm"]
    assert "-f" in rm_cmd


def test_restore_local_postgres_missing_file_raises(tmp_path: Path):
    backup_path = tmp_path / "nonexistent.sql"
    with pytest.raises(FileNotFoundError):
        restore_local_postgres(
            input_path=backup_path,
            user="blessing_user",
            db="blessing_ai",
        )


def test_backup_restore_cli_backup(tmp_path: Path):
    output_path = tmp_path / "cli_backup.sql"

    with patch("scripts.backup_local_postgres.dump_local_postgres") as mock_dump:
        mock_dump.return_value = output_path
        rc = main([
            "--action", "backup",
            "--file", str(output_path),
            "--user", "cli_user",
            "--db", "cli_db",
            "--container", "my-container",
            "--tables", "table1", "table2",
        ])

    assert rc == 0
    mock_dump.assert_called_once_with(
        output_path=output_path,
        container="my-container",
        user="cli_user",
        db="cli_db",
        tables=["table1", "table2"],
    )


def test_backup_restore_cli_restore(tmp_path: Path):
    input_path = tmp_path / "cli_backup.sql"
    input_path.write_text("-- test", encoding="utf-8")

    with patch("scripts.backup_local_postgres.restore_local_postgres") as mock_restore:
        rc = main([
            "--action", "restore",
            "--file", str(input_path),
            "--user", "cli_user",
            "--db", "cli_db",
            "--container", "my-container",
        ])

    assert rc == 0
    mock_restore.assert_called_once_with(
        input_path=input_path,
        container="my-container",
        user="cli_user",
        db="cli_db",
    )


def _docker_container_healthy(container_name: str) -> bool:
    try:
        res = subprocess.run(
            ["docker", "inspect", "--format", "{{.State.Health.Status}}", container_name],
            capture_output=True,
            text=True,
            check=True,
        )
        return res.stdout.strip() == "healthy"
    except Exception:
        return False


@pytest.mark.skipif(
    not os.environ.get('BLESSING_BACKUP_TEST_CONTAINER', '').startswith('blessing-pilot-acceptance-'),
    reason='Requires an explicitly named disposable backup fixture; never the trading container',
)
def test_live_docker_backup_roundtrip(tmp_path: Path):
    """Real pg_dump only against the explicitly opted-in disposable fixture."""
    container = os.environ['BLESSING_BACKUP_TEST_CONTAINER']
    assert _docker_container_healthy(container)
    output_file = tmp_path / "live_backup.sql"
    dump_local_postgres(
        output_path=output_file,
        container=container,
        user="pilot_ci",
        db=os.environ['BLESSING_BACKUP_TEST_DB'],
        tables=["mainnet_launch_sessions", "binance_algo_protections"],
    )

    assert output_file.is_file()
    assert output_file.stat().st_size > 0

    # Ensure no PowerShell UTF-16 LE BOM corruption (0xFF, 0xFE)
    raw_bytes = output_file.read_bytes()
    assert not raw_bytes.startswith(b"\xff\xfe"), "Corrupt UTF-16 LE BOM detected in dump!"
    assert not raw_bytes.startswith(b"\xfe\xff"), "Corrupt UTF-16 BE BOM detected in dump!"

    # Must be valid UTF-8 text
    text = output_file.read_text(encoding="utf-8")
    assert "PostgreSQL database dump" in text or "mainnet_launch_sessions" in text
