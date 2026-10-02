from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace
from zipfile import ZipFile

import pytest

from apps.trading_worker.backtest.economic import BacktestTrade, EconomicCostModel
from apps.trading_worker.backtest.event_dataset import (
    historical_events_sha256,
    write_historical_events_jsonl,
)
from apps.trading_worker.backtest.local_promotion_verifier import (
    TRUSTED_COST_POLICY_SHA256,
    CohortReplay,
    EvidenceNotRun,
    EvidenceRejected,
    _closed_baskets,
    _maximum_drawdown_pct,
    _metrics,
    _oos_event_max_drawdown_pct,
    _read_verified_binance_public_url,
    _replay_shadow,
    _rule0_violations,
    _shadow_decision_trace_sha256,
    _validate_shadow_capture,
    _validate_trusted_cost_policy,
    _validate_vision_archive_identity,
    _verify_cohort,
    _verify_oos_raw_sources,
    verify_promotion_bundle,
)
from apps.trading_worker.backtest.replay import (
    HistoricalMarketEvent,
    ReplayExecutionConfig,
    ReplaySymbolRules,
    run_replay,
)
from domain.enums import EconomicRiskClass, MarketType, OrderSide, RiskState

GIT_SHA = "a" * 40
SOURCE_FINGERPRINT = "b" * 64
START = datetime(2026, 1, 1, tzinfo=UTC)


def _trade(trade_id: str, closed_at: datetime, gross_pnl: str) -> BacktestTrade:
    return BacktestTrade(
        trade_id=trade_id,
        timestamp=closed_at,
        symbol="ETHUSDC",
        strategy_id="test-only",
        data_source="BINANCE_PUBLIC_MAINNET_READ_ONLY",
        gross_pnl=Decimal(gross_pnl),
        funding_pnl=Decimal(0),
        entry_notional=Decimal(100),
        exit_notional=Decimal(100),
        entry_spread_bps=Decimal(0),
        exit_spread_bps=Decimal(0),
        entry_slippage_bps=Decimal(0),
        exit_slippage_bps=Decimal(0),
    )


def test_missing_bundle_is_not_run(tmp_path):
    result = verify_promotion_bundle(
        tmp_path,
        "evidence/local-mainnet-promotion.json",
        GIT_SHA,
        SOURCE_FINGERPRINT,
    )

    assert result["status"] == "NOT_RUN"
    assert result["passed"] is False
    assert result["totalClosedBaskets"] == 0


def test_claimed_basket_rows_and_legacy_replay_artifact_are_never_counted(tmp_path):
    bundle = {
        "schemaVersion": 1,
        "generatedAt": datetime.now(UTC).isoformat(),
        "sourceGitSha": GIT_SHA,
        "sourceFingerprint": SOURCE_FINGERPRINT,
        "cohorts": {
            name: {
                "cohort": name,
                "closedBaskets": [{"basketId": f"caller-{i}"} for i in range(25)],
                "metrics": {"maxDrawdownPct": 0, "sharpe": 99, "winRatePct": 100},
            }
            for name in ("OOS", "SHADOW")
        },
    }
    evidence = tmp_path / "claimed.json"
    evidence.write_text(json.dumps(bundle), encoding="utf-8")
    result = verify_promotion_bundle(tmp_path, "claimed.json", GIT_SHA, SOURCE_FINGERPRINT)

    assert result["status"] == "FAIL"
    assert result["totalClosedBaskets"] == 0
    assert any("schemaVersion must be 2" in item for item in result["failures"])


def test_repository_btc_testnet_three_event_artifact_is_not_promotion_evidence():
    repository = Path(__file__).resolve().parents[2]
    artifact = repository / "docs" / "research" / "evidence_artifact_btcusdt.json"
    if not artifact.exists():
        pytest.skip("the reviewed repository research artifact is not present")
    result = verify_promotion_bundle(
        repository,
        "docs/research/evidence_artifact_btcusdt.json",
        GIT_SHA,
        SOURCE_FINGERPRINT,
    )

    assert result["status"] == "FAIL"
    assert result["passed"] is False
    assert result["totalClosedBaskets"] == 0
    assert result["failures"]


