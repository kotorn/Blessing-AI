import asyncio
import hashlib
import json
import os
import re
import shutil
import tempfile
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from urllib.parse import unquote, urlsplit

import asyncpg
import pytest

from apps.trading_worker.execution_lease import LeaseLostError, PostgresExecutionLease
from apps.trading_worker.persistence.postgres.repositories import PersistenceRepository
from scripts import apply_local_postgres_migrations as migration_verifier
from scripts.apply_local_postgres_migrations import (
    MIGRATED_DATA_TABLES,
    REQUIRED_COLUMNS,
    REQUIRED_COLUMN_NULLABILITY,
    REQUIRED_CONSTRAINT_DEFINITION_SHA256,
    REQUIRED_CONSTRAINTS,
    REQUIRED_LOCAL_PILOT_CONSTRAINTS,
    REQUIRED_LOCAL_PILOT_MIGRATION,
    REQUIRED_FOREIGN_KEYS,
    REQUIRED_INDEXES,
    REQUIRED_UNIQUE_KEYS,
    REQUIRED_IMMUTABLE_TRIGGERS,
    IMMUTABLE_TRIGGER_FUNCTIONS,
    SAFE_POPULATED_UPGRADES,
    EMERGENCY_CLOSE_TRIGGER,
    apply_migrations,
    constraint_definition_is_safe,
    decode_postgres_internal_char,
    discover_migrations,
    expression_tokens,
    has_existing_migrated_data,
    immutable_history_triggers_are_safe,
    emergency_close_claim_trigger_is_safe,
    index_predicate_is_safe,
    migration_checksum,
    populated_data_upgrade_is_safe,
    run,
    validate_migration_ledger,
    verify_schema,
)


# pg_get_constraintdef/prosrc captured read-only from PostgreSQL 17.11,
# blessing_migration_test_acceptance_20261001_095955 (2026-10-01).
CLAIM_TABLE = "binance_emergency_close_claims"
CLAIM_SHAPE_NAME = "binance_emergency_close_claim_shape_check"
CLAIM_SHAPE = (
    "CHECK ((((venue)::text = 'binance_mainnet'::text) AND "
    "((symbol)::text ~ '^[A-Z0-9]{2,32}$'::text) AND "
    "((close_client_order_id)::text ~ '^[A-Za-z0-9_-]{1,64}$'::text) AND "
    "((claimant_id)::text ~ '^[A-Za-z0-9_.:-]{1,128}$'::text) AND "
    "(fencing_token > 0) AND ((((status)::text = 'RESERVED'::text) AND "
    "(attempted_at IS NULL)) OR (((status)::text = 'ATTEMPTED'::text) AND "
    "(attempted_at IS NOT NULL)))))"
)
CLAIM_FK = (
    "FOREIGN KEY (venue, symbol, entry_client_order_id) REFERENCES "
    "binance_algo_protections(venue, symbol, entry_client_order_id)"
)
CLAIM_TRIGGER_BODY = """
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'Emergency close claims cannot be deleted';
    END IF;
    IF OLD.status = 'ATTEMPTED'
       OR ROW(NEW.venue, NEW.symbol, NEW.entry_client_order_id, NEW.close_client_order_id, NEW.created_at)
          IS DISTINCT FROM ROW(OLD.venue, OLD.symbol, OLD.entry_client_order_id, OLD.close_client_order_id, OLD.created_at)
    THEN
        RAISE EXCEPTION 'Emergency close identity/attempt is immutable';
    END IF;
    IF NEW.status = 'RESERVED' THEN
        IF OLD.lease_until > clock_timestamp() OR NEW.fencing_token <> OLD.fencing_token + 1 THEN
            RAISE EXCEPTION 'Emergency close reservation transfer is not fenced';
        END IF;
    ELSIF NEW.status = 'ATTEMPTED' THEN
        IF OLD.lease_until <= clock_timestamp()
           OR NEW.fencing_token <> OLD.fencing_token OR NEW.claimant_id <> OLD.claimant_id
           OR NEW.lease_until <> OLD.lease_until THEN
            RAISE EXCEPTION 'Emergency close submission has a stale fence';
        END IF;
    END IF;
    RETURN NEW;
END;
"""


def claim_trigger_row():
    return {
        "trigger_name": EMERGENCY_CLOSE_TRIGGER, "table_name": CLAIM_TABLE,
        "enabled": b"O", "trigger_type": 27, "trigger_condition": None,
        "trigger_arguments": 0, "function_name": "fence_binance_emergency_close_claim",
        "function_schema": "public", "function_language": "plpgsql",
        "function_source": CLAIM_TRIGGER_BODY,
    }


def test_emergency_claim_contract_requires_every_column_and_does_not_approve_020():
    assert REQUIRED_COLUMNS[CLAIM_TABLE] == {
        "venue", "symbol", "entry_client_order_id", "close_client_order_id", "claimant_id",
        "fencing_token", "lease_until", "status", "attempted_at", "created_at", "updated_at",
    }
    assert REQUIRED_COLUMN_NULLABILITY[CLAIM_TABLE] == {
        column: "YES" if column == "attempted_at" else "NO"
        for column in REQUIRED_COLUMNS[CLAIM_TABLE]
    }
    assert CLAIM_TABLE in MIGRATED_DATA_TABLES
    assert (CLAIM_TABLE, ("venue", "symbol", "entry_client_order_id"), True) in REQUIRED_UNIQUE_KEYS
    assert (CLAIM_TABLE, ("venue", "close_client_order_id"), False) in REQUIRED_UNIQUE_KEYS
    assert "020_emergency_close_submission_claims.sql" not in SAFE_POPULATED_UPGRADES


@pytest.mark.parametrize("old,new", [
    ("fencing_token > 0", "fencing_token >= 0"),
    ("AND (fencing_token", "OR (fencing_token"),
    ("attempted_at IS NULL", "attempted_at IS NOT NULL"),
    ("'RESERVED'", "'ATTEMPTED'"),
    ("[A-Za-z0-9_.:-]", "[a-z0-9_.:-]"),
])
def test_emergency_claim_shape_hash_rejects_semantic_mutations(old, new):
    assert constraint_definition_is_safe(CLAIM_SHAPE_NAME, CLAIM_SHAPE)
    assert not constraint_definition_is_safe(CLAIM_SHAPE_NAME, CLAIM_SHAPE.replace(old, new))


@pytest.mark.parametrize("old,new", [
    ("TG_OP = 'DELETE'", "TG_OP = 'INSERT'"),
    ("OLD.status = 'ATTEMPTED'", "OLD.status = 'RESERVED'"),
    ("IS DISTINCT FROM", "IS NOT DISTINCT FROM"),
    ("OLD.lease_until > clock_timestamp()", "OLD.lease_until < clock_timestamp()"),
    ("OLD.fencing_token + 1", "OLD.fencing_token"),
    ("OLD.lease_until <= clock_timestamp()", "OLD.lease_until >= clock_timestamp()"),
    ("NEW.fencing_token <> OLD.fencing_token OR", "NEW.fencing_token = OLD.fencing_token OR"),
    ("NEW.claimant_id <> OLD.claimant_id", "FALSE"),
    ("NEW.lease_until <> OLD.lease_until", "FALSE"),
    ("RETURN NEW;", "RETURN OLD;"),
])
def test_emergency_claim_trigger_requires_exact_fencing_body(old, new):
    row = claim_trigger_row()
    assert emergency_close_claim_trigger_is_safe([row])
    assert not emergency_close_claim_trigger_is_safe([
        {**row, "function_source": CLAIM_TRIGGER_BODY.replace(old, new)}
    ])


@pytest.mark.parametrize("field,value", [
    ("enabled", "D"), ("enabled", "R"), ("trigger_type", 29),
    ("trigger_condition", "false"), ("trigger_arguments", 1),
    ("table_name", "orders"), ("function_name", "other"),
    ("function_schema", "other"), ("function_language", "sql"),
])
def test_emergency_claim_trigger_rejects_disabled_or_substituted_metadata(field, value):
    row = claim_trigger_row()
    assert not emergency_close_claim_trigger_is_safe([{**row, field: value}])
    assert not emergency_close_claim_trigger_is_safe([])
    assert not emergency_close_claim_trigger_is_safe([row, row])


class ClaimSchemaConnection:
    """Catalog double; only unrelated CHECK hashes are bypassed by the fixture."""
    def __init__(self):
        self.columns = [
            {"table_name": table, "column_name": column,
             "is_nullable": REQUIRED_COLUMN_NULLABILITY.get(table, {}).get(column, "NO")}
            for table, columns in REQUIRED_COLUMNS.items() for column in columns
        ]
        self.indexes = []
        for name, (table, columns, unique, policy) in REQUIRED_INDEXES.items():
            if policy is None:
                predicate = None
            elif policy == "NOT_NULL":
                predicate = "continuation_approval_id IS NOT NULL"
            elif isinstance(policy, str) and policy.startswith("NOT_NULL:"):
                predicate = policy.split(":")[1] + " IS NOT NULL"
            elif policy == "NOT_CLOSED":
                predicate = "state <> 'CLOSED'"
            else:
                predicate = "state = ANY (ARRAY[" + ",".join(f"'{state}'" for state in policy) + "])"
            self.indexes.append({
                "indexname": name, "table_name": table, "key_columns": columns,
                "is_unique": unique, "is_primary": False, "is_valid": True,
                "is_ready": True, "predicate": predicate,
            })
        for number, (table, columns, primary) in enumerate(REQUIRED_UNIQUE_KEYS):
            self.indexes.append({
                "indexname": f"key_{number}", "table_name": table, "key_columns": columns,
                "is_unique": True, "is_primary": primary, "is_valid": True,
                "is_ready": True, "predicate": None,
            })
        self.constraints = [
            {"conname": name, "table_name": table, "columns": columns,
             "contype": b"c", "convalidated": True,
             "definition": CLAIM_SHAPE if name == CLAIM_SHAPE_NAME else "unrelated CHECK"}
            for name, (table, columns, _) in REQUIRED_CONSTRAINTS.items()
        ]
        self.foreign_keys = [
            {"conname": name, "table_name": table, "columns": columns,
             "referenced_table": target, "referenced_columns": target_columns,
             "contype": b"f", "convalidated": True, "definition": CLAIM_FK}
            for name, (table, columns, target, target_columns) in REQUIRED_FOREIGN_KEYS.items()
        ]
        self.triggers = []
        for name, table in REQUIRED_IMMUTABLE_TRIGGERS.items():
            function, body = IMMUTABLE_TRIGGER_FUNCTIONS.get(name, (
                "reject_binance_history_immutable_mutation",
                "BEGIN RAISE EXCEPTION 'BINANCE HISTORY EVIDENCE IS IMMUTABLE'; END;",
            ))
            self.triggers.append({
                "trigger_name": name, "table_name": table, "enabled": "O", "trigger_type": 27,
                "function_name": function, "function_language": "plpgsql", "function_source": body,
            })
        self.triggers.append(claim_trigger_row())

    async def fetch(self, query, *args):
        if "information_schema.columns" in query:
            return self.columns
        if "FROM pg_index" in query:
            return self.indexes
        if "FROM pg_constraint" in query:
            return self.foreign_keys if "referenced_columns" in query else self.constraints
        if "FROM pg_trigger" in query:
            assert EMERGENCY_CLOSE_TRIGGER in args[0]
            return self.triggers
        raise AssertionError(f"Unexpected schema query: {query}")


@pytest.fixture
def claim_schema(monkeypatch):
    original = constraint_definition_is_safe
    monkeypatch.setattr(migration_verifier, "constraint_definition_is_safe", lambda name, definition:
                        original(name, definition) if name == CLAIM_SHAPE_NAME else True)
    return ClaimSchemaConnection()


