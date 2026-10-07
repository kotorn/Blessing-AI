"""Apply the repository's Postgres migrations once to the isolated loopback DB."""

from __future__ import annotations

import asyncio
import hashlib
import os
import re
import sys
from pathlib import Path
from typing import Any

import asyncpg

REPO_ROOT = Path(__file__).resolve().parents[1]
MIGRATIONS_DIR = REPO_ROOT / "infra" / "postgres" / "migrations"
MIGRATION_LOCK_KEY = 0x424C455353494E47
REQUIRED_LOCAL_PILOT_MIGRATION = "017_local_live_pilot_runtime.sql"
REQUIRED_LOCAL_PILOT_CONSTRAINTS = frozenset(
    {
        "mainnet_launch_pilot_binding_check",
        "local_live_pilot_event_type_check",
        "local_live_pilot_event_source_check",
        "local_live_pilot_event_payload_check",
    }
)
MIGRATION_NAME = re.compile(r"^[0-9]{3}_[a-z0-9_]+\.sql$")
SAFE_POPULATED_UPGRADES = {
    # These exact SQL bytes were reviewed as additive identity/ownership
    # migrations. A filename alone is never authorization to alter a populated
    # trading database; any edit requires a new reviewed digest here.
    "007_mainnet_launch_runtime_identity.sql":
        "4df64f954486eabf118fb36044dfe875aebf7f467b503eeadf99ffd7d6e9cc13",
    "008_mainnet_launch_order_identity.sql":
        "6a73a7bd9620c3c6a297fcc1173701ed1b8b8ee5785b7f6a04918a2829a84d50",
    "009_binance_algo_protection_ownership.sql":
        "ae93d7c03c0954f72057a05ee7072bf3c370379ccc8dff40a730e579e28310c8",
    "010_binance_history_checkpoints.sql":
        "7a644ce9a2b32733518b89ccbe42aae1ba480868f30d24b1124a9c79f4a219c3",
    "011_local_mainnet_basket_identity.sql":
        "29eb75acdc445433517be032a7230f0729ea9a01b95d37fefb70069788a4a9a2",
    "012_mainnet_basket_owner_link.sql":
        "0511b2549641736a26ebc815aea5e5a7440e1cb29661c9d5f9a2ac9ba4326f14",
    "013_binance_algo_history_observations.sql":
        "0844476c679065be20c0d54ad5d132f8fa091550b7c4e1f01680b70776657029",
    "014_mainnet_closure_evidence.sql": "1a6c8e7532739a63a3e6c1d830a66d34a0a63862c37d4fbe551a4f15da72345d",
    "015_local_emergency_close_evidence.sql": "b8d973652963e8118fc64850334213f694bdcc4313005c7536108a8f7ebc2f8e",
    "016_live_research_pilot_and_history_fencing.sql": "25aeb77a0cbf2cf2194c76543614278ba82e493cd5fab00eb294da7075bc122f",
    "017_local_live_pilot_runtime.sql": "17cc101d27218946384e42e713811522cfedba27dd2f62f76fa69c806c4c2cff",
    "018_local_pilot_binding_not_null.sql": "75700a45774c23aeebdc055be57c774df13ad6c0c5f22a75b64dfb6e557b14fa",
    "019_local_pilot_event_ownership.sql": "1831a48d9de6f3268f29ca584d2445757df44d7ea9cdd275df4c17591a659f77",
}
REQUIRED_COLUMNS = {
    "persistence_outbox": {
        "event_id",
        "event_type",
        "idempotency_key",
        "aggregate_id",
        "payload",
        "status",
    },
    "execution_leases": {
        "scope_key",
        "owner_id",
        "fencing_token",
        "lease_until",
    },
    "mainnet_launch_sessions": {
        "launch_id",
        "approval_id",
        "image_digest",
        "symbol",
        "policy",
        "max_risk_increasing_orders",
        "reserved_orders",
        "submitted_orders",
        "state",
        "continuation_approval_id",
        "first_order_verified_at",
        "autonomous_approved_at",
        "last_restart_at",
        "pending_order_client_order_id",
        "first_order_client_order_id",
        "runtime_target",
        "runtime_fingerprint",
        "basket_id",
        "created_at",
        "updated_at",
        "pilot_campaign_expires_at",
        "pilot_strategy_hash",
        "pilot_risk_policy_hash",
        "pilot_max_position_notional_usdc",
        "pilot_per_position_risk_usdc",
        "pilot_max_drawdown_usdc",
        "pilot_peak_pnl_usdc",
        "pilot_drawdown_triggered",
        "pilot_quick_target_net_usdc",
        "pilot_quick_max_hold_seconds",
        "pilot_max_leverage",
        "pilot_campaign_id",
        "pilot_git_sha",
        "pilot_source_hash",
        "pilot_dependency_hash",
        "pilot_migration_hash",
        "pilot_secret_project_id",
        "pilot_api_key_version",
        "pilot_api_secret_version",
        "pilot_management_mode",
        "pilot_net_pnl_usdc",
        "pilot_status",
        "pilot_last_account_snapshot_at",
    },
    "binance_algo_protections": {
        "environment",
        "venue",
        "symbol",
        "entry_client_order_id",
        "basket_id",
        "mainnet_launch_id",
        "closure_evidence",
        "entry_side",
        "position_side",
        "requested_quantity",
        "filled_quantity",
        "entry_average_price",
        "stop_trigger_price",
        "take_profit_trigger_price",
        "stop_algo_id",
        "take_profit_algo_id",
        "stop_client_algo_id",
        "take_profit_client_algo_id",
        "state",
        "state_reason",
        "first_fill_at",
        "protection_verified_at",
        "last_reconciled_at",
        "closed_at",
        "created_at",
        "updated_at",
        "management_mode",
    },
    "binance_history_anchors": {
        "runtime_target",
        "run_id",
        "symbol",
        "anchor_at",
        "anchor_source",
        "mainnet_launch_id",
        "created_at",
    },
    "binance_history_checkpoints": {
        "runtime_target",
        "run_id",
        "symbol",
        "history_kind",
        "cursor_id",
        "coverage_status",
        "covered_through",
        "scan_started_at",
        "scan_from_at",
        "scan_to_at",
        "last_page_at",
        "failure_code",
        "created_at",
        "updated_at",
        "scan_id",
    },
    "binance_history_items": {
        "runtime_target",
        "run_id",
        "symbol",
        "history_kind",
        "item_id",
        "client_id",
        "event_at",
        "payload_sha256",
        "observed_at",
    },
    "binance_preexisting_algo_baselines": {
        "runtime_target",
        "run_id",
        "symbol",
        "algo_id",
        "client_algo_id",
        "algo_created_at",
        "anchor_at",
        "terminal_status",
        "snapshot_observed_at",
        "position_snapshot",
        "open_orders_snapshot",
        "open_algo_orders_snapshot",
        "proof_sha256",
        "created_at",
    },
    "binance_algo_history_observations": {
        "observation_id",
        "runtime_target",
        "run_id",
        "symbol",
        "history_kind",
        "item_id",
        "client_id",
        "event_at",
        "payload_sha256",
        "payload",
        "observed_at",
    },
    "binance_history_item_observations": {
        "observation_id",
        "runtime_target",
        "run_id",
        "symbol",
        "history_kind",
        "item_id",
        "client_id",
        "event_at",
        "payload_sha256",
        "payload",
        "observed_at",
    },
    "local_live_pilot_events": {
        "event_id", "campaign_id", "launch_id", "event_key", "event_type", "source",
        "observed_at", "net_pnl_delta_usdc", "payload_sha256", "payload",
    },
    "binance_emergency_close_claims": {
        "venue", "symbol", "entry_client_order_id", "close_client_order_id",
        "claimant_id", "fencing_token", "lease_until", "status", "attempted_at",
        "created_at", "updated_at",
    },
}
REQUIRED_COLUMN_NULLABILITY = {
    "binance_emergency_close_claims": {
        column: "YES" if column == "attempted_at" else "NO"
        for column in REQUIRED_COLUMNS["binance_emergency_close_claims"]
    },
}
MIGRATED_DATA_TABLES = (
    "orders",
    "fills",
    "positions",
    "persistence_outbox",
    "execution_leases",
    "mainnet_launch_sessions",
    "binance_algo_protections",
    "binance_history_anchors",
    "binance_history_checkpoints",
    "binance_history_items",
    "binance_preexisting_algo_baselines",
    "binance_algo_history_observations",
    "binance_history_item_observations",
    "local_live_pilot_events",
    "binance_emergency_close_claims",
)
REQUIRED_INDEXES = {
    "idx_persistence_outbox_idempotency": (
        "persistence_outbox",
        ("event_type", "idempotency_key"),
        True,
        None,
    ),
    "idx_execution_leases_expiry": (
        "execution_leases",
        ("lease_until",),
        False,
        None,
    ),
    "idx_mainnet_launch_one_active": (
        "mainnet_launch_sessions",
        ("symbol",),
        True,
        frozenset(
            {
                "ACTIVE",
                "PAUSED_NEW_RISK",
                "RECONCILIATION_REQUIRED",
                "AUTONOMOUS_ACTIVE",
                "REAUTH_REQUIRED",
            }
        ),
    ),
    "idx_mainnet_launch_continuation_approval": (
        "mainnet_launch_sessions",
        ("continuation_approval_id",),
        True,
        "NOT_NULL",
    ),
    "idx_mainnet_launch_basket_unique": (
        "mainnet_launch_sessions",
        ("basket_id",),
        True,
        "NOT_NULL:basket_id",
    ),
    "idx_binance_algo_protections_nonterminal": (
        "binance_algo_protections",
        ("venue", "symbol", "created_at"),
        False,
        "NOT_CLOSED",
    ),
    "idx_binance_history_items_client": (
        "binance_history_items",
        ("runtime_target", "run_id", "symbol", "history_kind", "client_id"),
        False,
        "NOT_NULL:client_id",
    ),
    "idx_binance_algo_history_observation_latest": (
        "binance_algo_history_observations",
        ("runtime_target", "run_id", "symbol", "item_id", "observed_at", "observation_id"),
        False,
        None,
    ),
    "idx_binance_history_item_observation_latest": (
        "binance_history_item_observations",
        ("runtime_target", "run_id", "symbol", "history_kind", "item_id", "observed_at", "observation_id"),
        False,
        None,
    ),
    "idx_mainnet_launch_pilot_campaign": (
        "mainnet_launch_sessions", ("pilot_campaign_id",), True, "NOT_NULL:pilot_campaign_id",
    ),
    "idx_local_live_pilot_events_campaign_time": (
        "local_live_pilot_events", ("campaign_id", "observed_at", "event_id"), False, None,
    ),
}
REQUIRED_UNIQUE_KEYS = (
    ("persistence_outbox", ("event_id",), True),
    ("execution_leases", ("scope_key",), True),
    ("mainnet_launch_sessions", ("launch_id",), True),
    ("mainnet_launch_sessions", ("launch_id", "basket_id"), False),
    ("mainnet_launch_sessions", ("approval_id",), False),
    ("mainnet_launch_sessions", ("pilot_campaign_id",), False),
    ("mainnet_launch_sessions", ("launch_id", "pilot_campaign_id"), False),
    ("binance_algo_protections", ("venue", "symbol", "entry_client_order_id"), True),
    ("local_live_pilot_events", ("event_id",), True),
    ("local_live_pilot_events", ("campaign_id", "event_key"), False),
    ("binance_emergency_close_claims", ("venue", "symbol", "entry_client_order_id"), True),
    ("binance_emergency_close_claims", ("venue", "close_client_order_id"), False),
    ("binance_history_anchors", ("runtime_target", "run_id", "symbol"), True),
    (
        "binance_history_checkpoints",
        ("runtime_target", "run_id", "symbol", "history_kind"),
        True,
    ),
    (
        "binance_history_items",
        ("runtime_target", "run_id", "symbol", "history_kind", "item_id"),
        True,
    ),
    (
        "binance_algo_history_observations",
        ("runtime_target", "run_id", "symbol", "item_id", "payload_sha256"),
        False,
    ),
    (
        "binance_preexisting_algo_baselines",
        ("runtime_target", "run_id", "symbol", "algo_id"),
        True,
    ),
)
REQUIRED_FOREIGN_KEYS = {
    "binance_emergency_close_claim_owner_fk": (
        "binance_emergency_close_claims", ("venue", "symbol", "entry_client_order_id"),
        "binance_algo_protections", ("venue", "symbol", "entry_client_order_id"),
    ),
    "binance_algo_history_observation_scope_fk": (
        "binance_algo_history_observations",
        ("runtime_target", "run_id", "symbol", "history_kind"),
        "binance_history_checkpoints",
        ("runtime_target", "run_id", "symbol", "history_kind"),
    ),
    "binance_history_item_observation_scope_fk": (
        "binance_history_item_observations",
        ("runtime_target", "run_id", "symbol", "history_kind"),
        "binance_history_checkpoints",
        ("runtime_target", "run_id", "symbol", "history_kind"),
    ),
    "binance_algo_protection_launch_basket_fk": (
        "binance_algo_protections",
        ("mainnet_launch_id", "basket_id"),
        "mainnet_launch_sessions",
        ("launch_id", "basket_id"),
    ),
    "local_live_pilot_events_launch_id_fkey": (
        "local_live_pilot_events", ("launch_id",), "mainnet_launch_sessions", ("launch_id",),
    ),
    "local_live_pilot_events_owner_fk": (
        "local_live_pilot_events", ("launch_id", "campaign_id"),
        "mainnet_launch_sessions", ("launch_id", "pilot_campaign_id"),
    ),
}
REQUIRED_CONSTRAINTS = {
    "binance_emergency_close_claim_shape_check": (
        "binance_emergency_close_claims",
        frozenset({"venue", "symbol", "close_client_order_id", "claimant_id",
                   "fencing_token", "status", "attempted_at"}),
        (),
    ),
    "persistence_outbox_status_check": (
        "persistence_outbox",
        frozenset({"status"}),
        ("check", "status", "=", "any", "array", "'pending'", "'processing'", "'processed'"),
    ),
    "mainnet_launch_policy_check": (
        "mainnet_launch_sessions",
        frozenset({"policy"}),
        ("check", "policy", "=", "any", "array", "'staged_first_order'", "'autonomous_after_review'", "'live_research_pilot'"),
    ),
    "mainnet_launch_limit_check": (
        "mainnet_launch_sessions",
        frozenset({"policy", "max_risk_increasing_orders"}),
        (
            "check",
            "policy",
            "=",
            "'staged_first_order'",
            "and",
            "max_risk_increasing_orders",
            "=",
            "1",
            "or",
            "policy",
            "=",
            "'autonomous_after_review'",
            "and",
            "max_risk_increasing_orders",
            "is",
            "null",
            "or",
            "policy",
            "=",
            "'live_research_pilot'",
            "and",
            "max_risk_increasing_orders",
            "=",
            "1",
        ),
    ),
    "mainnet_launch_reserved_check": (
        "mainnet_launch_sessions",
        frozenset({"reserved_orders", "max_risk_increasing_orders"}),
        (
            "check",
            "reserved_orders",
            ">=",
            "0",
            "and",
            "max_risk_increasing_orders",
            "is",
            "null",
            "or",
            "reserved_orders",
            "<=",
            "max_risk_increasing_orders",
        ),
    ),
    "mainnet_launch_submitted_check": (
        "mainnet_launch_sessions",
        frozenset({"submitted_orders", "max_risk_increasing_orders"}),
        (
            "check",
            "submitted_orders",
            ">=",
            "0",
            "and",
            "max_risk_increasing_orders",
            "is",
            "null",
            "or",
            "submitted_orders",
            "<=",
            "max_risk_increasing_orders",
        ),
    ),
    "mainnet_launch_state_check": (
        "mainnet_launch_sessions",
        frozenset({"state"}),
        (
            "check",
            "state",
            "=",
            "any",
            "array",
            "'active'",
            "'paused_new_risk'",
            "'reconciliation_required'",
            "'autonomous_active'",
            "'reauth_required'",
            "'closed'",
        ),
    ),
    "mainnet_launch_runtime_identity_check": (
        "mainnet_launch_sessions",
        frozenset({"runtime_target", "image_digest", "runtime_fingerprint"}),
        (
            "check",
            "runtime_target",
            "=",
            "'local'",
            "and",
            "image_digest",
            "is",
            "null",
            "and",
            "runtime_fingerprint",
            "is",
            "not",
            "null",
            "and",
            "runtime_fingerprint",
            "~*",
            "'^[0-9a-f]{64}$'",
            "or",
            "runtime_target",
            "=",
            "'cloud_run'",
            "and",
            "image_digest",
            "is",
            "not",
            "null",
            "and",
            "image_digest",
            "~*",
            "'^.+@sha256:[0-9a-f]{64}$'",
        ),
    ),
    "mainnet_launch_order_identity_check": (
        "mainnet_launch_sessions",
        frozenset({"pending_order_client_order_id", "first_order_client_order_id"}),
        (
            "check",
            "pending_order_client_order_id",
            "is",
            "null",
            "or",
            "pending_order_client_order_id",
            "~",
            "'^[A-Za-z0-9_-]{1,64}$'",
            "and",
            "first_order_client_order_id",
            "is",
            "null",
            "or",
            "first_order_client_order_id",
            "~",
            "'^[A-Za-z0-9_-]{1,64}$'",
        ),
    ),
    "binance_algo_protection_environment_check": (
        "binance_algo_protections",
        frozenset({"venue", "environment"}),
        (
            "check", "venue", "=", "'binance_testnet'", "and", "environment", "=", "'testnet'",
            "or", "venue", "=", "'binance_mainnet'", "and", "environment", "=", "'mainnet'",
        ),
    ),
    "binance_algo_protection_launch_identity_check": (
        "binance_algo_protections",
        frozenset({"mainnet_launch_id", "environment", "basket_id"}),
        (
            "check", "mainnet_launch_id", "is", "null", "or",
            "environment", "=", "'mainnet'", "and", "basket_id", "is", "not", "null",
        ),
    ),
    "binance_algo_protection_entry_identity_check": (
        "binance_algo_protections",
        frozenset({"symbol", "entry_client_order_id", "entry_side", "position_side"}),
        (
            "check", "symbol", "~", "'^[A-Z0-9]{2,32}$'", "and",
            "entry_client_order_id", "~", "'^[A-Za-z0-9_-]{1,64}$'", "and",
            "entry_side", "=", "any", "array", "'buy'", "'sell'", "and",
            "position_side", "=", "any", "array", "'both'", "'long'", "'short'", "and",
            "entry_side", "=", "'buy'", "and", "position_side", "=", "any", "array", "'both'", "'long'",
            "or", "entry_side", "=", "'sell'", "and", "position_side", "=", "any", "array", "'both'", "'short'",
        ),
    ),
    "binance_algo_protection_quantity_check": (
        "binance_algo_protections",
        frozenset({"requested_quantity", "filled_quantity", "entry_average_price", "first_fill_at"}),
        (
            "check", "requested_quantity", ">", "0", "and", "filled_quantity", ">=", "0",
            "and", "filled_quantity", "<=", "requested_quantity", "and",
            "filled_quantity", "=", "0", "and", "entry_average_price", "is", "null",
            "and", "first_fill_at", "is", "null", "or", "filled_quantity", ">", "0",
            "and", "entry_average_price", "is", "not", "null", "and", "entry_average_price", ">", "0",
            "and", "first_fill_at", "is", "not", "null",
        ),
    ),
    "binance_algo_protection_trigger_check": (
        "binance_algo_protections",
        frozenset({"stop_trigger_price", "take_profit_trigger_price", "entry_average_price", "entry_side"}),
        (
            "check", "stop_trigger_price", ">", "0", "and", "take_profit_trigger_price", ">", "0",
            "and", "stop_trigger_price", "<>", "take_profit_trigger_price", "and",
            "entry_average_price", "is", "null", "or", "entry_side", "=", "'buy'",
            "and", "stop_trigger_price", "<", "entry_average_price", "and",
            "entry_average_price", "<", "take_profit_trigger_price", "or",
            "entry_side", "=", "'sell'", "and", "take_profit_trigger_price", "<",
            "entry_average_price", "and", "entry_average_price", "<", "stop_trigger_price",
        ),
    ),
    "binance_algo_protection_algo_identity_check": (
        "binance_algo_protections",
        frozenset({"stop_client_algo_id", "take_profit_client_algo_id", "stop_algo_id", "take_profit_algo_id"}),
        (
            "check", "stop_client_algo_id", "~", "'^[A-Za-z0-9_-]{1,64}$'", "and",
            "take_profit_client_algo_id", "~", "'^[A-Za-z0-9_-]{1,64}$'", "and",
            "stop_client_algo_id", "<>", "take_profit_client_algo_id", "and",
            "stop_algo_id", "is", "null", "or", "stop_algo_id", "~", "'^[0-9]{1,64}$'", "and",
            "take_profit_algo_id", "is", "null", "or", "take_profit_algo_id", "~", "'^[0-9]{1,64}$'", "and",
            "stop_algo_id", "is", "null", "or", "take_profit_algo_id", "is", "null", "or",
            "stop_algo_id", "<>", "take_profit_algo_id",
        ),
    ),
    "binance_algo_protection_state_check": (
        "binance_algo_protections",
        frozenset({"state", "filled_quantity", "stop_algo_id", "take_profit_algo_id", "protection_verified_at", "closed_at"}),
        (
            "check", "state", "=", "any", "array", "'pending'", "'protected'", "'close_pending'", "'closed'", "'degraded'", "'unknown'", "and",
            "state", "<>", "'protected'", "or", "filled_quantity", ">", "0", "and",
            "stop_algo_id", "is", "not", "null", "and", "take_profit_algo_id", "is", "not", "null",
            "and", "protection_verified_at", "is", "not", "null", "and",
            "state", "<>", "'closed'", "or", "closed_at", "is", "not", "null",
        ),
    ),
    "binance_algo_protection_closure_evidence_check": (
        "binance_algo_protections",
        frozenset({"environment", "state", "closure_evidence"}),
        (
            "check", "environment", "=", "'mainnet'", "and", "state", "=", "'closed'",
            "and", "jsonb_typeof", "(", "closure_evidence", ")", "=", "'object'",
            "and", "closure_evidence", "->>", "'kind'", "=", "any", "array",
            "'legacy_unverified'", "'binance_algo_close_verified'",
            "'local_emergency_close_verified'", "'unfilled_entry_terminal'",
            "or", "state", "<>", "'closed'", "or", "environment", "=", "'testnet'",
            "and", "closure_evidence", "is", "null",
        ),
    ),
    "binance_history_anchor_identity_check": (
        "binance_history_anchors",
        frozenset({"runtime_target", "run_id", "symbol", "anchor_source", "mainnet_launch_id"}),
        (),
    ),
    "binance_history_checkpoint_shape_check": (
        "binance_history_checkpoints",
        frozenset({"history_kind", "cursor_id", "coverage_status", "covered_through", "last_page_at", "scan_started_at", "scan_from_at", "scan_to_at", "failure_code"}),
        (),
    ),
    "binance_history_item_shape_check": (
        "binance_history_items",
        frozenset({"history_kind", "item_id", "payload_sha256"}),
        (),
    ),
    "binance_preexisting_algo_proof_check": (
        "binance_preexisting_algo_baselines",
        frozenset({"runtime_target", "run_id", "symbol", "algo_id", "client_algo_id", "algo_created_at", "anchor_at", "terminal_status", "snapshot_observed_at", "position_snapshot", "open_orders_snapshot", "open_algo_orders_snapshot", "proof_sha256"}),
        (),
    ),
    "mainnet_launch_pilot_policy_check": (
        "mainnet_launch_sessions",
        frozenset({"policy", "runtime_target", "symbol", "max_risk_increasing_orders", "created_at", "pilot_campaign_expires_at", "pilot_strategy_hash", "pilot_risk_policy_hash", "pilot_max_position_notional_usdc", "pilot_per_position_risk_usdc", "pilot_max_drawdown_usdc", "pilot_quick_target_net_usdc", "pilot_quick_max_hold_seconds", "pilot_max_leverage"}),
        (),
    ),
    "binance_algo_protection_management_mode_check": (
        "binance_algo_protections", frozenset({"management_mode"}), (),
    ),
    "binance_history_checkpoint_scan_fence_check": (
        "binance_history_checkpoints", frozenset({"coverage_status", "scan_id"}), (),
    ),
    "binance_history_item_observation_shape_check": (
        "binance_history_item_observations", frozenset({"history_kind", "item_id", "payload_sha256", "payload"}), (),
    ),
    "mainnet_launch_pilot_binding_check": (
        "mainnet_launch_sessions",
        frozenset({"policy", "pilot_campaign_id", "pilot_git_sha", "pilot_source_hash", "pilot_dependency_hash", "pilot_migration_hash", "pilot_strategy_hash", "pilot_risk_policy_hash", "pilot_secret_project_id", "pilot_api_key_version", "pilot_api_secret_version", "pilot_management_mode", "pilot_campaign_expires_at", "pilot_status", "runtime_target", "symbol", "max_risk_increasing_orders", "pilot_max_position_notional_usdc", "pilot_per_position_risk_usdc", "pilot_max_drawdown_usdc", "pilot_max_leverage", "created_at"}),
        (),
    ),
    "local_live_pilot_event_type_check": ("local_live_pilot_events", frozenset({"event_type"}), ()),
    "local_live_pilot_event_source_check": ("local_live_pilot_events", frozenset({"source"}), ()),
    "local_live_pilot_event_payload_check": ("local_live_pilot_events", frozenset({"payload_sha256", "payload"}), ()),
}
# PostgreSQL 17 canonical CHECK expressions for safety-critical launch and
# Algo-protection constraints. Hashing complete normalized definitions
# preserves operator direction and grouping; token comparison alone does not.
REQUIRED_CONSTRAINT_DEFINITION_SHA256 = {
    # pg_get_constraintdef from the read-only PostgreSQL 17.11 acceptance fixture.
    "binance_emergency_close_claim_shape_check":
        "57bf8d63a9c037332adfe5cef4d0b8d687fa95e849c67e7db1eb1fd5d26b8283",
    "persistence_outbox_status_check":
        "75ba5fc9e5235fb30830eab1c461dab5aa1a0c22208936fe3cf8fd4d1358f2ed",
    "mainnet_launch_policy_check":
        "ec93dbfd21170594e0d24440769f87076539f8a0c2eb2a2c9eefb52906b47961",
    "mainnet_launch_limit_check":
        "285d2a2485710a0372385dbba6dfa35b2b4def7cd8adea00571ba4f457f781b5",
    "mainnet_launch_reserved_check":
        "754e745f4099f48091634688a7ab71598c32c1fff804b3f070394db8940f915d",
    "mainnet_launch_submitted_check":
        "27ae4e4dee6376d7936f994d378210ae74a227c98f4490645fa2ff4d68738d92",
    "mainnet_launch_state_check":
        "7f7784df289f7fc37d63d23ce98b41c2496b920d0e59710c89ea7fdb9a14c70a",
    "mainnet_launch_runtime_identity_check":
        "d918c5f46f93333f2fbe7af1628e8841c8e561fbf75cabcccd7b7dcbe6b863ad",
    "mainnet_launch_order_identity_check":
        "6f780865898f9558b9bcaaca050d5917d1157fb7813972c551f0915d5bc19146",
    "binance_algo_protection_environment_check":
        "0c7697b72064d99ce69a3d8c00a3ff66539a3cb283bf2739ac76e1abffa605fa",
    "binance_algo_protection_launch_identity_check": "34a70a88343ca443db938c623a81cb99acc63b2237bf1937648957ab4f8e8911",
    "binance_algo_protection_entry_identity_check":
        "7afcaf5c5c4db685b62912cea4a42cb1e8ebc35c648bb0e0394c29d48e6be24c",
    "binance_algo_protection_quantity_check":
        "388ba477d6b3583b4cdd7310de0fc3f83be630a11a84ac8c89ca24ac4daa618c",
    "binance_algo_protection_trigger_check":
        "3e7682fbad72385b549f3f60444e14290e919fb7bd86dca0261595c712d594d3",
    "binance_algo_protection_algo_identity_check":
        "549a281f5d544a7e329054480851c9ad31d06932705c6c0b82f11cf16330b3f1",
    "binance_algo_protection_state_check":
        "a05113b8f2e573f7ba159be0a40455322e8ff09eeef4c2f10bad227ff6395e8b",
    "binance_algo_protection_closure_evidence_check": "f22a8e41789a23edf205a2937dff08d22af2de800c80521409e8cca8f4e9adee",
    "binance_history_anchor_identity_check":
        "9956dfc03dd89bdc13d87c7bc9de7b01b679c1f5842ac0fed695ec1025a1fda3",
    "binance_history_checkpoint_shape_check":
        "2fd051e16a34edb8b9c5d9b948381b0ac3add81e42aca1c7d7f413be919314d1",
    "binance_history_item_shape_check":
        "a302a72cf3216ca66eb6a693842abee7d910825523f8cbd4f54c366ccc9f19af",
    "binance_preexisting_algo_proof_check":
        "9ff319da1defd629e3ad6072f90168c1e0c0abbb80bf95f0ae0f3d1f58cf640a",
    "mainnet_launch_pilot_policy_check": "e602c26bc79ce50eee52285c7e0439f3b5cac711dc0c92e66484214c90ad9204",
    "binance_algo_protection_management_mode_check": "b06b0b0fd8479dfea990e3d5d922889e4086d3e6de3fb95c22f7e9a2835b8d59",
    "binance_history_checkpoint_scan_fence_check": "7a34f2359e9772f52cbf836a4b348782550440561b1e70fddea62f7b7a8a93a9",
    "binance_history_item_observation_shape_check": "f76b9d81d72c06e4c2ee0b60bab057fe171bfe617ace5765cb5931133ad542fe",
    "mainnet_launch_pilot_binding_check": "fe3d0408e55d68e58d9a81926ad8471df4dc929c82225ed4cfcb9dbb399c3719",
    "local_live_pilot_event_type_check": "dadf5f5734b503be5512731be1b16f9abbe9ac9fbb6d1c7a22fd4890a7164197",
    "local_live_pilot_event_source_check": "4b477b1d14da7b3550681d27c2e2162ce842ce67978a540a8e48dce248e49ee6",
    "local_live_pilot_event_payload_check": "85556805e9eb2092f095776969c572d06489b18c301c0aa1d553946a16373989",
}
REQUIRED_IMMUTABLE_TRIGGERS = {
    "binance_history_anchor_immutable": "binance_history_anchors",
    "binance_history_item_immutable": "binance_history_items",
    "binance_preexisting_algo_baseline_immutable": "binance_preexisting_algo_baselines",
    "binance_algo_history_observation_immutable": "binance_algo_history_observations",
    "binance_history_item_observation_immutable": "binance_history_item_observations",
    "local_live_pilot_event_immutable": "local_live_pilot_events",
}
IMMUTABLE_TRIGGER_FUNCTIONS = {
    "local_live_pilot_event_immutable": (
        "reject_local_live_pilot_event_mutation",
        "BEGIN RAISE EXCEPTION 'LOCAL LIVE PILOT EVENTS ARE APPEND-ONLY'; END;",
    ),
}
# Canonical prosrc from PostgreSQL 17.11, whitespace normalized, case preserved.
EMERGENCY_CLOSE_TRIGGER = "binance_emergency_close_claim_fence"
EMERGENCY_CLOSE_TRIGGER_BODY_SHA256 = (
    "0b1c8f09c4e2ddc006354e158b078dc5f08d8845dc9df526104cca1ae70fe3a9"
)
EMERGENCY_CLOSE_OWNER_FK_SHA256 = (
    "bd945731bf389afbccb619e6ce426aeaed9e49734ff2856f30a1dbfffcffb46e"
)
CAST_SUFFIX = re.compile(r"::\s*(?:character\s+varying|[a-z_][a-z0-9_]*)(?:\[\])?", re.IGNORECASE)
SQL_TOKEN = re.compile(r"'(?:''|[^'])*'|!~\*|~\*|!~|>=|<=|<>|!=|>|<|~|=|[a-z_][a-z0-9_]*|[0-9]+", re.IGNORECASE)


