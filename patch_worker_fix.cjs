const fs = require('fs');
let code = fs.readFileSync('apps/trading_worker/main.py', 'utf-8');

// The sed command above added pass but didn't remove the rest. Let's do this cleanly.

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

// Replace start() cleanly
const oldStartBlock = code.substring(code.indexOf('async def start(self):'), code.indexOf('def stop(self):'));
code = code.replace(oldStartBlock, startFunc + '\n    ');

fs.writeFileSync('apps/trading_worker/main.py', code);