def test_default_schema_verifier_accepts_canonical_emergency_claims(claim_schema):
    asyncio.run(verify_schema(claim_schema))


@pytest.mark.parametrize("column", sorted(REQUIRED_COLUMNS[CLAIM_TABLE]))
def test_default_schema_verifier_rejects_missing_claim_column(claim_schema, column):
    claim_schema.columns = [row for row in claim_schema.columns
                            if (row["table_name"], row["column_name"]) != (CLAIM_TABLE, column)]
    with pytest.raises(RuntimeError, match="columns are missing"):
        asyncio.run(verify_schema(claim_schema))


@pytest.mark.parametrize("column", sorted(REQUIRED_COLUMNS[CLAIM_TABLE]))
def test_default_schema_verifier_rejects_claim_nullability_drift(claim_schema, column):
    for row in claim_schema.columns:
        if row["table_name"] == CLAIM_TABLE and row["column_name"] == column:
            row["is_nullable"] = "NO" if row["is_nullable"] == "YES" else "YES"
    with pytest.raises(RuntimeError, match="nullability is unsafe"):
        asyncio.run(verify_schema(claim_schema))


@pytest.mark.parametrize("primary", [True, False])
@pytest.mark.parametrize("mutation", ["missing", "partial", "invalid", "not_unique", "wrong_columns"])
def test_default_schema_verifier_rejects_unsafe_claim_keys(claim_schema, primary, mutation):
    rows = [row for row in claim_schema.indexes
            if row["table_name"] == CLAIM_TABLE and row["is_primary"] is primary]
    assert len(rows) == 1
    row = rows[0]
    if mutation == "missing":
        claim_schema.indexes.remove(row)
    else:
        field, value = {"partial": ("predicate", "status = 'RESERVED'"),
                        "invalid": ("is_valid", False), "not_unique": ("is_unique", False),
                        "wrong_columns": ("key_columns", ("close_client_order_id",))}[mutation]
        row[field] = value
    with pytest.raises(RuntimeError, match="indexes/keys are missing or unsafe"):
        asyncio.run(verify_schema(claim_schema))


@pytest.mark.parametrize("safeguard", ["shape", "fk", "trigger"])
@pytest.mark.parametrize("mutation", ["missing", "substituted"])
def test_default_schema_verifier_rejects_missing_or_substituted_claim_safeguards(
    claim_schema, safeguard, mutation,
):
    rows, field, name = {
        "shape": (claim_schema.constraints, "conname", CLAIM_SHAPE_NAME),
        "fk": (claim_schema.foreign_keys, "conname", "binance_emergency_close_claim_owner_fk"),
        "trigger": (claim_schema.triggers, "trigger_name", EMERGENCY_CLOSE_TRIGGER),
    }[safeguard]
    row = next(row for row in rows if row[field] == name)
    if mutation == "missing":
        rows.remove(row)
    elif safeguard == "shape":
        row["definition"] = CLAIM_SHAPE.replace("fencing_token > 0", "fencing_token >= 0")
    elif safeguard == "fk":
        row["definition"] += " ON DELETE CASCADE"
    else:
        row["function_source"] = "BEGIN RETURN NEW; END;"
    with pytest.raises(RuntimeError, match="missing or unsafe"):
        asyncio.run(verify_schema(claim_schema))


def test_local_migrations_are_numbered_and_discovered_in_order():
    migrations = discover_migrations(
        Path(__file__).resolve().parents[2] / "infra" / "postgres" / "migrations"
    )
    assert [path.name for path in migrations] == [
        "001_persistence_outbox_and_hedge_identity.sql",
        "002_execution_leases.sql",
        "003_mainnet_launch_sessions.sql",
        "004_environment_scoped_fill_identity.sql",
        "005_mainnet_autonomous_continuation.sql",
        "006_backfill_environment_scoped_venue.sql",
        "007_mainnet_launch_runtime_identity.sql",
        "008_mainnet_launch_order_identity.sql",
        "009_binance_algo_protection_ownership.sql",
        "010_binance_history_checkpoints.sql",
        "011_local_mainnet_basket_identity.sql",
        "012_mainnet_basket_owner_link.sql",
        "013_binance_algo_history_observations.sql",
        "014_mainnet_closure_evidence.sql",
        "015_local_emergency_close_evidence.sql",
        "016_live_research_pilot_and_history_fencing.sql",
        "017_local_live_pilot_runtime.sql",
        "018_local_pilot_binding_not_null.sql",
        "019_local_pilot_event_ownership.sql",
        "020_emergency_close_submission_claims.sql",
    ]
    assert all(len(migration_checksum(path)) == 64 for path in migrations)
    pilot_migration = next(path for path in migrations if path.name == REQUIRED_LOCAL_PILOT_MIGRATION)
    pilot_sql = pilot_migration.read_text(encoding="utf-8")
    assert REQUIRED_LOCAL_PILOT_CONSTRAINTS <= set(REQUIRED_CONSTRAINTS)
    assert REQUIRED_LOCAL_PILOT_CONSTRAINTS <= set(REQUIRED_CONSTRAINT_DEFINITION_SHA256)
    assert all(name in pilot_sql for name in REQUIRED_LOCAL_PILOT_CONSTRAINTS)


def test_local_pilot_schema_contract_pins_binding_and_all_event_constraints():
    assert REQUIRED_LOCAL_PILOT_CONSTRAINTS == {
        "mainnet_launch_pilot_binding_check",
        "local_live_pilot_event_type_check",
        "local_live_pilot_event_source_check",
        "local_live_pilot_event_payload_check",
    }
    for name in REQUIRED_LOCAL_PILOT_CONSTRAINTS:
        table, columns, _tokens = REQUIRED_CONSTRAINTS[name]
        assert table == (
            "mainnet_launch_sessions"
            if name == "mainnet_launch_pilot_binding_check"
            else "local_live_pilot_events"
        )
        assert columns
        assert len(REQUIRED_CONSTRAINT_DEFINITION_SHA256[name]) == 64


def test_duplicate_local_migration_versions_fail_closed(tmp_path):
    (tmp_path / "001_first.sql").write_text("SELECT 1;", encoding="utf-8")
    (tmp_path / "001_second.sql").write_text("SELECT 2;", encoding="utf-8")
    with pytest.raises(RuntimeError, match="duplicated"):
        discover_migrations(tmp_path)


def test_migration_discovery_requires_binding_and_event_constraints_in_017(tmp_path):
    (tmp_path / REQUIRED_LOCAL_PILOT_MIGRATION).write_text(
        "-- mainnet_launch_pilot_binding_check\n"
        "-- local_live_pilot_event_type_check\n"
        "-- local_live_pilot_event_source_check\n",
        encoding="utf-8",
    )
    with pytest.raises(RuntimeError, match="missing a required binding or event constraint"):
        discover_migrations(tmp_path)


def test_migration_runner_refuses_non_loopback_target_before_connecting(monkeypatch):
    monkeypatch.setenv("POSTGRES_HOST", "192.168.1.100")
    with pytest.raises(RuntimeError, match="127.0.0.1"):
        asyncio.run(run())


def test_migration_runner_refuses_a_non_local_runtime_port(monkeypatch):
    monkeypatch.setenv("POSTGRES_HOST", "127.0.0.1")
    monkeypatch.setenv("POSTGRES_PORT", "5432")
    with pytest.raises(RuntimeError, match="port 5433"):
        asyncio.run(run())


def test_migration_ledger_refuses_duplicates_order_drift_and_checksum_changes():
    migrations = discover_migrations()
    first, second = migrations[:2]
    first_row = {"version": first.name, "checksum": migration_checksum(first)}
    second_row = {"version": second.name, "checksum": migration_checksum(second)}

    with pytest.raises(RuntimeError, match="duplicate"):
        validate_migration_ledger([first_row, first_row], migrations)
    with pytest.raises(RuntimeError, match="out of order"):
        validate_migration_ledger([second_row, first_row], migrations)
    with pytest.raises(RuntimeError, match="changed"):
        validate_migration_ledger([{"version": first.name, "checksum": "0" * 64}], migrations)
    assert validate_migration_ledger([first_row, second_row], migrations) == {
        first.name: migration_checksum(first),
        second.name: migration_checksum(second),
    }


def test_populated_database_allows_only_verified_additive_identity_migrations():
    migrations = discover_migrations()
    first_six = {migration.name: migration_checksum(migration) for migration in migrations[:6]}
    # Only checksum-pinned additive migrations may upgrade populated local data.
    recorded = dict(first_six)
    # The new submission-claim migration remains unapproved for populated
    # databases until real PostgreSQL acceptance and independent review.
    verified_migrations = migrations[:-1]
    for index in range(6, len(verified_migrations)):
        assert populated_data_upgrade_is_safe(
            recorded, verified_migrations[index:], verified_migrations, ledger_exists=True
        )
        recorded[migrations[index].name] = migration_checksum(migrations[index])
    assert not populated_data_upgrade_is_safe(
        recorded, migrations[-1:], migrations, ledger_exists=True
    )
    assert not populated_data_upgrade_is_safe(
        first_six, migrations[6:], migrations, ledger_exists=False
    )
    assert not populated_data_upgrade_is_safe(
        {migration.name: migration_checksum(migration) for migration in migrations[:5]},
        migrations[5:],
        migrations,
        ledger_exists=True,
    )


def test_populated_database_rejects_modified_sql_with_an_approved_migration_name(tmp_path):
    migrations = discover_migrations()
    first_six = {
        migration.name: migration_checksum(migration) for migration in migrations[:6]
    }
    approved = migrations[6]
    altered = tmp_path / approved.name
    altered.write_text(
        approved.read_text(encoding="utf-8") + "\n-- unreviewed mutation\n",
        encoding="utf-8",
    )
    candidate_migrations = [*migrations[:6], altered, *migrations[7:]]
    assert not populated_data_upgrade_is_safe(
        first_six,
        candidate_migrations[6:],
        candidate_migrations,
        ledger_exists=True,
    )


