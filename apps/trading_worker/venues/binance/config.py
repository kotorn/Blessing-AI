from enum import Enum

class BinanceEnvironment(str, Enum):
    TESTNET = "TESTNET"
    MAINNET = "MAINNET"


# These are the only exchange environments that the execution adapter may
# address.  Keeping the map in one module prevents a runtime configuration
# value from becoming an arbitrary REST/WebSocket endpoint.
REST_URLS = {
    BinanceEnvironment.TESTNET: "https://testnet.binancefuture.com",
    BinanceEnvironment.MAINNET: "https://fapi.binance.com",
}
WS_URLS = {
    BinanceEnvironment.TESTNET: "wss://stream.binancefuture.com/ws",
    BinanceEnvironment.MAINNET: "wss://fstream.binance.com/ws",
}

PAPI_REST_URL = "https://papi.binance.com"
PAPI_WS_URL = "wss://fstream.binance.com/pm/ws"


def is_portfolio_margin_enabled() -> bool:
    """Return True if Binance Portfolio Margin mode is requested via environment."""
    import os
    return os.getenv("BINANCE_PORTFOLIO_MARGIN", "false").lower() in ("true", "1", "yes")


def environment_label(env: BinanceEnvironment) -> str:
    """Return the explicit provenance label used in evidence and ledgers."""

    return f"BINANCE_{env.value}"


def parse_environment(value: str | BinanceEnvironment) -> BinanceEnvironment:
    """Parse only the two supported Binance routes."""

    if isinstance(value, BinanceEnvironment):
        return value
    try:
        return BinanceEnvironment(str(value).strip().upper())
    except ValueError as exc:
        raise ValueError("Binance environment must be TESTNET or MAINNET") from exc

def get_rest_url(env: BinanceEnvironment) -> str:
    try:
        return REST_URLS[parse_environment(env)]
    except KeyError as exc:
        raise ValueError("Unsupported Binance REST environment") from exc

def get_ws_url(env: BinanceEnvironment) -> str:
    try:
        return WS_URLS[parse_environment(env)]
    except KeyError as exc:
        raise ValueError("Unsupported Binance WebSocket environment") from exc
