import subprocess
from pathlib import Path

import pytest

from apps.trading_worker.research.binance_cli import (
    BinanceCliPolicyError,
    BinanceCliResearchRunner,
    ReadOnlyCheck,
    build_read_only_command,
)


def test_only_allowlisted_read_command_can_be_built():
    assert build_read_only_command("server_time") == (
        "futures-usds",
        "check-server-time",
    )
    assert build_read_only_command("positions", symbol="btcusdt")[-2:] == (
        "--symbol",
        "BTCUSDT",
    )

    with pytest.raises(BinanceCliPolicyError, match="mutation/custom requests"):
        build_read_only_command("new_order")


def test_mainnet_read_only_route_is_fixed_and_other_routes_are_blocked(monkeypatch):
    monkeypatch.setattr(
        "apps.trading_worker.research.binance_cli.shutil.which",
        lambda binary: "C:/tools/binance-cli.exe" if binary == "binance-cli" else None,
    )
    monkeypatch.setattr(
        "apps.trading_worker.research.binance_cli.subprocess.run",
        lambda command, **kwargs: subprocess.CompletedProcess(
            command, 0, stdout='{"serverTime": 1700000000000}', stderr=""
        ),
    )

    mainnet = BinanceCliResearchRunner(
        environ={
            "BINANCE_API_ENV": "prod",
            "BINANCE_FUTURES_USDS_BASE_PATH": "https://fapi.binance.com/",
        }
    ).run(ReadOnlyCheck.SERVER_TIME)
    assert mainnet.status == "PASS"
    assert mainnet.environment == "MAINNET"
    assert mainnet.base_url == "https://fapi.binance.com"
    explicit_environment = BinanceCliResearchRunner(
        environment="mainnet",
        environ={"PATH": "C:/tools"},
    ).run(ReadOnlyCheck.SERVER_TIME)
    assert explicit_environment.status == "PASS"
    assert explicit_environment.base_url == "https://fapi.binance.com"
    with pytest.raises(BinanceCliPolicyError, match="ETHUSDC"):
        mainnet_runner = BinanceCliResearchRunner(
            environ={
                "BINANCE_API_ENV": "prod",
                "BINANCE_FUTURES_USDS_BASE_PATH": "https://fapi.binance.com/",
            }
        )
        mainnet_runner.run(ReadOnlyCheck.POSITIONS, symbol="BTCUSDT")

    for api_env, base_url in (
        ("prod", "https://api.binance.com"),
        ("demo", "https://demo-api.binance.com"),
        ("testnet", "https://api.binance.com"),
    ):
        runner = BinanceCliResearchRunner(
            environ={
                "BINANCE_API_ENV": api_env,
                "BINANCE_FUTURES_USDS_BASE_PATH": base_url,
            }
        )
        with pytest.raises(BinanceCliPolicyError):
            runner.run(ReadOnlyCheck.SERVER_TIME)


def test_public_check_uses_explicit_testnet_environment(monkeypatch):
    calls = []

    monkeypatch.setattr(
        "apps.trading_worker.research.binance_cli.shutil.which",
        lambda binary: "C:/tools/binance-cli.exe" if binary == "binance-cli" else None,
    )

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(
            command,
            0,
            stdout='{"serverTime": 1700000000000}',
            stderr="",
        )

    monkeypatch.setattr("apps.trading_worker.research.binance_cli.subprocess.run", fake_run)

    result = BinanceCliResearchRunner(
        environ={
            "PATH": "C:/tools",
            "BINANCE_API_ENV": "testnet",
            "BINANCE_FUTURES_USDS_BASE_PATH": "https://testnet.binancefuture.com/",
            "BINANCE_PROFILE": "a-profile-that-must-not-be-used",
        }
    ).run(ReadOnlyCheck.SERVER_TIME)

    assert result.status == "PASS"
    assert result.payload == {"serverTime": 1700000000000}
    assert result.base_url == "https://testnet.binancefuture.com"
    assert calls[0][0] == [
        "C:/tools/binance-cli.exe",
        "futures-usds",
        "check-server-time",
    ]
    child_env = calls[0][1]["env"]
    assert child_env["BINANCE_API_ENV"] == "testnet"
    assert child_env["BINANCE_FUTURES_USDS_BASE_PATH"] == "https://testnet.binancefuture.com"
    assert "BINANCE_PROFILE" not in child_env