def test_oos_requires_raw_source_files_before_manifest_hashes_can_promote(tmp_path):
    manifest = SimpleNamespace(
        source_records=(),
        exchange_info_source=SimpleNamespace(source_type="EXCHANGE_INFO"),
    )
    with pytest.raises(EvidenceNotRun, match="raw Binance source files are missing"):
        _verify_oos_raw_sources(tmp_path, None, manifest, ())


def test_oos_source_verifier_rejects_non_binance_hosts_without_network_access():
    with pytest.raises(EvidenceRejected, match="fixed Binance public HTTPS allowlist"):
        _read_verified_binance_public_url(
            "https://example.invalid/data.zip.CHECKSUM",
            expected_host="data.binance.vision",
            max_bytes=1024,
        )


def test_reopened_dataset_fingerprint_mismatch_fails_before_replay(tmp_path):
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    files = {
        "oos.jsonl": b"not-a-dataset\n",
        "oos-config.json": b"{}",
        "oos-replay.json": b"{}",
        "shadow.jsonl": b"not-a-dataset\n",
        "shadow-config.json": b"{}",
        "shadow-replay.json": b"{}",
    }
    for name, content in files.items():
        (evidence / name).write_bytes(content)
    cohorts = {}
    for name in ("OOS", "SHADOW"):
        lower = name.lower()
        cohorts[name] = {
            "cohort": name,
            "provenance": {
                "generator": "BLESSING_BACKTEST" if name == "OOS" else "BLESSING_SHADOW_RUNTIME",
                "sourceGitSha": GIT_SHA,
                "sourceFingerprint": SOURCE_FINGERPRINT,
                "datasetPath": f"evidence/{lower}.jsonl",
                "datasetSha256": "0" * 64,
                "configPath": f"evidence/{lower}-config.json",
                "configSha256": sha256(files[f"{lower}-config.json"]).hexdigest(),
                "replayArtifactPath": f"evidence/{lower}-replay.json",
                "replayArtifactSha256": sha256(files[f"{lower}-replay.json"]).hexdigest(),
                **({"sourceFiles": {}} if name == "OOS" else {}),
                **(
                    {}
                    if name == "OOS"
                    else {
                        "runtimeCapturePath": "evidence/shadow-capture.json",
                        "runtimeCaptureSha256": "0" * 64,
                    }
                ),
            },
        }
    bundle = {
        "schemaVersion": 2,
        "generatedAt": datetime.now(UTC).isoformat(),
        "sourceGitSha": GIT_SHA,
        "sourceFingerprint": SOURCE_FINGERPRINT,
        "cohorts": cohorts,
    }
    (evidence / "bundle.json").write_text(json.dumps(bundle), encoding="utf-8")

    result = verify_promotion_bundle(tmp_path, "evidence/bundle.json", GIT_SHA, SOURCE_FINGERPRINT)

    assert result["status"] == "NOT_RUN"
    assert result["cohorts"]["OOS"]["status"] == "FAIL"
    assert result["totalClosedBaskets"] == 0
    assert any(
        "datasetSha256 does not match reopened evidence bytes" in item
        for item in result["failures"]
    )


def test_partial_close_trades_are_recomputed_and_grouped_as_one_basket():
    close_one = START + timedelta(minutes=1)
    close_two = START + timedelta(minutes=2)
    fills = (
        SimpleNamespace(
            quantity=Decimal(2),
            side=OrderSide.BUY,
            symbol="ETHUSDC",
            exchange_trade_id="open",
            event_time=START,
            realized_pnl=Decimal(0),
        ),
        SimpleNamespace(
            quantity=Decimal(1),
            side=OrderSide.SELL,
            symbol="ETHUSDC",
            exchange_trade_id="reduce-1",
            event_time=close_one,
            realized_pnl=Decimal(1),
        ),
        SimpleNamespace(
            quantity=Decimal(1),
            side=OrderSide.SELL,
            symbol="ETHUSDC",
            exchange_trade_id="close",
            event_time=close_two,
            realized_pnl=Decimal(2),
        ),
    )
    trades = (
        _trade("partial-1", close_one, "1"),
        _trade("partial-2", close_two, "2"),
    )
    result = SimpleNamespace(fills=fills, trades=trades)
    cost_model = EconomicCostModel(maker_fee_rate=Decimal(0), taker_fee_rate=Decimal("0.001"))

    baskets = _closed_baskets(result, cost_model)

    assert len(baskets) == 1
    assert baskets[0].closed_at == close_two
    assert baskets[0].net_pnl == Decimal("2.6")
    metrics, failures = _metrics(
        CohortReplay(
            initial_capital=Decimal(250),
            replay_hash="c" * 64,
            baskets=baskets,
            rule0_violations=0,
            event_max_drawdown_pct=Decimal(0),
        )
    )
    assert metrics["closedBaskets"] == 1
    assert "Sharpe is below 1.0 or cannot be calculated" in failures


