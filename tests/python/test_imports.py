def test_trading_worker_main_imports():
    import apps.trading_worker.main as main

    assert main.TradingWorkerApp is not None
    assert main.BinanceExecutionAdapter is not None
    assert main.app is not None