def test_public_check_never_forwards_credentials(monkeypatch):
    captured = {}
    monkeypatch.setattr(
        "apps.trading_worker.research.binance_cli.shutil.which",
        lambda _: "binance-cli",
    )

    def fake_run(command, **kwargs):
        captured.update(kwargs["env"])
        return subprocess.CompletedProcess(command, 0, stdout="{}", stderr="")

    monkeypatch.setattr("apps.trading_worker.research.binance_cli.subprocess.run", fake_run)
    result = BinanceCliResearchRunner(
        environ={
            "BINANCE_API_ENV": "testnet",
            "BINANCE_FUTURES_USDS_BASE_PATH": "https://testnet.binancefuture.com",
            "BINANCE_TESTNET_API_KEY": "testnet-key-fixture",
            "BINANCE_TESTNET_API_SECRET": "testnet-secret-fixture",
        }
    ).run(ReadOnlyCheck.MARK_PRICE, symbol="BTCUSDT")

    assert result.status == "PASS"
    assert result.credential_source == "NONE"
    assert "BINANCE_API_KEY" not in captured
    assert "BINANCE_SECRET_KEY" not in captured
    assert "BINANCE_TESTNET_API_KEY" not in captured
    assert "BINANCE_TESTNET_API_SECRET" not in captured


def test_public_check_rejects_non_official_binary(monkeypatch):
    monkeypatch.setattr(
        "apps.trading_worker.research.binance_cli.shutil.which",
        lambda _: pytest.fail("arbitrary binaries must be rejected before execution"),
    )
    result = BinanceCliResearchRunner(
        binary="python.exe",
        environ={"BINANCE_API_ENV": "testnet"},
    ).run(ReadOnlyCheck.SERVER_TIME)

    assert result.status == "NOT_RUN"
    assert "official binance-cli" in result.error


def test_signed_check_without_credentials_is_not_run(monkeypatch):
    monkeypatch.setattr(
        "apps.trading_worker.research.binance_cli.shutil.which",
        lambda _: pytest.fail("credential-gated checks must not start the CLI"),
    )
    result = BinanceCliResearchRunner(
        environ={"BINANCE_API_ENV": "testnet"}
    ).run(ReadOnlyCheck.ACCOUNT)

    assert result.status == "NOT_RUN"
    assert result.credential_source == "NONE"


def test_worker_testnet_credentials_are_mapped_without_leaking_worker_names(monkeypatch):
    calls = []
    monkeypatch.setattr(
        "apps.trading_worker.research.binance_cli.shutil.which",
        lambda _: "binance-cli",
    )

    def fake_run(command, **kwargs):
        calls.append(kwargs["env"])
        return subprocess.CompletedProcess(command, 0, stdout="[]", stderr="")

    monkeypatch.setattr("apps.trading_worker.research.binance_cli.subprocess.run", fake_run)
    result = BinanceCliResearchRunner(
        binary=str(Path.cwd() / "binance-cli"),
        environ={
            "BINANCE_API_ENV": "testnet",
            "BINANCE_TESTNET_API_KEY": "testnet-key-fixture",
            "BINANCE_TESTNET_API_SECRET": "testnet-secret-fixture",
        }
    ).run(ReadOnlyCheck.ACCOUNT)

    assert result.status == "PASS"
    assert result.credential_source == "WORKER_TESTNET_ENV"
    assert calls[0]["BINANCE_API_KEY"] == "testnet-key-fixture"
    assert calls[0]["BINANCE_SECRET_KEY"] == "testnet-secret-fixture"
    assert "BINANCE_TESTNET_API_KEY" not in calls[0]
    assert "BINANCE_TESTNET_API_SECRET" not in calls[0]


def test_cli_result_redacts_credential_values_from_stdout(monkeypatch):
    monkeypatch.setattr(
        "apps.trading_worker.research.binance_cli.shutil.which",
        lambda _: "binance-cli",
    )

    def fake_run(command, **kwargs):
        return subprocess.CompletedProcess(
            command,
            0,
            stdout='{"apiKey":"cli-key-fixture","secret":"cli-secret-fixture"}',
            stderr="",
        )

    monkeypatch.setattr("apps.trading_worker.research.binance_cli.subprocess.run", fake_run)
    result = BinanceCliResearchRunner(
        binary=str(Path.cwd() / "binance-cli"),
        environ={
            "BINANCE_API_ENV": "testnet",
            "BINANCE_API_KEY": "cli-key-fixture",
            "BINANCE_SECRET_KEY": "cli-secret-fixture",
        }
    ).run(ReadOnlyCheck.ACCOUNT)

    rendered = str(result.as_dict())
    assert "cli-key-fixture" not in rendered
    assert "cli-secret-fixture" not in rendered
    assert "[REDACTED]" in rendered


