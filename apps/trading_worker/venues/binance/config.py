from enum import Enum

class BinanceEnvironment(str, Enum):
    TESTNET = "TESTNET"
    MAINNET = "MAINNET"

def get_rest_url(env: BinanceEnvironment) -> str:
    if env == BinanceEnvironment.TESTNET:
        return "https://testnet.binancefuture.com"
    return "https://fapi.binance.com"

def get_ws_url(env: BinanceEnvironment) -> str:
    if env == BinanceEnvironment.TESTNET:
        return "wss://stream.binancefuture.com/ws"
    return "wss://fstream.binance.com/ws"