def discover_migrations(directory: Path = MIGRATIONS_DIR) -> list[Path]:
    migrations = sorted(
        path for path in directory.glob("*.sql") if MIGRATION_NAME.fullmatch(path.name)
    )
    if not migrations:
        raise RuntimeError("No numbered local Postgres migrations were found")
    versions = [path.name[:3] for path in migrations]
    if len(versions) != len(set(versions)):
        raise RuntimeError("Local Postgres migration version numbers are duplicated")
    pilot_migration = next(
        (path for path in migrations if path.name == REQUIRED_LOCAL_PILOT_MIGRATION),
        None,
    )
    if pilot_migration is None:
        raise RuntimeError("Required Local live pilot migration 017 is missing")
    pilot_sql = pilot_migration.read_text(encoding="utf-8")
    if any(constraint_name not in pilot_sql for constraint_name in REQUIRED_LOCAL_PILOT_CONSTRAINTS):
        raise RuntimeError(
            "Local live pilot migration 017 is missing a required binding or event constraint"
        )
    return migrations


def migration_checksum(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def expression_tokens(expression: str) -> tuple[str, ...]:
    without_casts = CAST_SUFFIX.sub("", expression)
    # Lower-case SQL identifiers/enum literals, never a regex literal: the
    # character class [A-Za-z] is not equivalent to [a-z] for a CHECK.
    return tuple(
        token if token.startswith("'^") else token.lower()
        for token in SQL_TOKEN.findall(without_casts)
    )


def constraint_definition_is_safe(name: str, definition: str) -> bool:
    """Require an exact pinned PostgreSQL CHECK definition for every required constraint."""
    expected_hash = REQUIRED_CONSTRAINT_DEFINITION_SHA256.get(name)
    if expected_hash is None:
        return False
    normalized = " ".join(str(definition).split())
    actual_hash = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
    return actual_hash == expected_hash


def decode_postgres_internal_char(value: Any) -> str:
    if isinstance(value, bytes):
        return value.decode("ascii", errors="strict")
    return str(value)


def immutable_history_triggers_are_safe(rows: list[Any]) -> bool:
    """Require row-level BEFORE UPDATE/DELETE triggers on immutable evidence."""
    by_name = {row["trigger_name"]: row for row in rows}
    if set(by_name) != set(REQUIRED_IMMUTABLE_TRIGGERS):
        return False
    for name, table_name in REQUIRED_IMMUTABLE_TRIGGERS.items():
        row = by_name[name]
        function_name, expected_function_body = IMMUTABLE_TRIGGER_FUNCTIONS.get(
            name,
            (
                "reject_binance_history_immutable_mutation",
                "BEGIN RAISE EXCEPTION 'BINANCE HISTORY EVIDENCE IS IMMUTABLE'; END;",
            ),
        )
        function_body = " ".join(str(row.get("function_source", "")).split()).upper()
        try:
            trigger_type = int(row.get("trigger_type", -1))
        except (TypeError, ValueError):
            trigger_type = -1
        if (
            row.get("table_name") != table_name
            or decode_postgres_internal_char(row.get("enabled", "")) != "O"
            # ROW + BEFORE + DELETE + UPDATE; no INSERT or TRUNCATE.
            or trigger_type != 27
            or row.get("function_name") != function_name
            or row.get("function_language") != "plpgsql"
            or function_body != expected_function_body
        ):
            return False
    return True


def emergency_close_claim_trigger_is_safe(rows: list[Any]) -> bool:
    if len(rows) != 1:
        return False
    row = rows[0]
    body = " ".join(str(row.get("function_source", "")).split())
    return (
        row.get("trigger_name") == EMERGENCY_CLOSE_TRIGGER
        and row.get("table_name") == "binance_emergency_close_claims"
        and decode_postgres_internal_char(row.get("enabled", "")) == "O"
        and row.get("trigger_type") == 27
        and row.get("trigger_condition") is None
        and row.get("trigger_arguments") == 0
        and row.get("function_name") == "fence_binance_emergency_close_claim"
        and row.get("function_schema") == "public"
        and row.get("function_language") == "plpgsql"
        and hashlib.sha256(body.encode("utf-8")).hexdigest() == EMERGENCY_CLOSE_TRIGGER_BODY_SHA256
    )


def index_predicate_is_safe(predicate: str | None, policy: Any) -> bool:
    if policy is None:
        return predicate is None
    if predicate is None:
        return False
    tokens = expression_tokens(predicate)
    if policy == "NOT_NULL":
        return tokens == ("continuation_approval_id", "is", "not", "null")
    if isinstance(policy, str) and policy.startswith("NOT_NULL:"):
        column = policy.partition(":")[2]
        return bool(re.fullmatch(r"[a-z_][a-z0-9_]*", column)) and tokens == (
            column,
            "is",
            "not",
            "null",
        )
    if policy == "NOT_CLOSED":
        return tokens == ("state", "<>", "'closed'")
    values = {token.strip("'").upper() for token in tokens if token.startswith("'")}
    identifiers = {
        token
        for token in tokens
        if token not in {"=", "any", "array"} and not token.startswith("'")
    }
    return (
        values == policy
        and len(values) == sum(token.startswith("'") for token in tokens)
        and identifiers == {"state"}
        and "=" in tokens
        and "any" in tokens
        and "array" in tokens
    )


def validate_migration_ledger(rows: list[Any], migrations: list[Path]) -> dict[str, str]:
    row_versions = [row["version"] for row in rows]
    if len(row_versions) != len(set(row_versions)):
        raise RuntimeError("Local migration ledger contains duplicate versions")
    if row_versions != sorted(row_versions):
        raise RuntimeError("Local migration ledger is out of order")

    versions = [migration.name for migration in migrations]
    unknown_versions = sorted(set(row_versions) - set(versions))
    if unknown_versions:
        raise RuntimeError("Local migration ledger contains unknown versions")
    if row_versions != versions[: len(row_versions)]:
        raise RuntimeError("Local migration ledger is incomplete or out of order")

    recorded = {row["version"]: row["checksum"].strip() for row in rows}
    for migration in migrations[: len(row_versions)]:
        if recorded[migration.name] != migration_checksum(migration):
            raise RuntimeError(
                f"Applied migration {migration.name} changed; refusing to rerun it"
            )
    return recorded


def populated_data_upgrade_is_safe(
    recorded: dict[str, str], pending: list[Path], migrations: list[Path], ledger_exists: bool
) -> bool:
    """Allow only known additive Local identity migrations on a verified ledger."""
    if not ledger_exists or len(recorded) < 6 or not pending:
        return False
    migration_names = [migration.name for migration in migrations]
    recorded_names = list(recorded)
    if recorded_names != migration_names[: len(recorded_names)]:
        return False
    if [migration.name for migration in pending] != migration_names[len(recorded_names) :]:
        return False
    return all(
        migration.name in SAFE_POPULATED_UPGRADES
        and migration_checksum(migration) == SAFE_POPULATED_UPGRADES[migration.name]
        for migration in pending
    )


async def verify_schema(connection: Any) -> None:
    if not REQUIRED_LOCAL_PILOT_CONSTRAINTS <= set(REQUIRED_CONSTRAINTS):
        raise RuntimeError("Local schema verifier omits required Local live pilot constraints")
    rows = await connection.fetch(
        """
        SELECT table_name, column_name, is_nullable
        FROM information_schema.columns
        WHERE table_schema = 'public'
          AND table_name = ANY($1::text[])
        """,
        list(REQUIRED_COLUMNS),
    )
    actual: dict[str, set[str]] = {}
    for row in rows:
        actual.setdefault(row["table_name"], set()).add(row["column_name"])
    missing = sorted(
        f"{table}.{column}"
        for table, columns in REQUIRED_COLUMNS.items()
        for column in columns - actual.get(table, set())
    )
    if missing:
        raise RuntimeError("Required local Postgres columns are missing: " + ", ".join(missing))
    actual_nullability = {
        (row["table_name"], row["column_name"]): row.get("is_nullable") for row in rows
    }
    invalid_nullability = sorted(
        f"{table}.{column}"
        for table, columns in REQUIRED_COLUMN_NULLABILITY.items()
        for column, nullable in columns.items()
        if actual_nullability.get((table, column)) != nullable
    )
    if invalid_nullability:
        raise RuntimeError("Required local Postgres column nullability is unsafe: "
                           + ", ".join(invalid_nullability))

    key_tables = sorted({table for table, _, _ in REQUIRED_UNIQUE_KEYS})
    index_rows = await connection.fetch(
        """
        SELECT index_class.relname AS indexname,
               table_class.relname AS table_name,
               index_meta.indisunique AS is_unique,
               index_meta.indisprimary AS is_primary,
               index_meta.indisvalid AS is_valid,
               index_meta.indisready AS is_ready,
               pg_get_expr(index_meta.indpred, index_meta.indrelid) AS predicate,
               ARRAY(
                   SELECT attribute.attname
                   FROM unnest(index_meta.indkey::smallint[]) WITH ORDINALITY
                       AS index_key(attnum, ordinality)
                   JOIN pg_attribute AS attribute
                     ON attribute.attrelid = index_meta.indrelid
                    AND attribute.attnum = index_key.attnum
                   WHERE index_key.ordinality <= index_meta.indnkeyatts
                   ORDER BY index_key.ordinality
               ) AS key_columns
        FROM pg_index AS index_meta
        JOIN pg_class AS index_class ON index_class.oid = index_meta.indexrelid
        JOIN pg_namespace AS index_namespace ON index_namespace.oid = index_class.relnamespace
        JOIN pg_class AS table_class ON table_class.oid = index_meta.indrelid
        WHERE index_namespace.nspname = 'public'
          AND (index_class.relname = ANY($1::text[])
               OR table_class.relname = ANY($2::text[]))
        """,
        list(REQUIRED_INDEXES),
        key_tables,
    )
    actual_indexes = {row["indexname"]: row for row in index_rows}
    invalid_indexes = []
    for name, (table_name, key_columns, unique, predicate_policy) in REQUIRED_INDEXES.items():
        row = actual_indexes.get(name)
        if (
            row is None
            or row["table_name"] != table_name
            or tuple(row["key_columns"]) != key_columns
            or row["is_unique"] is not unique
            or row["is_primary"]
            or not row["is_valid"]
            or not row["is_ready"]
        ):
            invalid_indexes.append(name)
            continue

        if not index_predicate_is_safe(row["predicate"], predicate_policy):
            invalid_indexes.append(name)

    for table_name, key_columns, primary in REQUIRED_UNIQUE_KEYS:
        if not any(
            row["table_name"] == table_name
            and tuple(row["key_columns"]) == key_columns
            and row["is_unique"]
            and row["is_primary"] is primary
            and row["is_valid"]
            and row["is_ready"]
            and (table_name != "binance_emergency_close_claims" or row["predicate"] is None)
            for row in index_rows
        ):
            invalid_indexes.append(f"{table_name}({', '.join(key_columns)}) key")
    if invalid_indexes:
        raise RuntimeError(
            "Required local Postgres indexes/keys are missing or unsafe: "
            + ", ".join(sorted(set(invalid_indexes)))
        )

    constraint_rows = await connection.fetch(
        """
        SELECT conname, conrelid::regclass::text AS table_name,
               contype, convalidated, pg_get_constraintdef(constraint_row.oid) AS definition,
               ARRAY(
                   SELECT attribute.attname
                   FROM unnest(constraint_row.conkey) WITH ORDINALITY
                       AS constraint_key(attnum, ordinality)
                   JOIN pg_attribute AS attribute
                     ON attribute.attrelid = constraint_row.conrelid
                    AND attribute.attnum = constraint_key.attnum
                   ORDER BY constraint_key.ordinality
               ) AS columns
        FROM pg_constraint AS constraint_row
        WHERE constraint_row.connamespace = 'public'::regnamespace
          AND constraint_row.conname = ANY($1::text[])
        """,
        list(REQUIRED_CONSTRAINTS),
    )
    actual_constraints = {row["conname"]: row for row in constraint_rows}
    invalid_constraints = []
    for name, (table_name, expected_columns, _) in REQUIRED_CONSTRAINTS.items():
        row = actual_constraints.get(name)
        constraint_type = (
            decode_postgres_internal_char(row["contype"]) if row is not None else ""
        )
        if (
            row is None
            or row["table_name"].split(".")[-1] != table_name
            or constraint_type != "c"
            or not row["convalidated"]
            or frozenset(row["columns"]) != expected_columns
            or not constraint_definition_is_safe(name, row["definition"])
        ):
            invalid_constraints.append(name)
    if invalid_constraints:
        raise RuntimeError(
            "Required local Postgres constraints are missing or unsafe: "
            + ", ".join(sorted(invalid_constraints))
        )

    foreign_key_rows = await connection.fetch(
        """
        SELECT constraint_row.conname, constraint_row.conrelid::regclass::text AS table_name,
               constraint_row.contype, constraint_row.convalidated,
               pg_get_constraintdef(constraint_row.oid) AS definition,
               constraint_row.confrelid::regclass::text AS referenced_table,
               ARRAY(
                   SELECT attribute.attname
                   FROM unnest(constraint_row.conkey) WITH ORDINALITY
                       AS constraint_key(attnum, ordinality)
                   JOIN pg_attribute AS attribute
                     ON attribute.attrelid = constraint_row.conrelid
                    AND attribute.attnum = constraint_key.attnum
                   ORDER BY constraint_key.ordinality
               ) AS columns,
               ARRAY(
                   SELECT referenced_attribute.attname
                   FROM unnest(constraint_row.confkey) WITH ORDINALITY
                       AS referenced_key(attnum, ordinality)
                   JOIN pg_attribute AS referenced_attribute
                     ON referenced_attribute.attrelid = constraint_row.confrelid
                    AND referenced_attribute.attnum = referenced_key.attnum
                   ORDER BY referenced_key.ordinality
               ) AS referenced_columns
        FROM pg_constraint AS constraint_row
        WHERE constraint_row.connamespace = 'public'::regnamespace
          AND constraint_row.conname = ANY($1::text[])
        """,
        list(REQUIRED_FOREIGN_KEYS),
    )
    actual_foreign_keys = {row["conname"]: row for row in foreign_key_rows}
    invalid_foreign_keys = []
    for name, (table_name, columns, referenced_table, referenced_columns) in REQUIRED_FOREIGN_KEYS.items():
        row = actual_foreign_keys.get(name)
        if (
            row is None
            or row["table_name"].split(".")[-1] != table_name
            or decode_postgres_internal_char(row["contype"]) != "f"
            or not row["convalidated"]
            or tuple(row["columns"]) != columns
            or row["referenced_table"].split(".")[-1] != referenced_table
            or tuple(row["referenced_columns"]) != referenced_columns
            or (name == "binance_emergency_close_claim_owner_fk" and hashlib.sha256(
                " ".join(str(row.get("definition", "")).split()).encode("utf-8")
            ).hexdigest() != EMERGENCY_CLOSE_OWNER_FK_SHA256)
        ):
            invalid_foreign_keys.append(name)
    if invalid_foreign_keys:
        raise RuntimeError(
            "Required local Postgres foreign keys are missing or unsafe: "
            + ", ".join(sorted(invalid_foreign_keys))
        )

    trigger_rows = await connection.fetch(
        """
        SELECT trigger_row.tgname AS trigger_name,
               relation.relname AS table_name,
               trigger_row.tgenabled AS enabled,
               trigger_row.tgtype AS trigger_type,
               pg_get_expr(trigger_row.tgqual, trigger_row.tgrelid) AS trigger_condition,
               trigger_row.tgnargs AS trigger_arguments,
               procedure.proname AS function_name,
               procedure_namespace.nspname AS function_schema,
               language.lanname AS function_language,
               procedure.prosrc AS function_source
        FROM pg_trigger AS trigger_row
        JOIN pg_class AS relation ON relation.oid = trigger_row.tgrelid
        JOIN pg_namespace AS relation_namespace ON relation_namespace.oid = relation.relnamespace
        JOIN pg_proc AS procedure ON procedure.oid = trigger_row.tgfoid
        JOIN pg_namespace AS procedure_namespace ON procedure_namespace.oid = procedure.pronamespace
        JOIN pg_language AS language ON language.oid = procedure.prolang
        WHERE relation_namespace.nspname = 'public'
          AND NOT trigger_row.tgisinternal
          AND trigger_row.tgname = ANY($1::text[])
        """,
        [*REQUIRED_IMMUTABLE_TRIGGERS, EMERGENCY_CLOSE_TRIGGER],
    )
    if not immutable_history_triggers_are_safe(
        [row for row in trigger_rows if row["trigger_name"] != EMERGENCY_CLOSE_TRIGGER]
    ):
        raise RuntimeError("Required immutable Binance history triggers are missing or unsafe")
    if not emergency_close_claim_trigger_is_safe(
        [row for row in trigger_rows if row["trigger_name"] == EMERGENCY_CLOSE_TRIGGER]
    ):
        raise RuntimeError("Required emergency close claim fencing trigger is missing or unsafe")


async def has_existing_migrated_data(connection: Any) -> bool:
    rows = await connection.fetch(
        """
        SELECT table_name
        FROM information_schema.tables
        WHERE table_schema = 'public'
          AND table_type = 'BASE TABLE'
          AND table_name = ANY($1::text[])
        """,
        list(MIGRATED_DATA_TABLES),
    )
    for row in rows:
        table_name = row["table_name"]
        # table_name comes from a fixed allowlist and the system catalog.
        if table_name not in MIGRATED_DATA_TABLES:
            raise RuntimeError("Unexpected local migration table returned by Postgres")
        has_rows = await connection.fetchval(
            f'SELECT EXISTS (SELECT 1 FROM public."{table_name}" LIMIT 1)'
        )
        if has_rows:
            return True
    return False


async def apply_migrations(connection: Any, directory: Path = MIGRATIONS_DIR) -> list[str]:
    migrations = discover_migrations(directory)
    search_path = await connection.fetchval("SELECT current_setting('search_path')")
    if search_path != "public":
        raise RuntimeError("Local migration search_path must resolve exclusively to public")
    await connection.fetchval("SELECT pg_advisory_lock($1)", MIGRATION_LOCK_KEY)
    applied: list[str] = []
    try:
        ledger_exists = await connection.fetchval(
            "SELECT to_regclass('public.local_schema_migrations') IS NOT NULL"
        )
        recorded_rows = (
            await connection.fetch(
                "SELECT version, checksum FROM public.local_schema_migrations ORDER BY version"
            )
            if ledger_exists
            else []
        )
        recorded = validate_migration_ledger(recorded_rows, migrations)
        pending = migrations[len(recorded) :]
        has_data = pending and await has_existing_migrated_data(connection)
        if has_data and not populated_data_upgrade_is_safe(
            recorded, pending, migrations, bool(ledger_exists)
        ):
            raise RuntimeError(
                "Existing local trading data found while migrations are pending; "
                "only checksum-pinned additive migrations 007-019 are allowed. Back up and reconcile it first."
            )

        await connection.execute(
            """
            CREATE TABLE IF NOT EXISTS public.local_schema_migrations (
                version VARCHAR(128) PRIMARY KEY,
                checksum CHAR(64) NOT NULL,
                applied_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        for migration in pending:
            version = migration.name
            checksum = migration_checksum(migration)
            async with connection.transaction():
                await connection.execute(migration.read_text(encoding="utf-8"))
                await connection.execute(
                    """
                    INSERT INTO public.local_schema_migrations (version, checksum)
                    VALUES ($1, $2)
                    """,
                    version,
                    checksum,
                )
            applied.append(version)
        await verify_schema(connection)
        recorded_versions = await connection.fetch(
            "SELECT version FROM public.local_schema_migrations ORDER BY version"
        )
        if [row["version"] for row in recorded_versions] != [
            path.name for path in migrations
        ]:
            raise RuntimeError("Local Postgres migration ledger does not match repository")
        return applied
    finally:
        await connection.fetchval("SELECT pg_advisory_unlock($1)", MIGRATION_LOCK_KEY)


async def run() -> list[str]:
    host = os.getenv("POSTGRES_HOST", "")
    if host != "127.0.0.1":
        raise RuntimeError("Local migration target must be 127.0.0.1")
    try:
        port = int(os.getenv("POSTGRES_PORT", ""))
    except ValueError as exc:
        raise RuntimeError("Local Postgres port is invalid") from exc
    if port != 5433:
        raise RuntimeError("Local migration target must use port 5433")

    names = ("POSTGRES_DB", "POSTGRES_USER", "POSTGRES_PASSWORD")
    missing = [name for name in names if not os.getenv(name, "").strip()]
    if missing:
        raise RuntimeError("Missing local Postgres settings: " + ", ".join(missing))

    connection = await asyncpg.connect(
        host=host,
        port=port,
        database=os.environ["POSTGRES_DB"],
        user=os.environ["POSTGRES_USER"],
        password=os.environ["POSTGRES_PASSWORD"],
        timeout=10,
        server_settings={
            "application_name": "blessing-local-migrations",
            "search_path": "public",
        },
    )
    try:
        return await apply_migrations(connection)
    finally:
        await connection.close()


if __name__ == "__main__":
    try:
        applied = asyncio.run(run())
        if applied:
            for version in applied:
                print(f"Applied local Postgres migration {version}")
        else:
            print("Local Postgres migration ledger is current")
    except Exception as error:  # noqa: BLE001 - suppress unknown driver details that may contain connection context.
        if isinstance(error, RuntimeError):
            # These messages are authored above from fixed schema/guard checks,
            # never from credentials, query parameters, or database row values.
            print(
                f"Local Postgres migration failed (RuntimeError: {error}); Worker was not started",
                file=sys.stderr,
            )
        else:
            print(
                f"Local Postgres migration failed ({type(error).__name__}); Worker was not started",
                file=sys.stderr,
            )
        raise SystemExit(1)