def test_event_curve_drawdown_captures_intrabar_loss_and_controls_maxdd():
    event_drawdown = _maximum_drawdown_pct(Decimal(250), [Decimal(250), Decimal(270), Decimal(258)])
    assert event_drawdown == Decimal(100) * Decimal(12) / Decimal(270)

    metrics, failures = _metrics(
        CohortReplay(
            initial_capital=Decimal(250),
            replay_hash="d" * 64,
            baskets=(),
            rule0_violations=0,
            event_max_drawdown_pct=event_drawdown,
        )
    )
    assert metrics["eventMaxDrawdownPct"] == float(event_drawdown)
    assert metrics["maxDrawdownPct"] == float(event_drawdown)
    assert "MaxDD including event-level mark-to-market exceeds 3.5%" in failures


def test_sharpe_uses_mark_to_market_daily_equity_and_includes_idle_days():
    metrics, failures = _metrics(
        CohortReplay(
            initial_capital=Decimal(250),
            replay_hash="e" * 64,
            baskets=(
                SimpleNamespace(
                    basket_id="closed-1",
                    closed_at=START + timedelta(days=2),
                    net_pnl=Decimal(25),
                ),
            ),
            rule0_violations=0,
            event_max_drawdown_pct=Decimal(0),
            daily_equity_curve=(
                (START, Decimal(250)),
                (START + timedelta(days=2), Decimal(275)),
            ),
        )
    )
    assert metrics["sharpe"] is not None
    assert metrics["sharpe"] > 0
    assert not any("Sharpe" in item for item in failures)


def test_vision_archive_identity_binds_symbol_interval_period_and_csv_member(tmp_path):
    archive_path = tmp_path / "ETHUSDC-1m-2026-01-02.zip"
    with ZipFile(archive_path, "w") as archive:
        archive.writestr("ETHUSDC-1m-2026-01-02.csv", "row\n")
    record = SimpleNamespace(
        url=(
            "https://data.binance.vision/data/futures/um/daily/klines/"
            "ETHUSDC/1m/ETHUSDC-1m-2026-01-02.zip"
        ),
        start_time=START + timedelta(days=1),
        end_time=START + timedelta(days=2, hours=23),
    )
    _validate_vision_archive_identity(
        "KLINES_1M",
        record,
        archive_path,
        dataset_start=START + timedelta(days=1),
        dataset_end=START + timedelta(days=1, hours=22),
    )
    for invalid_url in (
        record.url.replace("ETHUSDC/1m", "BTCUSDT/1m"),
        record.url.replace("ETHUSDC/1m", "ETHUSDC/5m"),
    ):
        with pytest.raises(EvidenceRejected):
            _validate_vision_archive_identity(
                "KLINES_1M",
                SimpleNamespace(**{**record.__dict__, "url": invalid_url}),
                archive_path,
                dataset_start=START + timedelta(days=1),
                dataset_end=START + timedelta(days=1, hours=22),
            )
    wrong_member = tmp_path / "wrong" / archive_path.name
    wrong_member.parent.mkdir()
    with ZipFile(wrong_member, "w") as archive:
        archive.writestr("different.csv", "row\n")
    wrong_member_record = SimpleNamespace(
        **{
            **record.__dict__,
            "url": record.url,
        }
    )
    with pytest.raises(EvidenceRejected, match="ZIP member"):
        _validate_vision_archive_identity(
            "KLINES_1M",
            wrong_member_record,
            wrong_member,
            dataset_start=START + timedelta(days=1),
            dataset_end=START + timedelta(days=1, hours=22),
        )

    monthly_named = tmp_path / "ETHUSDC-1m-2026-01.zip"
    with ZipFile(monthly_named, "w") as archive:
        archive.writestr("ETHUSDC-1m-2026-01.csv", "row\n")
    monthly_under_daily = SimpleNamespace(
        url=record.url.replace(archive_path.name, monthly_named.name),
        start_time=START + timedelta(days=1),
        end_time=START + timedelta(days=20),
    )
    with pytest.raises(EvidenceRejected, match="cadence"):
        _validate_vision_archive_identity(
            "KLINES_1M",
            monthly_under_daily,
            monthly_named,
            dataset_start=START + timedelta(days=1),
            dataset_end=START + timedelta(days=20),
        )


