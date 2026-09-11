const fs = require('fs');
let code = fs.readFileSync('apps/trading_worker/main.py', 'utf-8');

code = code.replace(
  'from apps.trading_worker.engines.meta_allocator import MetaAllocator',
  'from apps.trading_worker.engines.market_scanner import MarketScannerEngine\nfrom apps.trading_worker.engines.meta_allocator import MetaAllocator'
);

code = code.replace(
  'self.risk_governor = RiskGovernor()',
  'self.risk_governor = RiskGovernor()\n        self.scanner = MarketScannerEngine(top_n=8)\n        self.scan_task = None'
);

// We need to move mock_risk definition BEFORE target_exposure in handle_market_event
const oldRiskMock = `        # 5. Risk Governor Validation
        # In a real environment, RiskSnapshot is maintained continuously by an account sync task
        mock_risk = RiskSnapshot(
            portfolio_equity=Decimal("100000.0"),
            unrealized_pnl=Decimal("0.0"),
            realized_pnl_24h=Decimal("0.0"),
            margin_utilization_pct=Decimal("5.0"),
            effective_leverage=Decimal("0.5"),
            current_drawdown_pct=Decimal("1.2"),
            liquidation_distance_pct=Decimal("45.0"),
            risk_state=RiskState.NORMAL
        )`;

const newRiskMock = `        # In a real environment, RiskSnapshot is maintained continuously by an account sync task
        mock_risk = RiskSnapshot(
            portfolio_equity=Decimal("100000.0"),
            unrealized_pnl=Decimal("0.0"),
            realized_pnl_24h=Decimal("0.0"),
            margin_utilization_pct=Decimal("5.0"),
            effective_leverage=Decimal("0.5"),
            current_drawdown_pct=Decimal("1.2"),
            liquidation_distance_pct=Decimal("45.0"),
            risk_state=RiskState.NORMAL
        )`;

code = code.replace(oldRiskMock, '');
code = code.replace('raw_target_exposure = self.meta_allocator.allocate(intents, event.symbol)', newRiskMock + '\n\n        raw_target_exposure = self.meta_allocator.allocate(intents, event.symbol)');


// Add scanner loop
const startFunc = `    async def start(self):
        logger.info("Initializing Blessing AI Trading Worker v0.2 (Paper/Live Mode)...")
        
        # Phase 5: Initial Market Scan
        self.symbols = await self.scanner.scan_active_symbols()
        
        logger.info("Connecting to Binance WS for: %s", self.symbols)
        self.ws_client = BinancePublicWebSocket(
            symbols=self.symbols,
            event_callback=self.handle_market_event,
        )
        
        # Phase 5: Background Scanner Task (Updates WS subscriptions if symbols change)
        self.scan_task = asyncio.create_task(self._periodic_scanner())
        
        logger.info("Worker Event Loop Running. Press Ctrl+C to terminate.")
        while self.is_running:
            await asyncio.sleep(1)

    async def _periodic_scanner(self):
        """Periodically scans market and updates WebSocket subscription if active pairs change."""
        while self.is_running:
            await asyncio.sleep(self.scanner.refresh_interval_sec)
            try:
                new_symbols = await self.scanner.scan_active_symbols()
                if set(new_symbols) != set(self.symbols):
                    logger.info("Market regime shifted. Active pairs updated: %s", new_symbols)
                    self.symbols = new_symbols
                    if self.ws_client:
                        # In production, call ws_client.subscribe(new_symbols) to dynamically update
                        pass
            except Exception as e:
                logger.error("Periodic scanner failed: %s", e)
`;

// Replace start()
const oldStartBlock = code.substring(code.indexOf('async def start(self):'), code.indexOf('def stop(self):'));
code = code.replace(oldStartBlock, startFunc + '    ');

fs.writeFileSync('apps/trading_worker/main.py', code);