def test_schema_expression_checks_are_exact_for_partial_indexes_and_constraints():
    states = frozenset(
        {
            "ACTIVE",
            "PAUSED_NEW_RISK",
            "RECONCILIATION_REQUIRED",
            "AUTONOMOUS_ACTIVE",
            "REAUTH_REQUIRED",
        }
    )
    valid_active = "((state)::text = ANY ((ARRAY['ACTIVE'::character varying, 'PAUSED_NEW_RISK'::character varying, 'RECONCILIATION_REQUIRED'::character varying, 'AUTONOMOUS_ACTIVE'::character varying, 'REAUTH_REQUIRED'::character varying])::text[]))"
    assert index_predicate_is_safe(valid_active, states)
    assert not index_predicate_is_safe(valid_active.replace("'ACTIVE'", "'CLOSED'"), states)
    assert index_predicate_is_safe("(continuation_approval_id IS NOT NULL)", "NOT_NULL")
    assert index_predicate_is_safe("(client_id IS NOT NULL)", "NOT_NULL:client_id")
    assert index_predicate_is_safe("(basket_id IS NOT NULL)", "NOT_NULL:basket_id")
    assert not index_predicate_is_safe("(run_id IS NOT NULL)", "NOT_NULL:client_id")
    assert not index_predicate_is_safe("(client_id IS NOT NULL)", "NOT_NULL:basket_id")
    assert not index_predicate_is_safe(
        "(continuation_approval_id IS NOT NULL OR state = 'ACTIVE')", "NOT_NULL"
    )
    assert index_predicate_is_safe("(state <> 'CLOSED')", "NOT_CLOSED")
    assert not index_predicate_is_safe("(state <> 'CLOSED' OR state = 'ACTIVE')", "NOT_CLOSED")
    assert expression_tokens("CHECK ((reserved_orders >= 0))") == (
        "check",
        "reserved_orders",
        ">=",
        "0",
    )
    assert expression_tokens("CHECK (stop_price > 0 AND stop_price < entry_price)") == (
        "check", "stop_price", ">", "0", "and", "stop_price", "<", "entry_price",
    )
    assert expression_tokens("CHECK (stop_price < 0 AND stop_price > entry_price)") != (
        "check", "stop_price", ">", "0", "and", "stop_price", "<", "entry_price",
    )
    assert expression_tokens("CHECK (client_order_id ~ '^[A-Za-z0-9_-]{1,64}$')") != (
        "check", "client_order_id", "~", "'^[a-z0-9_-]{1,64}$'",
    )
    assert (
        expression_tokens(
            "CHECK ((runtime_target = 'LOCAL' AND image_digest IS NULL "
            "AND runtime_fingerprint IS NOT NULL AND runtime_fingerprint ~* "
            "'^[0-9a-f]{64}$') OR (runtime_target = 'CLOUD_RUN' AND "
            "image_digest IS NOT NULL AND image_digest ~* "
            "'^.+@sha256:[0-9a-f]{64}$'))"
        )
        == REQUIRED_CONSTRAINTS["mainnet_launch_runtime_identity_check"][2]
    )
    assert "idx_binance_algo_protections_nonterminal" in REQUIRED_INDEXES
    assert "idx_binance_history_items_client" in REQUIRED_INDEXES
    assert {
        "binance_history_anchors",
        "binance_history_checkpoints",
        "binance_history_items",
        "binance_preexisting_algo_baselines",
        "binance_algo_history_observations",
        "binance_history_item_observations",
    } <= set(REQUIRED_COLUMNS)
    assert "mainnet_launch_id" in REQUIRED_COLUMNS["binance_algo_protections"]
    assert "scan_id" in REQUIRED_COLUMNS["binance_history_checkpoints"]
    assert "management_mode" in REQUIRED_COLUMNS["binance_algo_protections"]
    assert "pilot_campaign_expires_at" in REQUIRED_COLUMNS["mainnet_launch_sessions"]
    algo_protection_checks = {
        "binance_algo_protection_environment_check",
        "binance_algo_protection_entry_identity_check",
        "binance_algo_protection_quantity_check",
        "binance_algo_protection_trigger_check",
        "binance_algo_protection_algo_identity_check",
        "binance_algo_protection_state_check",
    }
    launch_safety_checks = {
        "mainnet_launch_policy_check",
        "mainnet_launch_limit_check",
        "mainnet_launch_reserved_check",
        "mainnet_launch_submitted_check",
        "mainnet_launch_state_check",
        "mainnet_launch_runtime_identity_check",
        "mainnet_launch_order_identity_check",
        "mainnet_launch_pilot_policy_check",
        "binance_algo_protection_management_mode_check",
        "binance_history_checkpoint_scan_fence_check",
        "binance_history_item_observation_shape_check",
    }
    pinned_checks = algo_protection_checks | launch_safety_checks | {
        "binance_emergency_close_claim_shape_check",
        "persistence_outbox_status_check",
        "binance_algo_protection_launch_identity_check",
        "binance_algo_protection_closure_evidence_check",
        "binance_history_anchor_identity_check",
        "binance_history_checkpoint_shape_check",
        "binance_history_item_shape_check",
        "binance_preexisting_algo_proof_check",
        "mainnet_launch_pilot_binding_check",
        "local_live_pilot_event_type_check",
        "local_live_pilot_event_source_check",
        "local_live_pilot_event_payload_check",
    }
    assert set(REQUIRED_CONSTRAINTS) == pinned_checks
    assert set(REQUIRED_CONSTRAINT_DEFINITION_SHA256) == (
        pinned_checks
    )


def test_postgres_protection_constraint_verification_preserves_operator_and_grouping():
    actual = (
        "CHECK (((stop_trigger_price > (0)::numeric) AND "
        "(take_profit_trigger_price > (0)::numeric) AND "
        "(stop_trigger_price <> take_profit_trigger_price) AND "
        "((entry_average_price IS NULL) OR (((entry_side)::text = 'BUY'::text) AND "
        "(stop_trigger_price < entry_average_price) AND "
        "(entry_average_price < take_profit_trigger_price)) OR "
        "(((entry_side)::text = 'SELL'::text) AND "
        "(take_profit_trigger_price < entry_average_price) AND "
        "(entry_average_price < stop_trigger_price)))))"
    )
    assert constraint_definition_is_safe(
        "binance_algo_protection_trigger_check", actual
    )
    assert not constraint_definition_is_safe(
        "binance_algo_protection_trigger_check",
        actual.replace(
            "stop_trigger_price < entry_average_price",
            "stop_trigger_price > entry_average_price",
        ),
    )
    assert not constraint_definition_is_safe(
        "binance_algo_protection_trigger_check",
        actual.replace("((entry_average_price IS NULL) OR", "(entry_average_price IS NULL OR"),
    )


def test_launch_reservation_constraint_rejects_semantic_grouping_mutation():
    valid = (
        "CHECK (((reserved_orders >= 0) AND "
        "((max_risk_increasing_orders IS NULL) OR "
        "(reserved_orders <= max_risk_increasing_orders))))"
    )
    unsafe = (
        "CHECK (((reserved_orders >= 0) AND "
        "(max_risk_increasing_orders IS NULL)) OR "
        "(reserved_orders <= max_risk_increasing_orders))"
    )
    assert expression_tokens(valid) == expression_tokens(unsafe)
    assert constraint_definition_is_safe("mainnet_launch_reserved_check", valid)
    assert not constraint_definition_is_safe("mainnet_launch_reserved_check", unsafe)


def test_launch_schema_verification_covers_columns_used_by_continuation_queries():
    assert {
        "image_digest",
        "max_risk_increasing_orders",
        "first_order_verified_at",
        "autonomous_approved_at",
        "last_restart_at",
        "pending_order_client_order_id",
        "first_order_client_order_id",
        "runtime_target",
        "runtime_fingerprint",
    } <= REQUIRED_COLUMNS["mainnet_launch_sessions"]


def test_mainnet_algo_protection_requires_launch_basket_foreign_key():
    assert REQUIRED_FOREIGN_KEYS[
        "binance_algo_protection_launch_basket_fk"
    ] == (
        "binance_algo_protections",
        ("mainnet_launch_id", "basket_id"),
        "mainnet_launch_sessions",
        ("launch_id", "basket_id"),
    )


def test_history_observations_require_checkpoint_scope_foreign_keys():
    scope = ("runtime_target", "run_id", "symbol", "history_kind")
    for name, table in (
        ("binance_algo_history_observation_scope_fk", "binance_algo_history_observations"),
        ("binance_history_item_observation_scope_fk", "binance_history_item_observations"),
    ):
        assert REQUIRED_FOREIGN_KEYS[name] == (
            table, scope, "binance_history_checkpoints", scope,
        )


def test_asyncpg_internal_constraint_type_is_normalized_from_bytes():
    assert decode_postgres_internal_char(b"c") == "c"
    assert decode_postgres_internal_char("c") == "c"


def test_immutable_history_triggers_require_exact_tables_scope_and_body():
    rows = [
        {
            "trigger_name": name,
            "table_name": table,
            "enabled": "O",
            "trigger_type": 27,
            "function_name": "reject_binance_history_immutable_mutation",
            "function_language": "plpgsql",
            "function_source": "BEGIN\n RAISE EXCEPTION 'Binance history evidence is immutable';\nEND;",
        }
        for name, table in {
            "binance_history_anchor_immutable": "binance_history_anchors",
            "binance_history_item_immutable": "binance_history_items",
            "binance_preexisting_algo_baseline_immutable": "binance_preexisting_algo_baselines",
            "binance_algo_history_observation_immutable": "binance_algo_history_observations",
            "binance_history_item_observation_immutable": "binance_history_item_observations",
        }.items()
    ]
    rows.append({
        "trigger_name": "local_live_pilot_event_immutable",
        "table_name": "local_live_pilot_events",
        "enabled": "O",
        "trigger_type": 27,
        "function_name": "reject_local_live_pilot_event_mutation",
        "function_language": "plpgsql",
        "function_source": "BEGIN\n RAISE EXCEPTION 'Local live pilot events are append-only';\nEND;",
    })
    assert immutable_history_triggers_are_safe(rows)
    assert not immutable_history_triggers_are_safe(rows[:-1])
    assert not immutable_history_triggers_are_safe(
        [
            {**row, "trigger_type": 29} if row["trigger_name"] == rows[0]["trigger_name"] else row
            for row in rows
        ]
    )
    assert not immutable_history_triggers_are_safe(
        [
            {**row, "function_source": "BEGIN RETURN OLD; END;"}
            if row["trigger_name"] == rows[0]["trigger_name"] else row
            for row in rows
        ]
    )


class MigrationGuardConnection:
    def __init__(self, populated_table=None, search_path="public"):
        self.populated_table = populated_table
        self.search_path = search_path
        self.executed = []
        self.lock_released = False

    async def fetchval(self, query, *args):
        if "current_setting('search_path')" in query:
            return self.search_path
        if "pg_advisory_lock" in query:
            return None
        if "to_regclass" in query:
            return False
        if "SELECT EXISTS" in query:
            return self.populated_table is not None and f'"{self.populated_table}"' in query
        if "pg_advisory_unlock" in query:
            self.lock_released = True
            return True
        raise AssertionError(f"Unexpected fetchval query: {query}")

    async def fetch(self, query, *args):
        if "information_schema.tables" in query:
            return [{"table_name": table} for table in MIGRATED_DATA_TABLES]
        raise AssertionError(f"Unexpected fetch query: {query}")

    async def execute(self, query, *args):
        self.executed.append(query)


def test_pending_migrations_refuse_existing_trading_data_before_schema_changes():
    connection = MigrationGuardConnection(populated_table="orders")
    with pytest.raises(RuntimeError, match="Existing local trading data"):
        asyncio.run(apply_migrations(connection))

    assert connection.lock_released is True
    assert connection.executed == []


def test_migration_runner_refuses_non_public_search_path_before_locking():
    connection = MigrationGuardConnection(search_path="attacker_schema, public")
    with pytest.raises(RuntimeError, match="search_path"):
        asyncio.run(apply_migrations(connection))
    assert connection.lock_released is False
    assert connection.executed == []


def test_empty_local_schema_is_allowed_through_migration_data_guard():
    connection = MigrationGuardConnection()
    assert asyncio.run(has_existing_migrated_data(connection)) is False


@pytest.mark.parametrize(
    "dsn",
    [
        "postgres://test:test@127.0.0.1:5433/blessing_trading",
        "postgres://test:test@192.168.1.100:5433/blessing_migration_test_case",
        "postgres://test:test@127.0.0.1/blessing_migration_test_case",
        "postgres://test:test@127.0.0.1:5433/blessing_migration_test_case?host=elsewhere",
        "postgres://test:test@127.0.0.1:55436/blessing_migration_test_case",
    ],
)
def test_integration_migration_rejects_unsafe_dsn_before_connect(monkeypatch, dsn):
    monkeypatch.setenv("BLESSING_MIGRATION_TEST_DSN", dsn)
    with pytest.raises(pytest.fail.Exception, match="isolated database"):
        test_populated_migration_upgrade_through_020_survives_reconnect()


