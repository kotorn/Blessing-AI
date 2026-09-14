"""CLI for producing a verified, research-only public-data replay artifact.

The command consumes already downloaded Binance public archives.  It does not
download data, read API credentials, contact an exchange, or submit orders.
"""

from __future__ import annotations

import argparse
import json
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

from .evidence_artifact import (
    build_exchange_info_source,
    build_manifest,
    build_replay_evidence_artifact,
    load_vision_replay_inputs,
    parse_replay_symbol_rules_from_exchange_info,
    write_replay_evidence_artifact,
)
from .replay import ReplayExecutionConfig


def _parse_utc_datetime(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("timestamp must be ISO-8601") from exc
    if parsed.tzinfo is None:
        raise argparse.ArgumentTypeError("timestamp must include a timezone")
    return parsed.astimezone(UTC)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Build a verified research-only Binance public-data replay artifact"
    )
    parser.add_argument("--symbol", required=True)
    parser.add_argument(
        "--venue", choices=("BINANCE_MAINNET", "BINANCE_TESTNET"), required=True
    )
    parser.add_argument("--dataset-id", required=True)
    parser.add_argument("--build-sha", required=True)
    parser.add_argument("--config-json", type=Path, required=True)
    parser.add_argument("--exchange-info-json", type=Path, required=True)
    parser.add_argument("--exchange-info-url", required=True)
    parser.add_argument("--start-time", type=_parse_utc_datetime, required=True)
    parser.add_argument("--end-time", type=_parse_utc_datetime, required=True)
    parser.add_argument("--kline-archive", type=Path, required=True)
    parser.add_argument("--kline-checksum", type=Path, required=True)
    parser.add_argument("--kline-url", required=True)
    parser.add_argument("--mark-price-archive", type=Path, required=True)
    parser.add_argument("--mark-price-checksum", type=Path, required=True)
    parser.add_argument("--mark-price-url", required=True)
    parser.add_argument("--book-ticker-archive", type=Path, required=True)
    parser.add_argument("--book-ticker-checksum", type=Path, required=True)
    parser.add_argument("--book-ticker-url", required=True)
    parser.add_argument("--funding-json", type=Path, required=True)
    parser.add_argument("--funding-url", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--max-book-age-sec", type=float, default=60.0)
    parser.add_argument("--funding-tolerance-sec", type=float, default=60.0)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    config_payload = json.loads(args.config_json.read_text(encoding="utf-8"))
    config = ReplayExecutionConfig.model_validate(config_payload)
    exchange_info_payload = json.loads(
        args.exchange_info_json.read_text(encoding="utf-8")
    )
    exchange_rules = parse_replay_symbol_rules_from_exchange_info(
        exchange_info_payload, args.symbol
    )
    if config.rules_for(args.symbol) != exchange_rules:
        raise ValueError("config symbol rules do not match captured exchangeInfo")
    events, source_records = load_vision_replay_inputs(
        symbol=args.symbol,
        venue=args.venue,
        kline_archive=args.kline_archive,
        mark_price_archive=args.mark_price_archive,
        book_ticker_archive=args.book_ticker_archive,
        funding_json=args.funding_json,
        kline_url=args.kline_url,
        mark_price_url=args.mark_price_url,
        book_ticker_url=args.book_ticker_url,
        funding_url=args.funding_url,
        kline_checksum=args.kline_checksum,
        mark_price_checksum=args.mark_price_checksum,
        book_ticker_checksum=args.book_ticker_checksum,
        max_book_age_sec=args.max_book_age_sec,
        funding_tolerance_sec=args.funding_tolerance_sec,
        start_time=args.start_time,
        end_time=args.end_time,
    )
    exchange_info_source = build_exchange_info_source(
        args.exchange_info_json,
        url=args.exchange_info_url,
        start_time=events[0].event_time,
        end_time=events[-1].event_time,
    )
    manifest = build_manifest(
        dataset_id=args.dataset_id,
        symbol=args.symbol,
        venue=args.venue,
        code_sha=args.build_sha,
        source_records=source_records,
        exchange_info_source=exchange_info_source,
        cost_model=config.cost_model,
        symbol_rules=config.symbol_rules,
    )
    artifact = build_replay_evidence_artifact(events, config, manifest=manifest)
    artifact_sha256 = write_replay_evidence_artifact(artifact, args.output)
    print(
        json.dumps(
            {
                "artifact": str(args.output),
                "artifact_sha256": artifact_sha256,
                "dataset_sha256": artifact.manifest.dataset_sha256,
                "event_count": artifact.manifest.event_count,
                "net_pnl": (
                    str(artifact.replay.economic_result.net_pnl)
                    if artifact.replay.economic_result is not None
                    else None
                ),
                "status": artifact.verification_status,
                "evidence_status": artifact.evidence_status,
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI entry point
    raise SystemExit(main())
