"""Pytest contract test configuration and automatic local environment loading."""

import os
from pathlib import Path

import pytest
from dotenv import dotenv_values


@pytest.fixture(autouse=True)
def load_contract_env(monkeypatch):
    """Load local .env into test environment only during contract tests."""
    root_dir = Path(__file__).resolve().parent.parent.parent.parent
    env_file = root_dir / ".env"
    if env_file.exists():
        values = dotenv_values(env_file)
        for key, val in values.items():
            if val is not None and key not in os.environ:
                monkeypatch.setenv(key, str(val))