def test_populated_migration_upgrade_through_020_survives_reconnect():
    """Opt-in, one-shot integration test for a fresh dedicated local test database.

    Set BLESSING_MIGRATION_TEST_DSN to a postgres:// URL for an EMPTY database
    named blessing_migration_test_<unique suffix> on loopback. The port defaults
    to 55433 and may be set to 55434 or 55435 for a separate persistent fixture.
    This test deliberately leaves its fixture in that database for inspection.
    """
    dsn = os.environ.get("BLESSING_MIGRATION_TEST_DSN", "")
    if not dsn:
        pytest.skip(
            "Set BLESSING_MIGRATION_TEST_DSN for an isolated local PostgreSQL test database"
        )
    try:
        parsed = urlsplit(dsn)
        port = parsed.port
        expected_port = int(os.environ.get("BLESSING_MIGRATION_TEST_PORT", "55433"))
    except ValueError:
        pytest.fail("Malformed isolated PostgreSQL test DSN", pytrace=False)
    if expected_port not in {55433, 55434, 55435}:
        pytest.fail("Test port must be one of the isolated loopback ports", pytrace=False)
    database = unquote(parsed.path.removeprefix("/"))
    if (
        parsed.scheme not in {"postgres", "postgresql"}
        or parsed.hostname != "127.0.0.1"
        or port != expected_port
        or parsed.query
        or parsed.fragment
        or not re.fullmatch(r"blessing_migration_test_[a-z0-9_]+", database)
    ):
        pytest.fail(
            "Test DSN must target an explicitly named isolated database on 127.0.0.1", pytrace=False
        )
    migrations = discover_migrations()
    assert [migration.name[:3] for migration in migrations] == [
        "001",
        "002",
        "003",
        "004",
        "005",
        "006",
        "007",
        "008",
        "009",
        "010",
        "011",
        "012",
        "013",
        "014",
        "015",
        "016",
        "017",
        "018",
        "019",
        "020",
    ]

    async def scenario():
        async def connect():
            try:
                return await asyncpg.connect(
                    dsn=dsn,
                    timeout=5,
                    server_settings={
                        "search_path": "public",
                        "application_name": "blessing-migration-test",
                    },
                )
            except Exception as exc:  # noqa: BLE001 - sanitize driver errors that may contain the DSN.
                pytest.fail(
                    f"Isolated PostgreSQL connection failed: {type(exc).__name__}", pytrace=False
                )

        connection = await connect()
        try:
            identity = await connection.fetchrow(
                "SELECT current_database() AS name, current_setting('search_path') AS search_path"
            )
            if (
                identity["name"] != database
                or identity["search_path"] != "public"
            ):
                pytest.fail(
                    "Connected PostgreSQL identity is not the isolated test target", pytrace=False
                )
            server_version_num = int(await connection.fetchval("SHOW server_version_num"))
            if not 170000 <= server_version_num < 180000:
                pytest.fail("Isolated migration integration requires PostgreSQL 17", pytrace=False)
            existing = await connection.fetchval(
                "SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
                "WHERE n.nspname NOT IN ('pg_catalog', 'information_schema') "
                "AND n.nspname NOT LIKE 'pg_toast%' AND c.relkind IN ('r', 'p', 'v', 'm', 'S', 'f')"
            )
            if existing:
                pytest.fail("Isolated test database must have no user relations", pytrace=False)

            # Minimal pre-001 schema: 001-006 are applied from their real SQL files.
            await connection.execute(
                "CREATE TABLE orders (venue varchar(32) DEFAULT 'binance_global')"
            )
            await connection.execute(
                "CREATE TABLE fills (venue varchar(32) DEFAULT 'binance_global', "
                "symbol varchar(32), executed_at timestamptz, exchange_trade_id varchar(64))"
            )
            await connection.execute(
                "CREATE TABLE positions (venue varchar(32) DEFAULT 'binance_global', symbol varchar(32), "
                "position_side varchar(8), UNIQUE (venue, symbol))"
            )
            await connection.execute(
                "CREATE TABLE public.local_schema_migrations (version varchar(128) PRIMARY KEY, "
                "checksum char(64) NOT NULL, applied_at timestamptz DEFAULT CURRENT_TIMESTAMP)"
            )
            for migration in migrations[:6]:
                async with connection.transaction():
                    await connection.execute(migration.read_text(encoding="utf-8"))
                    await connection.execute(
                        "INSERT INTO public.local_schema_migrations (version, checksum) VALUES ($1, $2)",
                        migration.name,
                        migration_checksum(migration),
                    )

            digest = "worker@sha256:" + "a" * 64
            await connection.execute(
                "INSERT INTO mainnet_launch_sessions "
                "(launch_id, approval_id, image_digest, symbol, policy, state, "
                "reserved_orders, submitted_orders, first_order_verified_at, continuation_approval_id) "
                "VALUES ($1, $2, $3, 'ETHUSDC', 'STAGED_FIRST_ORDER', 'PAUSED_NEW_RISK', "
                "1, 1, CURRENT_TIMESTAMP, $4)",
                "migration-test-cloud",
                "migration-test-approval",
                digest,
                "migration-test-review",
            )
            before = await connection.fetchrow(
                "SELECT launch_id, approval_id, image_digest, state, reserved_orders, submitted_orders, "
                "first_order_verified_at, continuation_approval_id "
                "FROM mainnet_launch_sessions WHERE launch_id = 'migration-test-cloud'"
            )
            # Exercise the checksum-pinned additive migrations 007-019 against
            # populated state in this disposable PostgreSQL 17 database.
            with tempfile.TemporaryDirectory(prefix="blessing-migrations-007-019-") as temporary:
                legacy_directory = Path(temporary)
                for migration in migrations[:-1]:
                    shutil.copyfile(migration, legacy_directory / migration.name)
                # The current production verifier deliberately refuses an
                # incomplete legacy schema. Additive upgrades are recorded,
                # but it must not call a pre-020 database ready.
                with pytest.raises(RuntimeError, match="binance_emergency_close_claims"):
                    await apply_migrations(connection, legacy_directory)
                recorded_versions = await connection.fetch(
                    "SELECT version FROM local_schema_migrations ORDER BY version"
                )
                assert [row["version"] for row in recorded_versions] == [
                    migration.name for migration in migrations[:-1]
                ]
                with pytest.raises(RuntimeError, match="binance_emergency_close_claims"):
                    await apply_migrations(connection, legacy_directory)
            # Exercise 020 only inside this explicitly isolated acceptance
            # database. This does not whitelist upgrades of operational data.
            async with connection.transaction():
                await connection.execute(migrations[-1].read_text(encoding="utf-8"))
                await connection.execute(
                    "INSERT INTO public.local_schema_migrations (version, checksum) VALUES ($1, $2)",
                    migrations[-1].name, migration_checksum(migrations[-1]),
                )
            await verify_schema(connection)
            assert await connection.fetchval(
                "SELECT to_regclass('public.binance_emergency_close_claims') IS NOT NULL"
            ) is True
            claim_keys = await connection.fetch(
                "SELECT conname, contype, convalidated FROM pg_constraint "
                "WHERE conrelid = 'public.binance_emergency_close_claims'::regclass"
            )
            assert {row["conname"] for row in claim_keys if row["convalidated"]} >= {
                "binance_emergency_close_claims_pkey",
                "binance_emergency_close_claim_identity_unique",
                "binance_emergency_close_claim_owner_fk",
                "binance_emergency_close_claim_shape_check",
            }
            assert await connection.fetchval(
                "SELECT count(*) FROM pg_trigger WHERE "
                "tgrelid = 'public.binance_emergency_close_claims'::regclass "
                "AND tgname = 'binance_emergency_close_claim_fence' "
                "AND tgenabled = 'O' AND NOT tgisinternal"
            ) == 1
            assert await connection.fetchval(
                "SELECT to_regclass('public.binance_algo_protections') IS NOT NULL"
            ) is True
            assert await connection.fetchval(
                "SELECT to_regclass('public.binance_history_anchors') IS NOT NULL"
            ) is True
            # Two independent PostgreSQL connections race to claim one close;
            # only one may pass PROTECTED -> CLOSE_PENDING, and the immediate
            # pre-submit marker must also be consumable exactly once.
            protections = PersistenceRepository(connection).algo_protections
            close_owner = {
                "venue": "binance_testnet", "environment": "TESTNET", "symbol": "ETHUSDC",
                "entry_client_order_id": "close-cas-entry", "entry_side": "BUY",
                "position_side": "BOTH", "requested_quantity": "0.1",
                "filled_quantity": "0.1", "entry_average_price": "2000",
                "stop_trigger_price": "1900", "take_profit_trigger_price": "2200",
                "stop_algo_id": "31001", "take_profit_algo_id": "31002",
                "stop_client_algo_id": "close-cas-stop", "take_profit_client_algo_id": "close-cas-target",
                "state": "PROTECTED",
            }
            await protections.upsert_protection(close_owner)
            concurrent_db = await connect()
            try:
                concurrent_protections = PersistenceRepository(concurrent_db).algo_protections
                reason = "protected_ethusdc_testnet_trial_close:close-cas-id:CLAIMED"
                claim_args = {
                    "venue": "binance_testnet", "symbol": "ETHUSDC",
                    "entry_client_order_id": "close-cas-entry", "entry_side": "BUY",
                    "position_side": "BOTH", "filled_quantity": Decimal("0.1"),
                    "stop_algo_id": "31001", "take_profit_algo_id": "31002",
                    "state_reason": reason,
                }
                competing_claims = await asyncio.gather(
                    protections.claim_testnet_protection_close(**claim_args),
                    concurrent_protections.claim_testnet_protection_close(**claim_args),
                )
                assert sum(item is not None for item in competing_claims) == 1
                marker_args = {
                    "venue": "binance_testnet", "symbol": "ETHUSDC",
                    "entry_client_order_id": "close-cas-entry", "claimed_reason": reason,
                    "submitting_reason": "protected_ethusdc_testnet_trial_close:close-cas-id:SUBMITTING",
                }
                competing_markers = await asyncio.gather(
                    protections.mark_testnet_protection_close_submitting(**marker_args),
                    concurrent_protections.mark_testnet_protection_close_submitting(**marker_args),
                )
                assert sum(item is not None for item in competing_markers) == 1
                await protections.set_protection_state(
                    "binance_testnet", "ETHUSDC", "close-cas-entry", "CLOSED",
                    reason="integration_close_claim_verified",
                )
            finally:
                await concurrent_db.close()
            history_run_id = "migration-test-readonly-history"
            history_now = datetime.now(UTC)
            history_anchor_at = history_now - timedelta(seconds=2)
            history_repository = PersistenceRepository(connection).binance_history
            anchor = await history_repository.register_testnet_anchor(
                run_id=history_run_id,
                symbol="ETHUSDC",
                anchor_at=history_anchor_at,
            )
            assert anchor["anchor_source"] == "TESTNET_READONLY_START"
            checkpoint = await history_repository.begin_testnet_scan(
                run_id=history_run_id,
                symbol="ETHUSDC",
                history_kind="ALL_ALGO_ORDERS",
                retention_seconds=3600,
                now=history_now,
            )
            with pytest.raises(RuntimeError, match="owned by another active scanner"):
                await history_repository.begin_testnet_scan(
                    run_id=history_run_id,
                    symbol="ETHUSDC",
                    history_kind="ALL_ALGO_ORDERS",
                    retention_seconds=3600,
                    now=history_now + timedelta(milliseconds=1),
                )
            history_item = {
                "item_id": 12345,
                "client_id": "read-only-baseline-check",
                "event_at": history_now,
                "payload": {
                    "algoId": 12345,
                    "clientAlgoId": "read-only-baseline-check",
                    "symbol": "ETHUSDC",
                    "algoStatus": "NEW",
                    "createTime": int(history_now.timestamp() * 1000),
                },
            }
            history_item["payload_sha256"] = hashlib.sha256(
                json.dumps(
                    history_item["payload"], sort_keys=True, separators=(",", ":")
                ).encode()
            ).hexdigest()
            cursor = await history_repository.persist_history_page(
                checkpoint=checkpoint,
                expected_cursor_id=0,
                items=[history_item],
                observed_at=history_now,
            )
            assert cursor == 12345
            changed_payload = {
                **history_item["payload"],
                "algoStatus": "TRIGGERED",
                "actualOrderId": 900,
            }
            changed_item = {
                **history_item,
                "payload": changed_payload,
                "payload_sha256": hashlib.sha256(
                    json.dumps(changed_payload, sort_keys=True, separators=(",", ":")).encode()
                ).hexdigest(),
            }
            assert await history_repository.persist_history_page(
                checkpoint=checkpoint,
                expected_cursor_id=cursor,
                items=[changed_item],
                observed_at=history_now + timedelta(milliseconds=1),
            ) == cursor
            await history_repository.complete_history_scan(checkpoint)
            latest_history = await history_repository.list_history_items(checkpoint)
            assert len(latest_history) == 1
            assert latest_history[0]["payload"]["algoStatus"] == "TRIGGERED"
            for history_kind, item_id, identity_key, first_payload, final_payload in (
                (
                    "ALL_ORDERS",
                    801,
                    "orderId",
                    {"orderId": 801, "clientOrderId": "entry-801", "status": "NEW", "executedQty": "0"},
                    {"orderId": 801, "clientOrderId": "entry-801", "status": "PARTIALLY_FILLED", "executedQty": "0.01"},
                ),
                (
                    "USER_TRADES",
                    802,
                    "id",
                    {"id": 802, "orderId": 801, "qty": "0.01", "commission": "0.001"},
                    {"id": 802, "orderId": 801, "qty": "0.01", "commission": "0.002"},
                ),
            ):
                lifecycle_checkpoint = await history_repository.begin_testnet_scan(
                    run_id=history_run_id,
                    symbol="ETHUSDC",
                    history_kind=history_kind,
                    retention_seconds=3600,
                    now=history_now + timedelta(milliseconds=2),
                )
                payload_versions = (
                    (first_payload, final_payload)
                    if history_kind == "ALL_ORDERS"
                    else (first_payload,)
                )
                for offset, payload in enumerate(payload_versions, start=3):
                    payload_hash = hashlib.sha256(
                        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
                    ).hexdigest()
                    item = {
                        "item_id": item_id,
                        "client_id": payload.get("clientOrderId", f"trade-{item_id}"),
                        "event_at": history_now + timedelta(milliseconds=1),
                        "payload": payload,
                        "payload_sha256": payload_hash,
                    }
                    assert await history_repository.persist_history_page(
                        checkpoint=lifecycle_checkpoint,
                        expected_cursor_id=0 if offset == 3 else item_id,
                        items=[item],
                        observed_at=history_now + timedelta(milliseconds=offset),
                    ) == item_id
                await history_repository.complete_history_scan(lifecycle_checkpoint)
                lifecycle_rows = await history_repository.list_history_items(lifecycle_checkpoint)
                assert len(lifecycle_rows) == 1
                assert lifecycle_rows[0]["item_id"] == item_id
                assert lifecycle_rows[0]["payload"][identity_key] == item_id
                assert lifecycle_rows[0]["payload"] == (
                    final_payload if history_kind == "ALL_ORDERS" else first_payload
                )
                observation_count = await connection.fetchval(
                    "SELECT count(*) FROM binance_history_item_observations "
                    "WHERE runtime_target = 'TESTNET' AND run_id = $1 AND symbol = 'ETHUSDC' "
                    "AND history_kind = $2 AND item_id = $3",
                    history_run_id,
                    history_kind,
                    item_id,
                )
                assert observation_count == (2 if history_kind == "ALL_ORDERS" else 1)
            after = await connection.fetchrow(
                "SELECT launch_id, approval_id, image_digest, state, reserved_orders, submitted_orders, "
                "first_order_verified_at, continuation_approval_id, runtime_target, runtime_fingerprint, "
                "pending_order_client_order_id, first_order_client_order_id "
                "FROM mainnet_launch_sessions WHERE launch_id = 'migration-test-cloud'"
            )
            assert dict(after) == {
                **dict(before),
                "runtime_target": "CLOUD_RUN",
                "runtime_fingerprint": None,
                "pending_order_client_order_id": None,
                "first_order_client_order_id": None,
            }
            fingerprint = "b" * 64
            await connection.execute(
                "INSERT INTO mainnet_launch_sessions "
                "(launch_id, approval_id, image_digest, symbol, policy, state, runtime_target, "
                "runtime_fingerprint, pending_order_client_order_id, first_order_client_order_id) "
                "VALUES ('migration-test-local', 'migration-test-local-approval', NULL, 'BTCUSDC', "
                "'STAGED_FIRST_ORDER', 'CLOSED', 'LOCAL', $1, 'pending_1', 'first_1')",
                fingerprint,
            )
            for column, bad_value in (
                ("runtime_fingerprint", "invalid"),
                ("pending_order_client_order_id", "bad space"),
                ("first_order_client_order_id", "bad space"),
            ):
                with pytest.raises(asyncpg.CheckViolationError):
                    async with connection.transaction():
                        await connection.execute(
                            f"UPDATE mainnet_launch_sessions SET {column} = $1 "
                            "WHERE launch_id = 'migration-test-local'",
                            bad_value,
                        )

            protection_repository = PersistenceRepository(connection).algo_protections
            protection_record = {
                "venue": "binance_testnet",
                "environment": "TESTNET",
                "symbol": "ETHUSDC",
                "entry_client_order_id": "restart-entry-1",
                "entry_side": "BUY",
                "position_side": "BOTH",
                "requested_quantity": "0.5000000000",
                "filled_quantity": "0",
                "entry_average_price": None,
                "stop_trigger_price": "1900",
                "take_profit_trigger_price": "2200",
                "stop_client_algo_id": "restart-stop-1",
                "take_profit_client_algo_id": "restart-target-1",
                "state": "PENDING",
            }
            created_protection = await protection_repository.upsert_protection(protection_record)
            assert created_protection["state"] == "PENDING"
            await protection_repository.upsert_protection(
                {
                    "venue": "binance_testnet",
                    "environment": "TESTNET",
                    "symbol": "ETHUSDC",
                    "entry_client_order_id": "restart-entry-1",
                    "filled_quantity": "0.2500000000",
                    "entry_average_price": "2000",
                    "stop_algo_id": "71001",
                    "take_profit_algo_id": "71002",
                }
            )
            assert await protection_repository.set_protection_state(
                "binance_testnet", "ETHUSDC", "restart-entry-1", "PROTECTED"
            ) is not None
            await connection.execute(
                "UPDATE mainnet_launch_sessions SET basket_id = 'migration-test-basket' "
                "WHERE launch_id = 'migration-test-cloud'"
            )
            await protection_repository.upsert_protection(
                {
                    **protection_record,
                    "venue": "binance_mainnet",
                    "environment": "MAINNET",
                    "basket_id": "migration-test-basket",
                    "mainnet_launch_id": "migration-test-cloud",
                    "management_mode": "QUICK",
                }
            )
            await protection_repository.upsert_protection(
                {
                    "venue": "binance_mainnet",
                    "environment": "MAINNET",
                    "symbol": "ETHUSDC",
                    "entry_client_order_id": "restart-entry-1",
                    "basket_id": "migration-test-basket",
                    "mainnet_launch_id": "migration-test-cloud",
                    "management_mode": "QUICK",
                    "filled_quantity": "0.2500000000",
                    "entry_average_price": "2000",
                    "stop_algo_id": "71003",
                    "take_profit_algo_id": "71004",
                    "protection_verified_at": datetime.now(UTC),
                }
            )
            assert await protection_repository.set_protection_state(
                "binance_mainnet", "ETHUSDC", "restart-entry-1", "PROTECTED"
            ) is not None
            assert await protection_repository.set_protection_state(
                "binance_mainnet", "ETHUSDC", "restart-entry-1", "CLOSE_PENDING"
            ) is not None
            closed_mainnet = await protection_repository.close_mainnet_protection_with_proof(
                "ETHUSDC",
                "restart-entry-1",
                {
                    "algo_id": "71003",
                    "order_id": "72003",
                    "client_order_id": "restart-close-order",
                    "order_status": "FILLED",
                    "executed_quantity": "0.25",
                    "trade_quantity": "0.25",
                    "position_quantity": "0",
                    "open_child_order_ids": [],
                    "open_owner_algo_ids": [],
                    "verified_at": datetime.now(UTC),
                },
            )
            assert closed_mainnet is not None
            assert closed_mainnet["state"] == "CLOSED"

            emergency_record = {
                **protection_record,
                "venue": "binance_mainnet",
                "environment": "MAINNET",
                "entry_client_order_id": "restart-emergency-entry",
                "basket_id": "migration-test-basket",
                "mainnet_launch_id": "migration-test-cloud",
                "management_mode": "QUICK",
                "stop_client_algo_id": "restart-emergency-stop",
                "take_profit_client_algo_id": "restart-emergency-target",
            }
            await protection_repository.upsert_protection(emergency_record)
            await protection_repository.upsert_protection(
                {
                    **emergency_record,
                    "filled_quantity": "0.2500000000",
                    "entry_average_price": "2000",
                    "stop_algo_id": "71005",
                    "take_profit_algo_id": "71006",
                }
            )
            assert await protection_repository.set_protection_state(
                "binance_mainnet", "ETHUSDC", "restart-emergency-entry", "PROTECTED"
            ) is not None
            assert await protection_repository.set_protection_state(
                "binance_mainnet",
                "ETHUSDC",
                "restart-emergency-entry",
                "CLOSE_PENDING",
                reason=(
                    "local_close_client_order_id=restart-emergency-close;"
                    "close_submission=ATTEMPTED"
                ),
            ) is not None
            emergency_closed = await protection_repository.close_mainnet_protection_with_proof(
                "ETHUSDC",
                "restart-emergency-entry",
                {
                    "algo_id": "LOCAL_EMERGENCY_CLOSE",
                    "order_id": "72004",
                    "client_order_id": "restart-emergency-close",
                    "order_status": "FILLED",
                    "executed_quantity": "0.25",
                    "trade_quantity": "0.25",
                    "position_quantity": "0",
                    "open_child_order_ids": [],
                    "open_owner_algo_ids": [],
                    "verified_at": datetime.now(UTC),
                },
            )
            assert emergency_closed is not None
            assert emergency_closed["state"] == "CLOSED"
            assert emergency_closed["closure_evidence"]["kind"] == (
                "LOCAL_EMERGENCY_CLOSE_VERIFIED"
            )
            unfilled_record = {
                **emergency_record,
                "entry_client_order_id": "restart-zero-fill-entry",
                "stop_client_algo_id": "restart-zero-fill-stop",
                "take_profit_client_algo_id": "restart-zero-fill-target",
            }
            await protection_repository.upsert_protection(unfilled_record)
            zero_fill_closed = await protection_repository.close_mainnet_unfilled_protection_with_proof(
                "ETHUSDC",
                "restart-zero-fill-entry",
                {
                    "order_id": "72005",
                    "client_order_id": "restart-zero-fill-entry",
                    "order_status": "CANCELED",
                    "executed_quantity": "0",
                    "original_quantity": "0.5000000000",
                    "symbol": "ETHUSDC",
                    "entry_side": "BUY",
                    "position_side": "BOTH",
                    "verified_at": datetime.now(UTC),
                },
            )
            assert zero_fill_closed is not None
            assert zero_fill_closed["state"] == "CLOSED"
            assert zero_fill_closed["closure_evidence"]["kind"] == "UNFILLED_ENTRY_TERMINAL"
            assert len(zero_fill_closed["closure_evidence"]["proof_sha256"]) == 64

            # Exercise the pilot ledger against real PostgreSQL 17 and verify
            # restart fencing, event idempotency, MARK replacement, and the
            # exact 5 USDC campaign drawdown boundary.
            await connection.execute(
                "UPDATE mainnet_launch_sessions SET state = 'CLOSED' "
                "WHERE launch_id = 'migration-test-cloud'"
            )
            staged_repository = PersistenceRepository(connection)
            staged = await staged_repository.create_mainnet_launch_session(
                launch_id="migration-test-staged-repository",
                approval_id="migration-test-staged-repository-approval",
                image_digest=digest,
            )
            assert staged["policy"] == "STAGED_FIRST_ORDER"
            assert await connection.fetchval(
                "SELECT pilot_max_position_notional_usdc IS NULL "
                "FROM mainnet_launch_sessions WHERE launch_id = $1",
                "migration-test-staged-repository",
            ) is True
            await connection.execute(
                "UPDATE mainnet_launch_sessions SET state = 'CLOSED' WHERE launch_id = $1",
                "migration-test-staged-repository",
            )
            pilot_id = "migration-test-pilot"
            campaign_id = "pilot-migration0001"
            hash_value = "c" * 64
            await connection.execute(
                """INSERT INTO mainnet_launch_sessions
                   (launch_id, approval_id, symbol, policy, state, runtime_target,
                    runtime_fingerprint, max_risk_increasing_orders, pilot_campaign_id,
                    pilot_git_sha, pilot_source_hash, pilot_dependency_hash,
                    pilot_migration_hash, pilot_strategy_hash, pilot_secret_project_id,
                    pilot_api_key_version, pilot_api_secret_version, pilot_management_mode,
                    pilot_campaign_expires_at, pilot_risk_policy_hash,
                    pilot_max_position_notional_usdc, pilot_per_position_risk_usdc,
                    pilot_max_drawdown_usdc, pilot_quick_target_net_usdc,
                    pilot_quick_max_hold_seconds, pilot_max_leverage, pilot_status)
                   VALUES ($1, 'migration-test-pilot-approval', 'ETHUSDC',
                    'LIVE_RESEARCH_PILOT', 'ACTIVE', 'LOCAL', $2, NULL, $3,
                    $4, $5, $5, $5, $5, 'test-project', '1', '2', 'QUICK',
                    CURRENT_TIMESTAMP + INTERVAL '7 days', $5, 50, 2, 5, 0.25, 86400, 10, 'ACTIVE')""",
                pilot_id, "d" * 64, campaign_id, "e" * 40, hash_value,
            )
            for column, bad_value in (
                ("pilot_git_sha", None),
                ("pilot_git_sha", "a" * 41),
                ("pilot_source_hash", None),
                ("pilot_dependency_hash", None),
                ("pilot_migration_hash", None),
                ("pilot_strategy_hash", None),
                ("pilot_risk_policy_hash", None),
                ("pilot_secret_project_id", " "),
                ("pilot_api_key_version", None),
                ("pilot_api_secret_version", None),
                ("pilot_campaign_expires_at", None),
                ("pilot_status", None),
                ("pilot_max_position_notional_usdc", None),
                ("pilot_per_position_risk_usdc", None),
                ("pilot_max_drawdown_usdc", None),
                ("pilot_max_leverage", None),
            ):
                with pytest.raises(asyncpg.CheckViolationError):
                    async with connection.transaction():
                        await connection.execute(
                            f"UPDATE mainnet_launch_sessions SET {column} = $1 "
                            "WHERE launch_id = $2",
                            bad_value,
                            pilot_id,
                        )
            pilot_repository = PersistenceRepository(connection)
            active_pilot = await pilot_repository.get_mainnet_launch(pilot_id)
            assert active_pilot["state"] == "ACTIVE"
            assert active_pilot["pilot_campaign_id"] == campaign_id
            # Two real database connections race for the same close identity.
            # No exchange adapter is involved: this proves durable POST authority.
            claim_entry = "pilot-concurrent-close-entry"
            claim_close = "pilot-concurrent-close"
            await connection.execute(
                "UPDATE mainnet_launch_sessions SET basket_id = $1 WHERE launch_id = $2",
                "pilot-concurrent-close-basket", pilot_id,
            )
            await protection_repository.upsert_protection({
                **emergency_record,
                "entry_client_order_id": claim_entry,
                "basket_id": "pilot-concurrent-close-basket",
                "mainnet_launch_id": pilot_id,
                "stop_client_algo_id": "pilot-claim-stop",
                "take_profit_client_algo_id": "pilot-claim-target",
            })
            claim_connection = await connect()
            try:
                other_protections = PersistenceRepository(claim_connection).algo_protections
                claims = await asyncio.gather(
                    protection_repository.claim_local_emergency_close(
                        "ETHUSDC", claim_entry, claim_close, claimant_id="worker-one"),
                    other_protections.claim_local_emergency_close(
                        "ETHUSDC", claim_entry, claim_close, claimant_id="worker-two"),
                )
                assert sum(bool(item["claimed"]) for item in claims) == 1
                winner = next(item for item in claims if item["claimed"])
                attempts = await asyncio.gather(
                    protection_repository.mark_local_emergency_close_attempted(
                        "ETHUSDC", claim_entry, claim_close,
                        claimant_id=winner["claimant_id"], fencing_token=winner["fencing_token"]),
                    other_protections.mark_local_emergency_close_attempted(
                        "ETHUSDC", claim_entry, claim_close,
                        claimant_id=winner["claimant_id"], fencing_token=winner["fencing_token"]),
                )
                assert attempts.count(True) == 1
                assert attempts.count(False) == 1
                immutable_before = await connection.fetchrow(
                    "SELECT * FROM binance_emergency_close_claims WHERE entry_client_order_id = $1",
                    claim_entry,
                )
                for assignment in (
                    "status = 'RESERVED', attempted_at = NULL",
                    "attempted_at = NULL",
                    "claimant_id = 'replacement'",
                    "fencing_token = fencing_token + 1",
                    "close_client_order_id = 'replacement-close'",
                ):
                    with pytest.raises(asyncpg.RaiseError):
                        async with connection.transaction():
                            await connection.execute(
                                f"UPDATE binance_emergency_close_claims SET {assignment} WHERE entry_client_order_id = $1",
                                claim_entry,
                            )
                immutable_after = await connection.fetchrow(
                    "SELECT * FROM binance_emergency_close_claims WHERE entry_client_order_id = $1",
                    claim_entry,
                )
                assert dict(immutable_after) == dict(immutable_before)
                transfer_entry = "pilot-expired-close-entry"
                transfer_close = "pilot-expired-close"
                await protection_repository.upsert_protection({
                    **emergency_record,
                    "entry_client_order_id": transfer_entry,
                    "basket_id": "pilot-concurrent-close-basket",
                    "mainnet_launch_id": pilot_id,
                    "stop_client_algo_id": "pilot-expiry-stop",
                    "take_profit_client_algo_id": "pilot-expiry-target",
                })
                async def discard_reservation_ack():
                    await protection_repository.claim_local_emergency_close(
                        "ETHUSDC", transfer_entry, transfer_close,
                        claimant_id="worker-expired", lease_seconds=2,
                    )
                    raise ConnectionError("Injected lost acknowledgment after committed reservation")

                with pytest.raises(ConnectionError, match="lost acknowledgment"):
                    await discard_reservation_ack()
                recovery_connection = await connect()
                try:
                    reservation_recovery = await PersistenceRepository(recovery_connection).algo_protections.claim_local_emergency_close(
                        "ETHUSDC", transfer_entry, transfer_close, claimant_id="worker-uncertain",
                    )
                    assert reservation_recovery["claimed"] is False
                    assert reservation_recovery["status"] == "RESERVED"
                    old_claim = dict(await recovery_connection.fetchrow(
                        "SELECT * FROM binance_emergency_close_claims WHERE entry_client_order_id = $1",
                        transfer_entry,
                    ))
                finally:
                    await recovery_connection.close()
                await connection.execute("SELECT pg_sleep(2.1)")
                new_claim = await other_protections.claim_local_emergency_close(
                    "ETHUSDC", transfer_entry, transfer_close, claimant_id="worker-takeover", lease_seconds=1,
                )
                assert new_claim["claimed"] is True
                assert new_claim["fencing_token"] == old_claim["fencing_token"] + 1
                for wrong_close, wrong_claimant, wrong_token in (
                    (transfer_close, "worker-wrong", new_claim["fencing_token"]),
                    (transfer_close, "worker-takeover", old_claim["fencing_token"]),
                    ("wrong-close-identity", "worker-takeover", new_claim["fencing_token"]),
                ):
                    assert await other_protections.mark_local_emergency_close_attempted(
                        "ETHUSDC", transfer_entry, wrong_close,
                        claimant_id=wrong_claimant, fencing_token=wrong_token,
                    ) is False
                untouched_claim = await claim_connection.fetchrow(
                    "SELECT * FROM binance_emergency_close_claims WHERE entry_client_order_id = $1",
                    transfer_entry,
                )
                assert dict(untouched_claim) == {key: value for key, value in new_claim.items() if key != "claimed"}
                assert await protection_repository.mark_local_emergency_close_attempted(
                    "ETHUSDC", transfer_entry, transfer_close,
                    claimant_id="worker-expired", fencing_token=old_claim["fencing_token"],
                ) is False
                async def discard_attempt_ack():
                    await other_protections.mark_local_emergency_close_attempted(
                        "ETHUSDC", transfer_entry, transfer_close,
                        claimant_id="worker-takeover", fencing_token=new_claim["fencing_token"],
                    )
                    raise ConnectionError("Injected lost acknowledgment after committed attempt")

                with pytest.raises(ConnectionError, match="lost acknowledgment"):
                    await discard_attempt_ack()
                recovery_connection = await connect()
                try:
                    recovery_repo = PersistenceRepository(recovery_connection).algo_protections
                    attempt_recovery = await recovery_repo.claim_local_emergency_close(
                        "ETHUSDC", transfer_entry, transfer_close, claimant_id="worker-uncertain",
                    )
                    assert attempt_recovery["claimed"] is False
                    assert attempt_recovery["status"] == "ATTEMPTED"
                    assert await recovery_repo.mark_local_emergency_close_attempted(
                        "ETHUSDC", transfer_entry, transfer_close,
                        claimant_id="worker-takeover", fencing_token=new_claim["fencing_token"],
                    ) is False
                finally:
                    await recovery_connection.close()
                await connection.execute("SELECT pg_sleep(1.1)")
                assert await connection.fetchval(
                    "SELECT lease_until <= clock_timestamp() FROM binance_emergency_close_claims "
                    "WHERE entry_client_order_id = $1", transfer_entry,
                ) is True
                for claimant in ("worker-takeover", "worker-replacement"):
                    after_expiry = await other_protections.claim_local_emergency_close(
                        "ETHUSDC", transfer_entry, transfer_close, claimant_id=claimant,
                    )
                    assert after_expiry["claimed"] is False
                    assert after_expiry["status"] == "ATTEMPTED"
                    assert await other_protections.mark_local_emergency_close_attempted(
                        "ETHUSDC", transfer_entry, transfer_close,
                        claimant_id=claimant, fencing_token=new_claim["fencing_token"],
                    ) is False
                with pytest.raises(asyncpg.RaiseError):
                    async with connection.transaction():
                        await connection.execute(
                            "DELETE FROM binance_emergency_close_claims WHERE entry_client_order_id = $1",
                            claim_entry,
                        )
            finally:
                await claim_connection.close()
            # A fresh connection cannot regain submission permission after an attempt.
            claim_connection = await connect()
            try:
                recovered = await PersistenceRepository(claim_connection).algo_protections.claim_local_emergency_close(
                    "ETHUSDC", claim_entry, claim_close, claimant_id="worker-restarted")
                assert recovered["status"] == "ATTEMPTED"
                assert recovered["claimed"] is False
            finally:
                await claim_connection.close()
            with pytest.raises(asyncpg.ForeignKeyViolationError):
                async with connection.transaction():
                    await connection.execute(
                        """INSERT INTO local_live_pilot_events
                           (campaign_id, launch_id, event_key, event_type, source,
                            observed_at, payload_sha256, payload)
                           VALUES ($1,$2,'STATE:wrong-owner','STATE','WORKER',
                            CURRENT_TIMESTAMP,$3,'{}'::jsonb)""",
                        "pilot-mismatched-owner", pilot_id, "a" * 64,
                    )

            run_id = pilot_id
            observed = datetime.now(UTC)
            fill_event = {
                "campaign_id": campaign_id,
                "launch_id": pilot_id,
                "run_id": run_id,
                "symbol": "ETHUSDC",
                "event_key": "FILL:trade-1",
                "event_type": "FILL",
                "source": "BINANCE",
                "observed_at": observed,
                "payload": {"run_id": run_id, "exchange_event_id": "trade-1"},
                "net_pnl_delta_usdc": "1.00",
            }
            first_accounting = await pilot_repository.append_local_live_pilot_event(**fill_event)
            # Hold realized deltas until a fresh mark replaces the previous
            # unrealized snapshot; otherwise the same exposure is double counted.
            assert first_accounting["pilot_net_pnl_usdc"] == Decimal("0E-8")
            repeated_accounting = await pilot_repository.append_local_live_pilot_event(**fill_event)
            assert repeated_accounting["pilot_net_pnl_usdc"] == Decimal("0E-8")
            with pytest.raises(RuntimeError, match="event key was reused with conflicting evidence"):
                await pilot_repository.append_local_live_pilot_event(
                    **{**fill_event, "net_pnl_delta_usdc": "1.01"}
                )
            with pytest.raises(RuntimeError, match="event key was reused with conflicting evidence"):
                await pilot_repository.append_local_live_pilot_event(
                    **{
                        **fill_event,
                        "observed_at": observed + timedelta(milliseconds=1),
                    }
                )
            fee_resume = await pilot_repository.append_local_live_pilot_event(
                campaign_id=campaign_id, launch_id=pilot_id, run_id=run_id,
                symbol="ETHUSDC", event_key="FEE:trade-1", event_type="FEE",
                source="BINANCE", observed_at=observed + timedelta(milliseconds=1),
                payload={"run_id": run_id, "exchange_event_id": "trade-1", "asset": "USDC"},
                net_pnl_delta_usdc="-0.10",
            )
            assert fee_resume["pilot_accounting_resume_eligible"] is True
            with pytest.raises(RuntimeError, match="mark predates a financial event"):
                await pilot_repository.append_local_live_pilot_event(
                    campaign_id=campaign_id, launch_id=pilot_id, run_id=run_id,
                    symbol="ETHUSDC", event_key="MARK:pre-fee", event_type="MARK",
                    source="BINANCE", observed_at=observed + timedelta(microseconds=500),
                    payload={"run_id": run_id, "account_snapshot_id": "pre-fee", "unrealized_pnl_usdc": "0.80"},
                )
            mark_one = await pilot_repository.append_local_live_pilot_event(
                campaign_id=campaign_id, launch_id=pilot_id, run_id=run_id,
                symbol="ETHUSDC", event_key="MARK:account-1", event_type="MARK",
                source="BINANCE", observed_at=observed + timedelta(milliseconds=2),
                payload={"run_id": run_id, "account_snapshot_id": "account-1", "unrealized_pnl_usdc": "0.80"},
            )
            mark_two = await pilot_repository.append_local_live_pilot_event(
                campaign_id=campaign_id, launch_id=pilot_id, run_id=run_id,
                symbol="ETHUSDC", event_key="MARK:account-2", event_type="MARK",
                source="BINANCE", observed_at=observed + timedelta(milliseconds=3),
                payload={"run_id": run_id, "account_snapshot_id": "account-2", "unrealized_pnl_usdc": "0.70"},
            )
            assert mark_one["pilot_net_pnl_usdc"] == Decimal("1.70000000")
            assert mark_two["pilot_net_pnl_usdc"] == Decimal("1.60000000")
            accounting_snapshot = await pilot_repository.get_local_live_pilot_accounting(pilot_id)
            assert accounting_snapshot["pilot_campaign_id"] == campaign_id
            assert accounting_snapshot["realized_pnl_usdc"] == Decimal("1.00000000")
            assert accounting_snapshot["fees_usdc"] == Decimal("0.10000000")
            assert accounting_snapshot["funding_usdc"] == Decimal("0E-8")
            assert accounting_snapshot["unrealized_pnl_usdc"] == "0.70"
            assert accounting_snapshot["pilot_net_pnl_usdc"] == Decimal("1.60000000")
            assert accounting_snapshot["last_financial_event_id"] < accounting_snapshot["last_mark_event_id"]
            assert (await pilot_repository.get_mainnet_launch(pilot_id))[
                "pilot_accounting_resume_eligible"
            ] is False
            with pytest.raises(RuntimeError, match="out-of-order pilot account snapshot"):
                await pilot_repository.append_local_live_pilot_event(
                    campaign_id=campaign_id, launch_id=pilot_id, run_id=run_id,
                    symbol="ETHUSDC", event_key="MARK:stale-account", event_type="MARK",
                    source="BINANCE", observed_at=observed + timedelta(milliseconds=2),
                    payload={"run_id": run_id, "account_snapshot_id": "stale-account", "unrealized_pnl_usdc": "0.90"},
                )
            below_threshold = await pilot_repository.append_local_live_pilot_event(
                campaign_id=campaign_id, launch_id=pilot_id, run_id=run_id,
                symbol="ETHUSDC", event_key="FEE:dd-below",
                event_type="FEE", source="BINANCE",
                observed_at=observed + timedelta(milliseconds=4),
                payload={"run_id": run_id, "exchange_event_id": "dd-below"},
                net_pnl_delta_usdc="-4.89",
            )
            assert below_threshold["pilot_net_pnl_usdc"] == Decimal("1.60000000")
            below_threshold = await pilot_repository.append_local_live_pilot_event(
                campaign_id=campaign_id, launch_id=pilot_id, run_id=run_id,
                symbol="ETHUSDC", event_key="MARK:account-3", event_type="MARK",
                source="BINANCE", observed_at=observed + timedelta(milliseconds=5),
                payload={"run_id": run_id, "account_snapshot_id": "account-3", "unrealized_pnl_usdc": "0.70"},
            )
            assert below_threshold["pilot_net_pnl_usdc"] == Decimal("-3.29000000")
            assert below_threshold["pilot_drawdown_triggered"] is False
            await pilot_repository.append_local_live_pilot_event(
                campaign_id=campaign_id, launch_id=pilot_id, run_id=run_id,
                symbol="ETHUSDC", event_key="FEE:dd-boundary", event_type="FEE",
                source="BINANCE", observed_at=observed + timedelta(milliseconds=5),
                payload={"run_id": run_id, "exchange_event_id": "dd-boundary"},
                net_pnl_delta_usdc="-0.01",
            )
            drawdown = await pilot_repository.append_local_live_pilot_event(
                campaign_id=campaign_id, launch_id=pilot_id, run_id=run_id,
                symbol="ETHUSDC", event_key="MARK:account-4", event_type="MARK",
                source="BINANCE", observed_at=observed + timedelta(milliseconds=7),
                payload={"run_id": run_id, "account_snapshot_id": "account-4", "unrealized_pnl_usdc": "0.70"},
            )
            assert drawdown["pilot_net_pnl_usdc"] == Decimal("-3.30000000")
            assert drawdown["pilot_peak_pnl_usdc"] == Decimal("1.70000000")
            assert drawdown["pilot_drawdown_triggered"] is True
            assert drawdown["pilot_status"] == "CLOSE_ONLY"
            assert drawdown["state"] == "PAUSED_NEW_RISK"
            concurrent_connection = await connect()
            try:
                concurrent_event = {
                    "campaign_id": campaign_id,
                    "launch_id": pilot_id,
                    "run_id": run_id,
                    "symbol": "ETHUSDC",
                    "event_key": "FILL:trade-concurrent",
                    "event_type": "FILL",
                    "source": "BINANCE",
                    "observed_at": observed + timedelta(milliseconds=8),
                    "payload": {"run_id": run_id, "exchange_event_id": "trade-concurrent"},
                    "net_pnl_delta_usdc": "0.10",
                }
                concurrent_results = await asyncio.gather(
                    pilot_repository.append_local_live_pilot_event(**concurrent_event),
                    PersistenceRepository(concurrent_connection).append_local_live_pilot_event(
                        **concurrent_event
                    ),
                )
                assert all(
                    result["pilot_net_pnl_usdc"] == Decimal("-3.30000000")
                    for result in concurrent_results
                )
            finally:
                await concurrent_connection.close()
            with pytest.raises(asyncpg.RaiseError):
                await connection.execute(
                    "DELETE FROM local_live_pilot_events WHERE campaign_id = $1", campaign_id
                )
            await connection.execute(
                "UPDATE mainnet_launch_sessions SET state = 'CLOSED' WHERE launch_id = $1",
                pilot_id,
            )
        finally:
            await connection.close()

        # A new connection represents the local worker starting after a restart.
        connection = await connect()
        try:
            assert await apply_migrations(connection) == []
            await verify_schema(connection)
            assert await connection.fetchval(
                "SELECT count(*) FROM public.local_schema_migrations"
            ) == len(migrations)
            local_row = await connection.fetchrow(
                "SELECT runtime_target, runtime_fingerprint, pending_order_client_order_id, "
                "first_order_client_order_id FROM mainnet_launch_sessions "
                "WHERE launch_id = 'migration-test-local'"
            )
            assert tuple(local_row.values()) == ("LOCAL", fingerprint, "pending_1", "first_1")
            pilot_row = await PersistenceRepository(connection).get_mainnet_launch(
                "migration-test-pilot"
            )
            assert pilot_row["pilot_campaign_id"] == "pilot-migration0001"
            assert pilot_row["pilot_net_pnl_usdc"] == Decimal("-3.30000000")
            assert pilot_row["pilot_peak_pnl_usdc"] == Decimal("1.70000000")
            assert pilot_row["pilot_drawdown_triggered"] is True
            assert pilot_row["pilot_status"] == "CLOSE_ONLY"
            assert pilot_row["pilot_last_account_snapshot_at"] == observed + timedelta(milliseconds=7)
            assert await connection.fetchval(
                "SELECT count(*) FROM local_live_pilot_events WHERE campaign_id = $1",
                "pilot-migration0001",
            ) == 14

            # Verify resume_mainnet_pilot_campaign and trigger_pilot_drawdown on PostgreSQL 17
            test_repo = PersistenceRepository(connection)
            await connection.execute(
                """UPDATE mainnet_launch_sessions
                   SET state = 'REAUTH_REQUIRED', pilot_status = 'ACTIVE',
                       pilot_drawdown_triggered = FALSE, pending_order_client_order_id = NULL
                   WHERE launch_id = 'migration-test-pilot'"""
            )
            pending_resume = await test_repo.resume_mainnet_pilot_campaign("migration-test-pilot")
            assert pending_resume is not None
            assert pending_resume["state"] == "PAUSED_NEW_RISK"
            assert await connection.fetchval(
                "SELECT state FROM mainnet_launch_sessions WHERE launch_id = 'migration-test-pilot'"
            ) == "PAUSED_NEW_RISK"
            await test_repo.append_local_live_pilot_event(
                campaign_id="pilot-migration0001", launch_id="migration-test-pilot",
                run_id="migration-test-pilot", symbol="ETHUSDC",
                event_key="MARK:restart-reconciliation", event_type="MARK",
                source="BINANCE", observed_at=observed + timedelta(milliseconds=9),
                payload={"run_id": "migration-test-pilot",
                         "account_snapshot_id": "restart-reconciliation",
                         "unrealized_pnl_usdc": "0.70"},
            )
            reconciled_accounting = await test_repo.get_local_live_pilot_accounting(
                "migration-test-pilot"
            )
            assert reconciled_accounting["last_financial_event_id"] < reconciled_accounting[
                "last_mark_event_id"
            ]
            resumed_pilot = await test_repo.resume_mainnet_pilot_campaign("migration-test-pilot")
            assert resumed_pilot is None  # the fresh mark already restored ACTIVE atomically
            active_after_mark = await test_repo.get_mainnet_launch("migration-test-pilot")
            assert active_after_mark["state"] == "ACTIVE"
            assert active_after_mark["pilot_status"] == "ACTIVE"

            triggered_dd = await test_repo.trigger_pilot_drawdown(
                "migration-test-pilot", reason="PILOT_DRAWDOWN_LIMIT_REACHED"
            )
            assert triggered_dd is not None
            assert triggered_dd["pilot_drawdown_triggered"] is True
            assert triggered_dd["pilot_status"] == "CLOSE_ONLY"
            assert triggered_dd["state"] == "PAUSED_NEW_RISK"

            await connection.execute(
                "UPDATE mainnet_launch_sessions SET state = 'CLOSED' WHERE launch_id = 'migration-test-pilot'"
            )

            persisted_event = await connection.fetchrow(
                "SELECT event_type, source, net_pnl_delta_usdc, payload "
                "FROM local_live_pilot_events WHERE campaign_id = $1 AND event_key = $2",
                "pilot-migration0001", "FILL:trade-concurrent",
            )
            assert tuple(persisted_event.values())[:3] == (
                "FILL", "BINANCE", Decimal("0.10000000")
            )
            persisted_payload = persisted_event["payload"]
            if isinstance(persisted_payload, str):
                persisted_payload = json.loads(persisted_payload)
            assert persisted_payload == {
                "run_id": "migration-test-pilot", "exchange_event_id": "trade-concurrent"
            }
            persisted = await connection.fetchrow(
                "SELECT image_digest, runtime_target, submitted_orders, continuation_approval_id "
                "FROM mainnet_launch_sessions WHERE launch_id = 'migration-test-cloud'"
            )
            assert tuple(persisted.values()) == (digest, "CLOUD_RUN", 1, "migration-test-review")
            protection_repository = PersistenceRepository(connection).algo_protections
            testnet_protection = await protection_repository.get_protection(
                "binance_testnet", "ETHUSDC", "restart-entry-1"
            )
            mainnet_protection = await protection_repository.get_protection(
                "binance_mainnet", "ETHUSDC", "restart-entry-1"
            )
            assert testnet_protection is not None
            assert testnet_protection["state"] == "PROTECTED"
            assert str(testnet_protection["stop_algo_id"]) == "71001"
            assert mainnet_protection is not None
            assert mainnet_protection["state"] == "CLOSED"
            assert mainnet_protection["closure_evidence"]["kind"] == (
                "BINANCE_ALGO_CLOSE_VERIFIED"
            )
            assert len(mainnet_protection["closure_evidence"]["proof_sha256"]) == 64
            emergency_protection = await protection_repository.get_protection(
                "binance_mainnet", "ETHUSDC", "restart-emergency-entry"
            )
            assert emergency_protection is not None
            assert emergency_protection["state"] == "CLOSED"
            assert emergency_protection["closure_evidence"]["kind"] == (
                "LOCAL_EMERGENCY_CLOSE_VERIFIED"
            )
            zero_fill_protection = await protection_repository.get_protection(
                "binance_mainnet", "ETHUSDC", "restart-zero-fill-entry"
            )
            assert zero_fill_protection is not None
            assert zero_fill_protection["state"] == "CLOSED"
            assert zero_fill_protection["closure_evidence"]["kind"] == "UNFILLED_ENTRY_TERMINAL"
            active_claim_owners = await protection_repository.list_active_protections(
                "binance_mainnet", "ETHUSDC"
            )
            assert {row["entry_client_order_id"] for row in active_claim_owners} == {claim_entry, transfer_entry}
            assert all(row["state"] == "CLOSE_PENDING" for row in active_claim_owners)
            assert len(
                await protection_repository.list_active_protections(
                    "binance_testnet", "ETHUSDC"
                )
            ) == 1
            assert await protection_repository.set_protection_state(
                "binance_testnet", "ETHUSDC", "restart-entry-1", "CLOSE_PENDING"
            ) is not None
            assert await protection_repository.set_protection_state(
                "binance_testnet", "ETHUSDC", "restart-entry-1", "CLOSED"
            ) is not None
            assert await protection_repository.list_active_protections(
                "binance_testnet", "ETHUSDC"
            ) == []
            history_repository = PersistenceRepository(connection).binance_history
            persisted_history = await history_repository.list_history_items(checkpoint)
            assert persisted_history == [
                {
                    "item_id": 12345,
                    "client_id": "read-only-baseline-check",
                    "event_at": history_now,
                    "payload_sha256": changed_item["payload_sha256"],
                    "payload": changed_payload,
                    "observed_at": history_now + timedelta(milliseconds=1),
                }
            ]
            checkpoint_row = await connection.fetchrow(
                "SELECT cursor_id, coverage_status, covered_through "
                "FROM binance_history_checkpoints WHERE runtime_target = 'TESTNET' "
                "AND run_id = $1 AND symbol = 'ETHUSDC' AND history_kind = 'ALL_ALGO_ORDERS'",
                history_run_id,
            )
            assert int(checkpoint_row["cursor_id"]) == 12345
            assert checkpoint_row["coverage_status"] == "COVERED"
            assert checkpoint_row["covered_through"] == history_now

            # Exercise the restart fence against PostgreSQL itself, not only
            # the asyncpg-shaped unit-test double.
            await connection.execute(
                "UPDATE mainnet_launch_sessions SET state = 'CLOSED' "
                "WHERE launch_id = 'migration-test-cloud'"
            )
            await connection.execute(
                "INSERT INTO mainnet_launch_sessions "
                "(launch_id, approval_id, symbol, policy, state, runtime_target, "
                "runtime_fingerprint, reserved_orders, submitted_orders, "
                "pending_order_client_order_id) "
                "VALUES ('migration-test-pending', 'migration-test-pending-approval', "
                "'ETHUSDC', 'STAGED_FIRST_ORDER', 'ACTIVE', 'LOCAL', $1, 1, 0, 'entry-pending-1')",
                fingerprint,
            )
            repository = PersistenceRepository(connection)
            assert await repository.mark_mainnet_launches_reauth_required() == 1
            assert await repository.mark_mainnet_risk_order_submitted(
                "migration-test-pending", "entry-pending-1"
            ) is True
            pending = await connection.fetchrow(
                "SELECT state, submitted_orders, pending_order_client_order_id "
                "FROM mainnet_launch_sessions WHERE launch_id = 'migration-test-pending'"
            )
            assert tuple(pending.values()) == ("RECONCILIATION_REQUIRED", 1, None)
            assert await repository.mark_mainnet_launch_reconciled("migration-test-pending") is True
            assert await connection.fetchval(
                "SELECT state FROM mainnet_launch_sessions "
                "WHERE launch_id = 'migration-test-pending'"
            ) == "PAUSED_NEW_RISK"
            # Release the fixture's single-active-session slot before creating
            # the distinct pilot session below.
            await connection.execute(
                "UPDATE mainnet_launch_sessions SET state = 'CLOSED' "
                "WHERE launch_id = 'migration-test-pending'"
            )
            restart_pilot_id = "migration-test-pilot-restart"
            restart_campaign_id = "pilot-migration0002"
            await connection.execute(
                """INSERT INTO mainnet_launch_sessions
                   (launch_id, approval_id, symbol, policy, state, runtime_target,
                    runtime_fingerprint, max_risk_increasing_orders, pilot_campaign_id,
                    pilot_git_sha, pilot_source_hash, pilot_dependency_hash,
                    pilot_migration_hash, pilot_strategy_hash, pilot_secret_project_id,
                    pilot_api_key_version, pilot_api_secret_version, pilot_management_mode,
                    pilot_campaign_expires_at, pilot_risk_policy_hash,
                    pilot_max_position_notional_usdc, pilot_per_position_risk_usdc,
                    pilot_max_drawdown_usdc, pilot_quick_target_net_usdc,
                    pilot_quick_max_hold_seconds, pilot_max_leverage, pilot_status)
                   VALUES ($1, 'migration-test-pilot-restart-approval', 'ETHUSDC',
                    'LIVE_RESEARCH_PILOT', 'ACTIVE', 'LOCAL', $2, NULL, $3,
                    $4, $5, $5, $5, $5, 'test-project', '1', '2', 'QUICK',
                    CURRENT_TIMESTAMP + INTERVAL '7 days', $5, 50, 2, 5, 0.25, 86400, 10, 'ACTIVE')""",
                restart_pilot_id, "f" * 64, restart_campaign_id,
                "a" * 40, hash_value,
            )
            assert await repository.mark_mainnet_launches_reauth_required() == 1
            fenced_pilot = await repository.get_mainnet_launch(restart_pilot_id)
            assert fenced_pilot["state"] == "REAUTH_REQUIRED"
            pilot_readback = await connect()
            try:
                readback_row = await PersistenceRepository(pilot_readback).get_mainnet_launch(
                    restart_pilot_id
                )
                assert readback_row["state"] == "REAUTH_REQUIRED"
            finally:
                await pilot_readback.close()
            with pytest.raises(RuntimeError, match="does not match its durable Local campaign"):
                await repository.append_local_live_pilot_event(
                    campaign_id=restart_campaign_id, launch_id=restart_pilot_id,
                    run_id=restart_pilot_id, symbol="ETHUSDC",
                    event_key="restart-fenced-event", event_type="STATE",
                    source="WORKER", observed_at=observed + timedelta(seconds=1),
                    payload={"run_id": restart_pilot_id},
                )

            # Two connections represent competing processes. A stale owner must
            # never renew or release the newer owner's fencing token.
            other = await connect()
            try:
                scope = "migration-test:isolated-account"
                first = PostgresExecutionLease(connection, scope, "first", ttl_seconds=30)
                second = PostgresExecutionLease(other, scope, "second", ttl_seconds=30)
                assert await first.acquire() is True
                first_token = first.fencing_token
                assert await second.acquire() is False
                assert await first.renew() is True
                await first.assert_valid()
                await first.release()
                assert await second.acquire() is True
                assert second.fencing_token == first_token + 1
                # Simulate a restarted process retaining stale in-memory state.
                stale = PostgresExecutionLease(connection, scope, "first")
                stale.fencing_token = first_token
                assert await stale.renew() is False
                with pytest.raises(LeaseLostError):
                    await stale.assert_valid()
                await stale.release()
                await second.assert_valid()
                assert await connection.fetchval(
                    "SELECT fencing_token FROM execution_leases WHERE scope_key = $1", scope
                ) == second.fencing_token
                await second.release()
            finally:
                await other.close()
        finally:
            await connection.close()

    asyncio.run(scenario())
