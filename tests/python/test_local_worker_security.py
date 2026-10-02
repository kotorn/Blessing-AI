from fastapi.testclient import TestClient

from apps.trading_worker.main import (
    app,
    local_worker_identity_matches,
    worker_bind_host,
)


def test_local_worker_bind_is_loopback_even_if_a_wider_host_is_configured():
    assert worker_bind_host(
        {
            "LOCAL_ONLY": "true",
            "BIND_HOST": "0.0.0.0",
            "K_SERVICE": "production-service",
        }
    ) == "127.0.0.1"


def test_worker_container_and_cloud_bind_contract_is_preserved():
    assert worker_bind_host({"BIND_HOST": "0.0.0.0"}) == "0.0.0.0"
    assert worker_bind_host({"K_SERVICE": "production-service"}) == "0.0.0.0"
    assert worker_bind_host({}) == "127.0.0.1"


def test_local_worker_container_bind_is_internal_only(monkeypatch):
    import apps.trading_worker.main as worker_main

    monkeypatch.setattr(worker_main, "local_container_runtime", lambda values: True)
    assert worker_bind_host({"LOCAL_ONLY": "true", "LOCAL_WORKER_CONTAINER": "true"}) == "0.0.0.0"


def test_local_container_marker_alone_does_not_widen_host_bind(monkeypatch):
    import apps.trading_worker.main as worker_main

    monkeypatch.setattr(worker_main, "local_container_runtime", lambda values: False)
    assert worker_bind_host(
        {"LOCAL_ONLY": "true", "LOCAL_WORKER_CONTAINER": "true", "BIND_HOST": "0.0.0.0"}
    ) == "127.0.0.1"


def test_local_worker_identity_is_fail_closed_and_exact():
    assert local_worker_identity_matches("per-run-token", "Bearer per-run-token")
    assert not local_worker_identity_matches("", "Bearer per-run-token")
    assert not local_worker_identity_matches("per-run-token", "")
    assert not local_worker_identity_matches("per-run-token", "Bearer wrong-token")


def test_worker_http_api_requires_per_run_identity_only_when_local_guard_is_enabled(monkeypatch):
    monkeypatch.setenv("LOCAL_ONLY", "true")
    monkeypatch.delenv("LOCAL_WORKER_AUTH_REQUIRED", raising=False)
    monkeypatch.setenv("WORKER_IDENTITY_TOKEN", "unit-test-only-token")
    with TestClient(app) as client:
        assert client.get("/health").status_code == 401
        accepted = client.get(
            "/health",
            headers={"Authorization": "Bearer unit-test-only-token"},
        )
        assert accepted.status_code == 200
        assert "unit-test-only-token" not in accepted.text
