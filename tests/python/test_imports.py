def test_trading_worker_main_imports():
    import apps.trading_worker.main as main

    assert main.TradingWorkerApp is not None
    assert main.BinanceExecutionAdapter is not None
    assert main.app is not None


def test_persistence_uses_the_canonical_package_path():
    import importlib.util
    from pathlib import Path

    import apps.trading_worker.persistence.manager as manager

    assert manager.PersistenceManager is not None
    assert importlib.util.find_spec("apps.trading_worker.persistence.manager") is not None
    assert not Path("app/applet/apps/trading_worker/persistence").exists()