def test_oos_event_drawdown_aggregates_fold_resets_conservatively():
    folds = [
        SimpleNamespace(
            equity_curve=tuple(SimpleNamespace(equity=value) for value in (250, 275, 270)),
            final_equity=Decimal(270),
        ),
        SimpleNamespace(
            equity_curve=tuple(SimpleNamespace(equity=value) for value in (250, 240, 260)),
            final_equity=Decimal(260),
        ),
    ]

    result = _oos_event_max_drawdown_pct(folds, Decimal(250))

    assert result == Decimal(100) * Decimal(15) / Decimal(275)


def test_unsigned_shadow_capture_stays_not_run_and_contributes_zero(tmp_path):
    bundle = {
        "schemaVersion": 2,
        "generatedAt": datetime.now(UTC).isoformat(),
        "sourceGitSha": GIT_SHA,
        "sourceFingerprint": SOURCE_FINGERPRINT,
        "cohorts": {
            "OOS": {"cohort": "OOS"},
            "SHADOW": {
                "cohort": "SHADOW",
                "closedBaskets": [{"basketId": f"claimed-{index}"} for index in range(50)],
                "metrics": {"maxDrawdownPct": 0, "sharpe": 99, "winRatePct": 100},
                "capture": {"orderSubmissionAttempts": 0},
            },
        },
    }
    (tmp_path / "bundle.json").write_text(json.dumps(bundle), encoding="utf-8")

    result = verify_promotion_bundle(tmp_path, "bundle.json", GIT_SHA, SOURCE_FINGERPRINT)

    assert result["status"] == "FAIL"
    assert result["totalClosedBaskets"] == 0
    assert result["cohorts"]["SHADOW"]["status"] == "FAIL"
    assert any("cannot contain caller-asserted metrics" in item for item in result["failures"])


def _shadow_config() -> ReplayExecutionConfig:
    return ReplayExecutionConfig(
        initial_capital=Decimal(250),
        cost_model=EconomicCostModel(
            maker_fee_rate=Decimal("0.0002"), taker_fee_rate=Decimal("0.0005")
        ),
        market_slippage_bps=Decimal(2),
        funding_interval_sec=3600,
        enabled_strategies=("shock",),
        symbol_rules=(
            ReplaySymbolRules(
                symbol="ETHUSDC",
                tick_size=Decimal("0.01"),
                step_size=Decimal("0.001"),
                min_quantity=Decimal("0.001"),
                min_notional=Decimal(5),
            ),
        ),
    )


def _shadow_events() -> tuple[HistoricalMarketEvent, ...]:
    events = []
    for index, close in enumerate(("2000", "2001", "2000.5")):
        price = Decimal(close)
        events.append(
            HistoricalMarketEvent(
                event_id=f"shadow-{index}",
                event_time=START + timedelta(minutes=index),
                symbol="ETHUSDC",
                venue="BINANCE_MAINNET",
                market_type=MarketType.USDM_FUTURES,
                open=price,
                high=price + Decimal(1),
                low=price - Decimal(1),
                close=price,
                volume=Decimal(100),
                trade_count=5,
                best_bid=price - Decimal("0.1"),
                best_ask=price + Decimal("0.1"),
                bid_qty=Decimal(10),
                ask_qty=Decimal(10),
                mark_price=price,
                funding_rate=Decimal(0) if index == 0 else None,
                funding_event=index == 0,
                data_source="BINANCE_PUBLIC_MAINNET_READ_ONLY",
            )
        )
    return tuple(events)


def test_shadow_replay_runs_strategy_and_returns_deterministic_cohort():
    events = _shadow_events()
    config = _shadow_config()

    first = _replay_shadow(events, config)
    second = _replay_shadow(events, config)

    assert first.replay_hash == second.replay_hash
    assert first.initial_capital == Decimal(250)
    assert first.rule0_violations == 0
    assert first.daily_equity_curve


