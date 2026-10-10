"""Safe break-glass backup and restore for local Postgres database.

Avoids PowerShell shell redirection pitfalls (UTF-16 LE BOM encoding corruption)
by executing pg_dump / psql directly inside the Docker container to a /tmp file,
copying via `docker cp`, and removing the container temp file.
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import subprocess
import sys
from typing import Sequence
import uuid

DEFAULT_CONTAINER = "blessing-postgres-local"
DEFAULT_TABLES = ("mainnet_launch_sessions", "binance_algo_protections")


def dump_local_postgres(
    output_path: Path | str,
    container: str = DEFAULT_CONTAINER,
    user: str | None = None,
    db: str | None = None,
    tables: Sequence[str] | None = DEFAULT_TABLES,
    docker_bin: str = "docker",
) -> Path:
    """Execute pg_dump inside container and copy to output_path cleanly."""
    target_path = Path(output_path).resolve()
    target_path.parent.mkdir(parents=True, exist_ok=True)

    resolved_user = user or os.getenv("POSTGRES_USER")
    resolved_db = db or os.getenv("POSTGRES_DB")
    if not resolved_user or not resolved_db:
        raise ValueError("POSTGRES_USER and POSTGRES_DB must be set or passed as arguments")

    container_tmp = f"/tmp/break-glass-backup-{os.getpid()}-{uuid.uuid4().hex[:8]}.sql"

    try:
        table_args: list[str] = []
        if tables:
            for table in tables:
                table_args.extend(["-t", table])

        dump_cmd = [
            docker_bin,
            "exec",
            container,
            "pg_dump",
            "-U",
            resolved_user,
            "-d",
            resolved_db,
            *table_args,
            "-f",
            container_tmp,
        ]
        subprocess.run(dump_cmd, check=True)

        cp_cmd = [
            docker_bin,
            "cp",
            f"{container}:{container_tmp}",
            str(target_path),
        ]
        subprocess.run(cp_cmd, check=True)

        return target_path
    finally:
        rm_cmd = [
            docker_bin,
            "exec",
            container,
            "rm",
            "-f",
            container_tmp,
        ]
        subprocess.run(rm_cmd, check=False)


def restore_local_postgres(
    input_path: Path | str,
    container: str = DEFAULT_CONTAINER,
    user: str | None = None,
    db: str | None = None,
    docker_bin: str = "docker",
) -> None:
    """Copy SQL file into container and restore using psql."""
    source_path = Path(input_path).resolve()
    if not source_path.is_file():
        raise FileNotFoundError(f"Backup file not found: {source_path}")

    resolved_user = user or os.getenv("POSTGRES_USER")
    resolved_db = db or os.getenv("POSTGRES_DB")
    if not resolved_user or not resolved_db:
        raise ValueError("POSTGRES_USER and POSTGRES_DB must be set or passed as arguments")

    container_tmp = f"/tmp/break-glass-restore-{os.getpid()}-{uuid.uuid4().hex[:8]}.sql"

    try:
        cp_cmd = [
            docker_bin,
            "cp",
            str(source_path),
            f"{container}:{container_tmp}",
        ]
        subprocess.run(cp_cmd, check=True)

        psql_cmd = [
            docker_bin,
            "exec",
            container,
            "psql",
            "-U",
            resolved_user,
            "-d",
            resolved_db,
            "-f",
            container_tmp,
        ]
        subprocess.run(psql_cmd, check=True)
    finally:
        rm_cmd = [
            docker_bin,
            "exec",
            container,
            "rm",
            "-f",
            container_tmp,
        ]
        subprocess.run(rm_cmd, check=False)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Safe break-glass backup and restore for local Postgres database."
    )
    parser.add_argument(
        "--action",
        choices=["backup", "restore"],
        default="backup",
        help="Action to perform (backup or restore)",
    )
    parser.add_argument(
        "--file",
        "-f",
        default="break-glass-backup.sql",
        help="Output file for backup, or input file for restore (default: break-glass-backup.sql)",
    )
    parser.add_argument(
        "--container",
        default=DEFAULT_CONTAINER,
        help=f"Target Docker container name (default: {DEFAULT_CONTAINER})",
    )
    parser.add_argument(
        "--user",
        default=None,
        help="Postgres username (defaults to POSTGRES_USER environment variable)",
    )
    parser.add_argument(
        "--db",
        default=None,
        help="Postgres database name (defaults to POSTGRES_DB environment variable)",
    )
    parser.add_argument(
        "--tables",
        nargs="*",
        default=list(DEFAULT_TABLES),
        help="Tables to dump (defaults to mainnet_launch_sessions binance_algo_protections)",
    )

    args = parser.parse_args(argv)

    if args.action == "backup":
        out = dump_local_postgres(
            output_path=Path(args.file),
            container=args.container,
            user=args.user,
            db=args.db,
            tables=args.tables,
        )
        print(f"Postgres backup written safely to {out}")
        return 0
    elif args.action == "restore":
        restore_local_postgres(
            input_path=Path(args.file),
            container=args.container,
            user=args.user,
            db=args.db,
        )
        print(f"Postgres backup {args.file} restored successfully into container {args.container}")
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
