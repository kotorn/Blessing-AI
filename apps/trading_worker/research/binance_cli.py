"""Fail-closed, read-only bridge to Binance's official CLI.

The Python Trading Worker remains the only execution authority.  This module
exists for research and contract cross-checks, so it deliberately exposes a
small allowlist of USDⓈ-M read operations and has no generic subprocess or
custom-request escape hatch.  It also refuses to run unless the CLI is pinned
to Binance USDⓈ-M Testnet.

The official CLI is an independently installed Rust binary.  It is therefore
not a Python or npm runtime dependency and is never imported by the worker's
execution path.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any
from urllib.parse import urlparse

TESTNET_FUTURES_BASE_URL = "https://testnet.binancefuture.com"
TESTNET_FUTURES_HOST = "testnet.binancefuture.com"
DEFAULT_TIMEOUT_SEC = 15.0
DEFAULT_SYMBOL = "BTCUSDT"
DEFAULT_RECV_WINDOW = "5000"
DEFAULT_BINARY = "binance-cli"
ALLOWED_BINARY_NAMES = frozenset({"binance-cli", "binance-cli.exe"})


class BinanceCliResearchError(RuntimeError):
    """Base error for the isolated Binance CLI research boundary."""


class BinanceCliPolicyError(BinanceCliResearchError):
    """Raised when a requested operation violates the read-only Testnet policy."""


class ReadOnlyCheck(StrEnum):
    """Supported CLI checks; there is intentionally no mutation variant."""

    SERVER_TIME = "server_time"
    EXCHANGE_INFO = "exchange_info"
    BOOK_TICKER = "book_ticker"
    MARK_PRICE = "mark_price"
    ACCOUNT = "account"
    BALANCE = "balance"
    ACCOUNT_CONFIGURATION = "account_configuration"
    POSITION_MODE = "position_mode"
    POSITIONS = "positions"
    OPEN_ORDERS = "open_orders"
    USER_TRADES = "user_trades"
    ALL_ORDERS = "all_orders"
    QUERY_ORDER = "query_order"


@dataclass(frozen=True)
class _CheckSpec:
    command: tuple[str, ...]
    requires_credentials: bool = False
    requires_symbol: bool = False
    accepts_order_reference: bool = False


_CHECKS: dict[ReadOnlyCheck, _CheckSpec] = {
    ReadOnlyCheck.SERVER_TIME: _CheckSpec(("futures-usds", "check-server-time")),
    ReadOnlyCheck.EXCHANGE_INFO: _CheckSpec(("futures-usds", "exchange-information")),
    ReadOnlyCheck.BOOK_TICKER: _CheckSpec(
        ("futures-usds", "symbol-order-book-ticker"), requires_symbol=True
    ),
    ReadOnlyCheck.MARK_PRICE: _CheckSpec(
        ("futures-usds", "mark-price"), requires_symbol=True
    ),
    ReadOnlyCheck.ACCOUNT: _CheckSpec(
        (
            "futures-usds",
            "account-information-v3",
            "--recv-window",
            DEFAULT_RECV_WINDOW,
        ),
        requires_credentials=True,
    ),
    ReadOnlyCheck.BALANCE: _CheckSpec(
        (
            "futures-usds",
            "futures-account-balance-v3",
            "--recv-window",
            DEFAULT_RECV_WINDOW,
        ),
        requires_credentials=True,
    ),
    ReadOnlyCheck.ACCOUNT_CONFIGURATION: _CheckSpec(
        (
            "futures-usds",
            "futures-account-configuration",
            "--recv-window",
            DEFAULT_RECV_WINDOW,
        ),
        requires_credentials=True,
    ),
    ReadOnlyCheck.POSITION_MODE: _CheckSpec(
        ("futures-usds", "get-current-position-mode", "--recv-window", DEFAULT_RECV_WINDOW),
        requires_credentials=True,
    ),
    ReadOnlyCheck.POSITIONS: _CheckSpec(
        ("futures-usds", "position-information-v3", "--recv-window", DEFAULT_RECV_WINDOW),
        requires_credentials=True,
        requires_symbol=True,
    ),
    ReadOnlyCheck.OPEN_ORDERS: _CheckSpec(
        ("futures-usds", "current-all-open-orders", "--recv-window", DEFAULT_RECV_WINDOW),
        requires_credentials=True,
        requires_symbol=True,
    ),
    ReadOnlyCheck.USER_TRADES: _CheckSpec(
        (
            "futures-usds",
            "account-trade-list",
            "--limit",
            "100",
            "--recv-window",
            DEFAULT_RECV_WINDOW,
        ),
        requires_credentials=True,
        requires_symbol=True,
    ),
    ReadOnlyCheck.ALL_ORDERS: _CheckSpec(
        (
            "futures-usds",
            "all-orders",
            "--limit",
            "100",
            "--recv-window",
            DEFAULT_RECV_WINDOW,
        ),
        requires_credentials=True,
        requires_symbol=True,
    ),
    ReadOnlyCheck.QUERY_ORDER: _CheckSpec(
        ("futures-usds", "query-order", "--recv-window", DEFAULT_RECV_WINDOW),
        requires_credentials=True,
        requires_symbol=True,
        accepts_order_reference=True,
    ),
}

_SYMBOL_PATTERN = re.compile(r"^[A-Z0-9]{2,30}$")
_CLIENT_ORDER_ID_PATTERN = re.compile(r"^[A-Za-z0-9._:-]{1,36}$")
_SENSITIVE_ENV_KEYS = frozenset(
    {
        "BINANCE_API_KEY",
        "BINANCE_SECRET_KEY",
        "BINANCE_TESTNET_API_KEY",
        "BINANCE_TESTNET_API_SECRET",
    }
)


def _as_check(value: ReadOnlyCheck | str) -> ReadOnlyCheck:
    try:
        return value if isinstance(value, ReadOnlyCheck) else ReadOnlyCheck(value)
    except ValueError as exc:
        raise BinanceCliPolicyError(
            f"unsupported Binance CLI check {value!r}; mutation/custom requests are not allowed"
        ) from exc


def _validated_symbol(symbol: str) -> str:
    normalized = symbol.strip().upper()
    if not _SYMBOL_PATTERN.fullmatch(normalized):
        raise BinanceCliPolicyError("symbol must contain only 2-30 alphanumeric characters")
    return normalized


def _validated_client_order_id(client_order_id: str) -> str:
    normalized = client_order_id.strip()
    if not _CLIENT_ORDER_ID_PATTERN.fullmatch(normalized):
        raise BinanceCliPolicyError("client_order_id contains unsupported characters")
    return normalized


def build_read_only_command(
    check: ReadOnlyCheck | str,
    *,
    symbol: str = DEFAULT_SYMBOL,
    order_id: int | None = None,
    client_order_id: str | None = None,
) -> tuple[str, ...]:
    """Build one allowlisted USDⓈ-M command without accepting raw CLI flags."""

    normalized_check = _as_check(check)
    spec = _CHECKS[normalized_check]
    command = list(spec.command)

    if spec.requires_symbol:
        command.extend(("--symbol", _validated_symbol(symbol)))

    if spec.accepts_order_reference:
        if order_id is None and client_order_id is None:
            raise BinanceCliPolicyError("query_order requires order_id or client_order_id")
        if order_id is not None:
            if order_id <= 0:
                raise BinanceCliPolicyError("order_id must be positive")
            command.extend(("--order-id", str(order_id)))
        if client_order_id is not None:
            command.extend(("--orig-client-order-id", _validated_client_order_id(client_order_id)))
    elif order_id is not None or client_order_id is not None:
        raise BinanceCliPolicyError(f"{normalized_check.value} does not accept an order reference")

    return tuple(command)


def _validate_testnet_base_url(value: str) -> str:
    parsed = urlparse(value)
    try:
        valid_port = parsed.port in (None, 443)
    except ValueError:
        valid_port = False
    if not (
        parsed.scheme == "https"
        and parsed.hostname == TESTNET_FUTURES_HOST
        and valid_port
        and parsed.path in ("", "/")
        and not parsed.query
        and not parsed.fragment
    ):
        raise BinanceCliPolicyError(
            "BINANCE_FUTURES_USDS_BASE_PATH must be exactly the Binance USDⓈ-M Testnet host"
        )
    return TESTNET_FUTURES_BASE_URL


def _credential_pair(environ: Mapping[str, str]) -> tuple[str, str, str] | None:
    official_key = environ.get("BINANCE_API_KEY", "")
    official_secret = environ.get("BINANCE_SECRET_KEY", "")
    worker_key = environ.get("BINANCE_TESTNET_API_KEY", "")
    worker_secret = environ.get("BINANCE_TESTNET_API_SECRET", "")

    if bool(official_key) != bool(official_secret):
        raise BinanceCliPolicyError(
            "BINANCE_API_KEY and BINANCE_SECRET_KEY must be provided together"
        )
    if bool(worker_key) != bool(worker_secret):
        raise BinanceCliPolicyError(
            "BINANCE_TESTNET_API_KEY and BINANCE_TESTNET_API_SECRET must be provided together"
        )
    if official_key and worker_key:
        raise BinanceCliPolicyError(
            "choose one credential namespace; refusing ambiguous CLI/worker credentials"
        )
    if official_key:
        return official_key, official_secret, "BINANCE_CLI_ENV"
    if worker_key:
        return worker_key, worker_secret, "WORKER_TESTNET_ENV"
    return None


def _prepare_environment(environ: Mapping[str, str]) -> tuple[dict[str, str], str]:
    """Return a child environment with only the explicit Testnet route/keys."""

    if environ.get("BINANCE_API_ENV") != "testnet":
        raise BinanceCliPolicyError(
            "BINANCE_API_ENV must be exactly 'testnet'; prod/demo and an unset value are blocked"
        )

    base_url = _validate_testnet_base_url(
        environ.get("BINANCE_FUTURES_USDS_BASE_PATH", TESTNET_FUTURES_BASE_URL)
    )
    credentials = _credential_pair(environ)

    # Keep PATH/HOME/TMP and other process basics, but strip every Binance
    # variable first so a profile or another product cannot override the route.
    child_env = {
        key: value for key, value in environ.items() if not key.startswith("BINANCE_")
    }
    child_env["BINANCE_API_ENV"] = "testnet"
    child_env["BINANCE_FUTURES_USDS_BASE_PATH"] = base_url
    credential_source = "NONE"
    if credentials is not None:
        api_key, secret_key, credential_source = credentials
        child_env["BINANCE_API_KEY"] = api_key
        child_env["BINANCE_SECRET_KEY"] = secret_key
    return child_env, credential_source


def _redact_text(value: str, environ: Mapping[str, str]) -> str:
    redacted = value
    for key in _SENSITIVE_ENV_KEYS:
        secret = environ.get(key, "")
        if secret:
            redacted = redacted.replace(secret, "[REDACTED]")
    return redacted[:4000]


def _decode_output(value: str) -> Any:
    text = value.strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return text


@dataclass(frozen=True)
class BinanceCliResult:
    """Sanitized result suitable for a research log or evidence record."""

    check: str
    status: str
    environment: str = "TESTNET"
    base_url: str = TESTNET_FUTURES_BASE_URL
    command: tuple[str, ...] | None = None
    credential_source: str = "NONE"
    returncode: int | None = None
    payload: Any = None
    error: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "tool": "binance-cli",
            "check": self.check,
            "status": self.status,
            "environment": self.environment,
            "base_url": self.base_url,
            "command": list(self.command) if self.command is not None else None,
            "credential_source": self.credential_source,
            "returncode": self.returncode,
            "payload": self.payload,
            "error": self.error,
            "verified": False,
            "evidence_status": "RESEARCH_OR_CONTRACT_CROSS_CHECK_ONLY",
        }


class BinanceCliResearchRunner:
    """Run only the read-only Binance CLI checks declared in ``ReadOnlyCheck``."""

    def __init__(
        self,
        *,
        binary: str | None = None,
        timeout_sec: float = DEFAULT_TIMEOUT_SEC,
        environ: Mapping[str, str] | None = None,
    ) -> None:
        if timeout_sec <= 0 or timeout_sec > 120:
            raise ValueError("timeout_sec must be between 0 and 120 seconds")
        self.timeout_sec = timeout_sec
        self.environ = dict(os.environ if environ is None else environ)
        self.binary = (
            binary or self.environ.get("BINANCE_CLI_PATH") or DEFAULT_BINARY
        ).strip()
        if not self.binary:
            raise ValueError("binance-cli binary path must not be empty")

    def _validate_binary_for_credentials(self) -> None:
        """Prevent PATH hijacking or arbitrary tools from receiving secrets."""

        if not os.path.isabs(self.binary):
            raise BinanceCliPolicyError(
                "signed checks require an absolute BINANCE_CLI_PATH to the official binance-cli binary"
            )
        binary_name = os.path.basename(self.binary).lower()
        if binary_name not in ALLOWED_BINARY_NAMES:
            raise BinanceCliPolicyError(
                "signed checks only allow an executable named binance-cli or binance-cli.exe"
            )

    def run(
        self,
        check: ReadOnlyCheck | str,
        *,
        symbol: str = DEFAULT_SYMBOL,
        order_id: int | None = None,
        client_order_id: str | None = None,
    ) -> BinanceCliResult:
        normalized_check = _as_check(check)
        command = build_read_only_command(
            normalized_check,
            symbol=symbol,
            order_id=order_id,
            client_order_id=client_order_id,
        )
        child_env, credential_source = _prepare_environment(self.environ)
        spec = _CHECKS[normalized_check]
        if spec.requires_credentials and credential_source == "NONE":
            return BinanceCliResult(
                check=normalized_check.value,
                status="NOT_RUN",
                command=command,
                credential_source=credential_source,
                error="signed read-only check requires explicit Testnet credentials",
            )
        if spec.requires_credentials:
            try:
                self._validate_binary_for_credentials()
            except BinanceCliPolicyError as exc:
                return BinanceCliResult(
                    check=normalized_check.value,
                    status="NOT_RUN",
                    command=command,
                    credential_source=credential_source,
                    error=str(exc),
                )

        resolved_binary = shutil.which(self.binary)
        if resolved_binary is None:
            return BinanceCliResult(
                check=normalized_check.value,
                status="NOT_RUN",
                command=command,
                credential_source=credential_source,
                error="binance-cli is not installed or not on PATH",
            )

        # The official CLI supports local profiles.  An isolated config home
        # ensures a profile cannot silently replace the explicit Testnet route
        # or credential namespace supplied above.
        try:
            with tempfile.TemporaryDirectory(prefix="blessing-binance-cli-") as config_dir:
                for config_key in (
                    "HOME",
                    "USERPROFILE",
                    "APPDATA",
                    "LOCALAPPDATA",
                    "XDG_CONFIG_HOME",
                ):
                    child_env[config_key] = config_dir
                completed = subprocess.run(
                    [resolved_binary, *command],
                    env=child_env,
                    capture_output=True,
                    text=True,
                    timeout=self.timeout_sec,
                    check=False,
                    shell=False,
                )
        except subprocess.TimeoutExpired:
            return BinanceCliResult(
                check=normalized_check.value,
                status="FAIL",
                command=command,
                credential_source=credential_source,
                error=f"binance-cli timed out after {self.timeout_sec:g} seconds",
            )
        except OSError as exc:
            return BinanceCliResult(
                check=normalized_check.value,
                status="NOT_RUN",
                command=command,
                credential_source=credential_source,
                error=f"could not start binance-cli: {type(exc).__name__}",
            )

        stderr = _redact_text(completed.stderr or "", self.environ)
        return BinanceCliResult(
            check=normalized_check.value,
            status="PASS" if completed.returncode == 0 else "FAIL",
            command=command,
            credential_source=credential_source,
            returncode=completed.returncode,
            payload=_decode_output(_redact_text(completed.stdout or "", self.environ)),
            error=stderr
            or (None if completed.returncode == 0 else "binance-cli returned a non-zero exit code"),
        )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run one allowlisted, read-only Binance USDⓈ-M Testnet CLI check."
    )
    parser.add_argument(
        "--check", required=True, choices=[check.value for check in ReadOnlyCheck]
    )
    parser.add_argument("--symbol", default=DEFAULT_SYMBOL)
    parser.add_argument("--order-id", type=int)
    parser.add_argument("--client-order-id")
    parser.add_argument(
        "--binary",
        default=None,
        help="optional binary path for public checks; signed checks require an absolute official binance-cli path",
    )
    parser.add_argument("--timeout-sec", type=float, default=DEFAULT_TIMEOUT_SEC)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        result = BinanceCliResearchRunner(
            binary=args.binary,
            timeout_sec=args.timeout_sec,
        ).run(
            args.check,
            symbol=args.symbol,
            order_id=args.order_id,
            client_order_id=args.client_order_id,
        )
    except BinanceCliPolicyError as exc:
        result = BinanceCliResult(
            check=args.check,
            status="BLOCKED",
            environment="UNKNOWN",
            base_url="UNVERIFIED",
            error=str(exc),
        )
        print(json.dumps(result.as_dict(), ensure_ascii=False))
        return 2

    print(json.dumps(result.as_dict(), ensure_ascii=False))
    return 0 if result.status == "PASS" else 3


if __name__ == "__main__":
    raise SystemExit(main())