def test_trusted_cost_floor_rejects_zero_fees_and_zero_slippage():
    config = _shadow_config()
    no_fee = config.model_copy(
        update={
            "cost_model": EconomicCostModel(
                maker_fee_rate=Decimal(0), taker_fee_rate=Decimal(0)
            )
        }
    )
    no_slippage = config.model_copy(update={"market_slippage_bps": Decimal(0)})

    for invalid in (no_fee, no_slippage):
        with pytest.raises(EvidenceRejected, match="trusted .* floor"):
            _validate_trusted_cost_policy((invalid,))
    _validate_trusted_cost_policy((config,))


def test_shadow_capture_binds_zero_order_claim_and_replay_trace():
    events = _shadow_events()
    raw_result = run_replay(events, _shadow_config())
    capture = {
        "schemaVersion": 1,
        "generator": "BLESSING_SHADOW_RUNTIME",
        "captureId": "capture-001",
        "sourceGitSha": GIT_SHA,
        "sourceFingerprint": SOURCE_FINGERPRINT,
        "datasetSha256": historical_events_sha256(events),
        "configSha256": "c" * 64,
        "eventCount": len(events),
        "startTime": events[0].event_time.isoformat(),
        "endTime": events[-1].event_time.isoformat(),
        "orderSubmissionAttempts": 0,
        "orderEndpointCalls": 0,
        "decisionTraceSha256": _shadow_decision_trace_sha256(raw_result),
    }

    trace_hash = _validate_shadow_capture(
        capture,
        expected_git_sha=GIT_SHA,
        expected_source_fingerprint=SOURCE_FINGERPRINT,
        dataset_sha256=historical_events_sha256(events),
        config_sha256="c" * 64,
        events=events,
    )
    assert trace_hash == _shadow_decision_trace_sha256(raw_result)

    capture["orderSubmissionAttempts"] = 1
    with pytest.raises(EvidenceRejected, match="order attempt"):
        _validate_shadow_capture(
            capture,
            expected_git_sha=GIT_SHA,
            expected_source_fingerprint=SOURCE_FINGERPRINT,
            dataset_sha256=historical_events_sha256(events),
            config_sha256="c" * 64,
            events=events,
        )


def test_shadow_cohort_opens_capture_and_replays_instead_of_not_run(tmp_path):
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    events = _shadow_events()
    config = _shadow_config()
    dataset_path = evidence / "shadow.jsonl"
    dataset_sha = write_historical_events_jsonl(events, dataset_path)
    config_payload = {"kind": "SHADOW_REPLAY", "config": config.model_dump(mode="json")}
    config_path = evidence / "shadow-config.json"
    config_raw = json.dumps(config_payload, sort_keys=True, separators=(",", ":")).encode()
    config_path.write_bytes(config_raw)
    raw_result = run_replay(events, config)
    capture_payload = {
        "schemaVersion": 1,
        "generator": "BLESSING_SHADOW_RUNTIME",
        "captureId": "capture-integrated",
        "sourceGitSha": GIT_SHA,
        "sourceFingerprint": SOURCE_FINGERPRINT,
        "datasetSha256": dataset_sha,
        "configSha256": sha256(config_raw).hexdigest(),
        "eventCount": len(events),
        "startTime": events[0].event_time.isoformat(),
        "endTime": events[-1].event_time.isoformat(),
        "orderSubmissionAttempts": 0,
        "orderEndpointCalls": 0,
        "decisionTraceSha256": _shadow_decision_trace_sha256(raw_result),
    }
    capture_path = evidence / "shadow-capture.json"
    capture_raw = json.dumps(capture_payload, sort_keys=True, separators=(",", ":")).encode()
    capture_path.write_bytes(capture_raw)
    replayed = _replay_shadow(events, config)
    artifact_payload = {
        "schemaVersion": 1,
        "generator": "BLESSING_BACKTEST",
        "cohort": "SHADOW",
        "sourceGitSha": GIT_SHA,
        "sourceFingerprint": SOURCE_FINGERPRINT,
        "datasetSha256": dataset_sha,
        "configSha256": sha256(config_raw).hexdigest(),
        "costPolicySha256": TRUSTED_COST_POLICY_SHA256,
        "runtimeCaptureSha256": sha256(capture_raw).hexdigest(),
        "deterministicReplaySha256": replayed.replay_hash,
    }
    replay_path = evidence / "shadow-replay.json"
    replay_raw = json.dumps(artifact_payload, sort_keys=True, separators=(",", ":")).encode()
    replay_path.write_bytes(replay_raw)
    cohort = {
        "cohort": "SHADOW",
        "provenance": {
            "generator": "BLESSING_SHADOW_RUNTIME",
            "sourceGitSha": GIT_SHA,
            "sourceFingerprint": SOURCE_FINGERPRINT,
            "datasetPath": "evidence/shadow.jsonl",
            "datasetSha256": sha256(dataset_path.read_bytes()).hexdigest(),
            "configPath": "evidence/shadow-config.json",
            "configSha256": sha256(config_raw).hexdigest(),
            "replayArtifactPath": "evidence/shadow-replay.json",
            "replayArtifactSha256": sha256(replay_raw).hexdigest(),
            "runtimeCapturePath": "evidence/shadow-capture.json",
            "runtimeCaptureSha256": sha256(capture_raw).hexdigest(),
        },
    }

    metrics, failures = _verify_cohort(
        tmp_path,
        "SHADOW",
        cohort,
        expected_git_sha=GIT_SHA,
        expected_source_fingerprint=SOURCE_FINGERPRINT,
    )

    assert metrics["closedBaskets"] == len(replayed.baskets)
    assert "cohort has no replay-verified closed baskets" in failures

    artifact_payload["costPolicySha256"] = "0" * 64
    replay_raw = json.dumps(artifact_payload, sort_keys=True, separators=(",", ":")).encode()
    replay_path.write_bytes(replay_raw)
    cohort["provenance"]["replayArtifactSha256"] = sha256(replay_raw).hexdigest()
    with pytest.raises(EvidenceRejected, match="trusted cost policy"):
        _verify_cohort(
            tmp_path,
            "SHADOW",
            cohort,
            expected_git_sha=GIT_SHA,
            expected_source_fingerprint=SOURCE_FINGERPRINT,
        )


