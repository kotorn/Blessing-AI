import importlib.util
from pathlib import Path


def test_contract_loader_never_loads_generic_env_or_mainnet(monkeypatch):
    path = Path(__file__).parent / "contract" / "conftest.py"
    spec = importlib.util.spec_from_file_location("contract_isolation", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    seen = []
    monkeypatch.delenv("BLESSING_DISABLE_TEST_DOTENV", raising=False)
    monkeypatch.setenv("BINANCE_MAINNET_API_KEY", "fixture-mainnet-never-use")
    monkeypatch.setenv("BINANCE_MAINNET_API_KEY_FILE", "fixture-path")
    monkeypatch.delenv("BINANCE_TESTNET_API_KEY", raising=False)
    monkeypatch.setattr(module.Path, "exists", lambda self: True)

    def values(source):
        seen.append(source.name)
        return {"BINANCE_TESTNET_API_KEY": "fixture-testnet", "BINANCE_MAINNET_API_SECRET": "forbidden", "MAINNET_LIVE_APPROVED": "true"}

    monkeypatch.setattr(module, "dotenv_values", values)
    monkeypatch.setenv("MAINNET_LIVE_APPROVED", "false")
    module.load_contract_env.__wrapped__(monkeypatch)
    assert seen == [".env.testnet"]
    assert "BINANCE_MAINNET_API_KEY" not in module.os.environ
    assert "BINANCE_MAINNET_API_KEY_FILE" not in module.os.environ
    assert "BINANCE_MAINNET_API_SECRET" not in module.os.environ
    assert module.os.environ["BINANCE_TESTNET_API_KEY"] == "fixture-testnet"
    assert module.os.environ["MAINNET_LIVE_APPROVED"] == "false"
