"""A fresh docker volume runs init_schema.sql, then start-local applies pending
migrations. Without ledger rows for the migrations init_schema already contains,
001-012 are re-applied and fail on existing objects (e.g. migration 010's trigger)."""

import re
from pathlib import Path

from scripts.apply_local_postgres_migrations import discover_migrations, migration_checksum

INIT = Path(__file__).resolve().parents[2] / "infra" / "postgres" / "init_schema.sql"


def test_init_schema_records_exactly_migrations_001_to_012_with_current_checksums():
    sql = INIT.read_text(encoding="utf-8")
    rows = dict(re.findall(r"\('(\d{3}_[a-z0-9_]+\.sql)', '([0-9a-f]{64})'\)", sql))
    expected = {m.name: migration_checksum(m) for m in discover_migrations()[:12]}
    assert rows == expected