def test_unknown_risk_snapshot_on_accepted_risk_increase_is_rule_zero_violation():
    timestamp = START
    result = SimpleNamespace(
        risk_snapshots=(
            SimpleNamespace(
                timestamp=timestamp,
                risk_state=RiskState.NO_NEW_RISK,
                hard_violations=[],
            ),
        ),
        execution_decisions=(
            SimpleNamespace(decision_id="decision-1", risk_class=EconomicRiskClass.NEW_RISK),
        ),
        decisions=(
            SimpleNamespace(
                accepted=True,
                exchange_order_id="exchange-1",
                decision_id="decision-1",
                risk_class=EconomicRiskClass.NEW_RISK,
                timestamp=timestamp,
            ),
        ),
        fills=(SimpleNamespace(exchange_order_id="exchange-1"),),
    )

    assert _rule0_violations(result) == 1


def test_rule_zero_is_not_assumed_zero_when_the_snapshot_is_missing():
    result = SimpleNamespace(
        risk_snapshots=(),
        execution_decisions=(
            SimpleNamespace(decision_id="decision-1", risk_class=EconomicRiskClass.NEW_RISK),
        ),
        decisions=(
            SimpleNamespace(
                accepted=True,
                exchange_order_id="exchange-1",
                decision_id="decision-1",
                risk_class=EconomicRiskClass.NEW_RISK,
                timestamp=START,
            ),
        ),
        fills=(SimpleNamespace(exchange_order_id="exchange-1"),),
    )

    with pytest.raises(EvidenceRejected, match="Rule #0 cannot be evaluated"):
        _rule0_violations(result)


def test_flat_to_flat_basket_reconstruction_rejects_unmatched_trade():
    fills = (
        SimpleNamespace(
            quantity=Decimal(1),
            side=OrderSide.BUY,
            symbol="ETHUSDC",
            exchange_trade_id="open",
            event_time=START,
            realized_pnl=Decimal(0),
        ),
        SimpleNamespace(
            quantity=Decimal(1),
            side=OrderSide.SELL,
            symbol="ETHUSDC",
            exchange_trade_id="close",
            event_time=START + timedelta(minutes=1),
            realized_pnl=Decimal(7),
        ),
    )
    result = SimpleNamespace(
        fills=fills,
        trades=(_trade("different-pnl", START + timedelta(minutes=1), "1"),),
    )

    with pytest.raises(EvidenceRejected, match="no unique realized trade record"):
        _closed_baskets(
            result,
            EconomicCostModel(maker_fee_rate=Decimal(0), taker_fee_rate=Decimal(0)),
        )
