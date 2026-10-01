"""Pytest contract test configuration and automatic local environment loading."""

import os
from pathlib import Path

import pytest
from dotenv import dotenv_values


@pytest.fixture(autouse=True)
def load_contract_env(monkeypatch):
    """Load only Testnet credentials from a dedicated file, never generic .env."""
    for key in tuple(os.environ):
        if key.startswith("BINANCE_MAINNET_"):
            monkeypatch.delenv(key, raising=False)
    if os.getenv("BLESSING_DISABLE_TEST_DOTENV", "").strip().lower() in {"1", "true", "yes", "on"}:
        return
    root_dir = Path(__file__).resolve().parent.parent.parent.parent
    env_file = root_dir / ".env.testnet"
    if env_file.exists():
        values = dotenv_values(env_file)
        for key, val in values.items():
            if (key == "BINANCE_TESTNET" or key.startswith("BINANCE_TESTNET_")) \
                    and val is not None and key not in os.environ:
                monkeypatch.setenv(key, str(val))