def test_signed_account_payload_is_summarized_without_account_values(monkeypatch):
    monkeypatch.setattr(
        "apps.trading_worker.research.binance_cli.shutil.which",
        lambda _: "binance-cli",
    )

    def fake_run(command, **kwargs):
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=(
                '{"canTrade":true,"assets":[{"asset":"USDC",'
                '"walletBalance":"100.00","availableBalance":"99.50"}],'
                '"positions":[{"symbol":"ETHUSDC","positionAmt":"0.5",'
                '"entryPrice":"2500.00","positionId":"position-123"}],'
                '"openOrders":[{"orderId":987654,"origQty":"0.01",'
                '"price":"2500.00"}],"apiKey":"cli-key-fixture",'
                '"secret":"cli-secret-fixture"}'
            ),
            stderr="",
        )

    monkeypatch.setattr("apps.trading_worker.research.binance_cli.subprocess.run", fake_run)
    result = BinanceCliResearchRunner(
        binary=str(Path.cwd() / "binance-cli"),
        environ={
            "BINANCE_API_ENV": "prod",
            "BINANCE_FUTURES_USDS_BASE_PATH": "https://fapi.binance.com",
            "BINANCE_API_KEY": "cli-key-fixture",
            "BINANCE_SECRET_KEY": "cli-secret-fixture",
        },
        environment="mainnet",
    ).run(ReadOnlyCheck.ACCOUNT, symbol="ETHUSDC")

    rendered = str(result.as_dict())
    assert result.status == "PASS"
    assert result.payload["safe_fields"]["canTrade"] is True
    assert result.payload["payload_kind"] == "object"
    assert "100.00" not in rendered
    assert "99.50" not in rendered
    assert "position-123" not in rendered
    assert "987654" not in rendered
    assert "[REDACTED]" in rendered


def test_signed_check_without_explicit_official_binary_path_is_not_run(monkeypatch):
    monkeypatch.setattr(
        "apps.trading_worker.research.binance_cli.shutil.which",
        lambda _: pytest.fail("signed checks must not use an implicit PATH binary"),
    )
    result = BinanceCliResearchRunner(
        environ={
            "BINANCE_API_ENV": "testnet",
            "BINANCE_TESTNET_API_KEY": "testnet-key-fixture",
            "BINANCE_TESTNET_API_SECRET": "testnet-secret-fixture",
        }
    ).run(ReadOnlyCheck.ACCOUNT)

    assert result.status == "NOT_RUN"
    assert "absolute" in result.error


def test_child_process_uses_isolated_cli_config_home(monkeypatch):
    captured = {}
    monkeypatch.setattr(
        "apps.trading_worker.research.binance_cli.shutil.which",
        lambda _: "binance-cli",
    )

    def fake_run(command, **kwargs):
        captured.update(kwargs["env"])
        return subprocess.CompletedProcess(command, 0, stdout="{}", stderr="")

    monkeypatch.setattr("apps.trading_worker.research.binance_cli.subprocess.run", fake_run)
    result = BinanceCliResearchRunner(
        environ={
            "BINANCE_API_ENV": "testnet",
            "BINANCE_FUTURES_USDS_BASE_PATH": "https://testnet.binancefuture.com",
        }
    ).run(ReadOnlyCheck.SERVER_TIME)

    assert result.status == "PASS"
    assert captured["HOME"] == captured["XDG_CONFIG_HOME"]
    assert captured["APPDATA"] == captured["LOCALAPPDATA"]


def test_query_order_requires_a_reference_and_never_accepts_mutation_flags():
    with pytest.raises(BinanceCliPolicyError, match="requires order_id"):
        build_read_only_command("query_order", symbol="BTCUSDT")

    assert build_read_only_command(
        "query_order", symbol="BTCUSDT", order_id=42
    )[-2:] == ("--order-id", "42")
    assert build_read_only_command(
        "query_order", symbol="BTCUSDT", client_order_id="research-42"
    )[-2:] == ("--orig-client-order-id", "research-42")
