"""Fail-closed, read-only bridge to Binance's official CLI.

The Python Trading Worker remains the only execution authority.  This module
exists for research and contract cross-checks, so it deliberately exposes a
small allowlist of USDⓈ-M read operations and has no generic subprocess or
custom-request escape hatch.  Mainnet is supported only for the fixed
production host and remains read-only.

The official CLI is an independently installed Rust binary.  It is therefore
not a Python or npm runtime dependency and is never imported by the worker's
execution path. The wrapper supports the documented ``testnet`` and ``prod``
routes, but keeps both routes fixed and read-only.
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
MAINNET_FUTURES_BASE_URL = "https://fapi.binance.com"
MAINNET_FUTURES_HOST = "fapi.binance.com"
DEFAULT_TIMEOUT_SEC = 15.0
DEFAULT_SYMBOL = "BTCUSDT"
MAINNET_SYMBOL = "ETHUSDC"
DEFAULT_RECV_WINDOW = "5000"
DEFAULT_BINARY = "binance-cli"
ALLOWED_BINARY_NAMES = frozenset({"binance-cli", "binance-cli.exe"})


class BinanceCliEnvironment(StrEnum):
    TESTNET = "TESTNET"
    MAINNET = "MAINNET"


def _parse_environment(value: str | BinanceCliEnvironment) -> BinanceCliEnvironment:
    try:
        return value if isinstance(value, BinanceCliEnvironment) else BinanceCliEnvironment(value.strip().upper())
    except (AttributeError, ValueError) as exc:
        raise BinanceCliPolicyError("CLI environment must be testnet or mainnet") from exc


def _route_for_environment(environment: BinanceCliEnvironment) -> tuple[str, str, str]:
    if environment is BinanceCliEnvironment.TESTNET:
        return "testnet", TESTNET_FUTURES_BASE_URL, TESTNET_FUTURES_HOST
    # Binance CLI calls its production environment `prod`; no arbitrary URL is
    # accepted by this research wrapper.
    return "prod", MAINNET_FUTURES_BASE_URL, MAINNET_FUTURES_HOST


class BinanceCliResearchError(RuntimeError):
    """Base error for the isolated Binance CLI research boundary."""


class BinanceCliPolicyError(BinanceCliResearchError):
    """Raised when a requested operation violates the read-only CLI policy."""


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
        "BINANCE_MAINNET_API_KEY",
        "BINANCE_MAINNET_API_SECRET",
    }
)
_SENSITIVE_PAYLOAD_KEY_PATTERN = re.compile(
    r"(?:api[_-]?key|secret|password|passphrase|token|authorization|signature|"
    r"private[_-]?key|access[_-]?key|credential|dsn|database[_-]?url)",
    re.IGNORECASE,
)
_SAFE_SIGNED_FIELDS = frozenset(
    {
        "canTrade",
        "dualSidePosition",
        "multiAssetsMargin",
        "multiAssetsMode",
        "status",
        "contractType",
        "quoteAsset",
        "marginAsset",
        "positionSide",
        "serverTime",
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


def _validate_base_url(value: str, environment: BinanceCliEnvironment) -> str:
    _, expected_url, expected_host = _route_for_environment(environment)
    parsed = urlparse(value)
    try:
        valid_port = parsed.port in (None, 443)
    except ValueError:
        valid_port = False
    if not (
        parsed.scheme == "https"
        and parsed.hostname == expected_host
        and valid_port
        and parsed.path in ("", "/")
        and not parsed.query
        and not parsed.fragment
    ):
        raise BinanceCliPolicyError(
            f"BINANCE_FUTURES_USDS_BASE_PATH must be exactly the Binance USDⓈ-M {environment.value} host"
        )
    return expected_url


def _credential_pair(
    environ: Mapping[str, str], environment: BinanceCliEnvironment
) -> tuple[str, str, str] | None:
    official_key = environ.get("BINANCE_API_KEY", "")
    official_secret = environ.get("BINANCE_SECRET_KEY", "")
    suffix = "TESTNET" if environment is BinanceCliEnvironment.TESTNET else "MAINNET"
    worker_key = environ.get(f"BINANCE_{suffix}_API_KEY", "")
    worker_secret = environ.get(f"BINANCE_{suffix}_API_SECRET", "")

    if bool(official_key) != bool(official_secret):
        raise BinanceCliPolicyError(
            "BINANCE_API_KEY and BINANCE_SECRET_KEY must be provided together"
        )
    if bool(worker_key) != bool(worker_secret):
        raise BinanceCliPolicyError(
            f"BINANCE_{suffix}_API_KEY and BINANCE_{suffix}_API_SECRET must be provided together"
        )
    if official_key and worker_key:
        raise BinanceCliPolicyError(
            "choose one credential namespace; refusing ambiguous CLI/worker credentials"
        )
    if official_key:
        return official_key, official_secret, "BINANCE_CLI_ENV"
    if worker_key:
        return worker_key, worker_secret, f"WORKER_{suffix}_ENV"
    return None


def _prepare_environment(
    environ: Mapping[str, str],
    *,
    environment: BinanceCliEnvironment,
    include_credentials: bool,
) -> tuple[dict[str, str], str]:
    """Return a child environment with one explicit fixed route and keys."""

    api_env, default_base_url, _ = _route_for_environment(environment)
    configured_api_env = environ.get("BINANCE_API_ENV")
    if configured_api_env not in (None, api_env):
        raise BinanceCliPolicyError(
            f"BINANCE_API_ENV must be exactly '{api_env}'; other environments are blocked"
        )

    base_url = _validate_base_url(
        environ.get("BINANCE_FUTURES_USDS_BASE_PATH", default_base_url), environment
    )
    credentials = _credential_pair(environ, environment)

    # Keep PATH/HOME/TMP and other process basics, but strip every Binance
    # variable first so a profile or another product cannot override the route.
    child_env = {
        key: value for key, value in environ.items() if not key.startswith("BINANCE_")
    }
    child_env["BINANCE_API_ENV"] = api_env
    child_env["BINANCE_FUTURES_USDS_BASE_PATH"] = base_url
    credential_source = "NONE"
    if include_credentials and credentials is not None:
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


def _payload_key_is_sensitive(key: object) -> bool:
    return bool(_SENSITIVE_PAYLOAD_KEY_PATTERN.search(str(key)))


def _redact_payload(value: Any, environ: Mapping[str, str]) -> Any:
    """Redact credential-shaped fields recursively before a result is retained."""

    if isinstance(value, Mapping):
        return {
            str(key): "[REDACTED]"
            if _payload_key_is_sensitive(key)
            else _redact_payload(item, environ)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact_payload(item, environ) for item in value]
    if isinstance(value, tuple):
        return [_redact_payload(item, environ) for item in value]
    if isinstance(value, str):
        return _redact_text(value, environ)
    return value


def _signed_payload_summary(value: Any, environ: Mapping[str, str]) -> dict[str, Any]:
    """Keep only bounded, non-sensitive evidence from a signed account call.

    Signed account responses can contain balances, order IDs, positions, and
    other account-sensitive values.  The CLI is only a cross-check, so callers
    receive shape/count metadata and a tiny allowlist of safe status fields,
    never the original account payload.
    """

    if isinstance(value, Mapping):
        payload_kind = "object"
        top_level_keys = sorted(str(key) for key in value)
        item_count = len(value)
        candidates = value.items()
    elif isinstance(value, list):
        payload_kind = "array"
        top_level_keys = []
        item_count = len(value)
        candidates = ()
    elif value is None:
        payload_kind = "empty"
        top_level_keys = []
        item_count = 0
        candidates = ()
    else:
        payload_kind = type(value).__name__
        top_level_keys = []
        item_count = 1
        candidates = ()

    safe_fields: dict[str, bool | int | str] = {}
    for key, item in candidates:
        key_text = str(key)
        if key_text not in _SAFE_SIGNED_FIELDS or _payload_key_is_sensitive(key):
            continue
        if isinstance(item, (bool, int, str)) and not isinstance(item, float):
            safe_fields[key_text] = _redact_text(str(item), environ) if isinstance(item, str) else item

    return {
        "payload_kind": payload_kind,
        "top_level_keys": top_level_keys[:100],
        "item_count": item_count,
        "safe_fields": safe_fields,
        "redacted_fields": ["signed_account_payload", "[REDACTED]"],
    }


def _signed_stderr(value: str, environ: Mapping[str, str]) -> str | None:
    """Never retain account-sensitive stderr from a credentialed CLI call."""

    if not value.strip():
        return None
    return "binance-cli emitted stderr for a signed check (content suppressed)"


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
            "payload": _redact_payload(self.payload, {}),
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
        environment: BinanceCliEnvironment | str | None = None,
    ) -> None:
        if timeout_sec <= 0 or timeout_sec > 120:
            raise ValueError("timeout_sec must be between 0 and 120 seconds")
        self.timeout_sec = timeout_sec
        self.environ = dict(os.environ if environ is None else environ)
        inferred_environment = (
            "MAINNET" if self.environ.get("BINANCE_API_ENV") == "prod" else "TESTNET"
        )
        self.environment = _parse_environment(environment or inferred_environment)
        self.api_env, self.base_url, _ = _route_for_environment(self.environment)
        self.binary = (
            binary or self.environ.get("BINANCE_CLI_PATH") or DEFAULT_BINARY
        ).strip()
        if not self.binary:
            raise ValueError("binance-cli binary path must not be empty")

    def _validate_binary_name(self) -> None:
        """Prevent this bridge from executing an arbitrary helper binary."""

        binary_name = os.path.basename(self.binary).lower()
        if binary_name not in ALLOWED_BINARY_NAMES:
            raise BinanceCliPolicyError(
                "only the official binance-cli or binance-cli.exe binary is allowed"
            )

    def _validate_binary_for_credentials(self) -> None:
        """Prevent PATH hijacking or arbitrary tools from receiving secrets."""

        if not os.path.isabs(self.binary):
            raise BinanceCliPolicyError(
                "signed checks require an absolute BINANCE_CLI_PATH to the official binance-cli binary"
            )
        self._validate_binary_name()

    def run(
        self,
        check: ReadOnlyCheck | str,
        *,
        symbol: str | None = None,
        order_id: int | None = None,
        client_order_id: str | None = None,
    ) -> BinanceCliResult:
        normalized_check = _as_check(check)
        if self.environment is BinanceCliEnvironment.MAINNET:
            effective_symbol = MAINNET_SYMBOL if symbol is None else symbol
            if effective_symbol.strip().upper() != MAINNET_SYMBOL:
                raise BinanceCliPolicyError(
                    "Mainnet Binance CLI checks are restricted to ETHUSDC"
                )
        else:
            effective_symbol = DEFAULT_SYMBOL if symbol is None else symbol
        command = build_read_only_command(
            normalized_check,
            symbol=effective_symbol,
            order_id=order_id,
            client_order_id=client_order_id,
        )
        spec = _CHECKS[normalized_check]
        child_env, credential_source = _prepare_environment(
            self.environ,
            environment=self.environment,
            include_credentials=spec.requires_credentials,
        )
        if spec.requires_credentials and credential_source == "NONE":
            return BinanceCliResult(
                check=normalized_check.value,
                status="NOT_RUN",
                environment=self.environment.value,
                base_url=self.base_url,
                command=command,
                credential_source=credential_source,
                error=f"signed read-only check requires explicit {self.environment.value} credentials",
            )
        try:
            self._validate_binary_name()
            if spec.requires_credentials:
                self._validate_binary_for_credentials()
        except BinanceCliPolicyError as exc:
            return BinanceCliResult(
                check=normalized_check.value,
                status="NOT_RUN",
                environment=self.environment.value,
                base_url=self.base_url,
                command=command,
                credential_source=credential_source,
                error=str(exc),
            )

        resolved_binary = shutil.which(self.binary)
        if resolved_binary is None:
            return BinanceCliResult(
                check=normalized_check.value,
                status="NOT_RUN",
                environment=self.environment.value,
                base_url=self.base_url,
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
                environment=self.environment.value,
                base_url=self.base_url,
                command=command,
                credential_source=credential_source,
                error=f"binance-cli timed out after {self.timeout_sec:g} seconds",
            )
        except OSError as exc:
            return BinanceCliResult(
                check=normalized_check.value,
                status="NOT_RUN",
                environment=self.environment.value,
                base_url=self.base_url,
                command=command,
                credential_source=credential_source,
                error=f"could not start binance-cli: {type(exc).__name__}",
            )

        stderr = (
            _signed_stderr(completed.stderr or "", self.environ)
            if spec.requires_credentials
            else _redact_text(completed.stderr or "", self.environ)
        )
        decoded_output = _decode_output(completed.stdout or "")
        payload = (
            _signed_payload_summary(decoded_output, self.environ)
            if spec.requires_credentials
            else _redact_payload(
                _decode_output(_redact_text(completed.stdout or "", self.environ)),
                self.environ,
            )
        )
        return BinanceCliResult(
            check=normalized_check.value,
            status="PASS" if completed.returncode == 0 else "FAIL",
            environment=self.environment.value,
            base_url=self.base_url,
            command=command,
            credential_source=credential_source,
            returncode=completed.returncode,
            payload=payload,
            error=stderr
            or (None if completed.returncode == 0 else "binance-cli returned a non-zero exit code"),
        )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run one allowlisted, read-only Binance USDⓈ-M CLI check."
    )
    parser.add_argument("--environment", choices=("testnet", "mainnet"), default="testnet")
    parser.add_argument(
        "--check", required=True, choices=[check.value for check in ReadOnlyCheck]
    )
    parser.add_argument("--symbol", default=None)
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
            environment=args.environment,
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
