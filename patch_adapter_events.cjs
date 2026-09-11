const fs = require('fs');
let code = fs.readFileSync('src/backend/execution/binanceTestnet.ts', 'utf-8');

code = code.replace(
  /export class BinanceTestnetExecutionAdapter implements ExecutionAdapter \{/,
  `export type AdapterEvents = {
  onOrderUpdate?: (order: ExchangeOrder) => void;
  onPositionUpdate?: (positions: ExchangePosition[]) => void;
};

export class BinanceTestnetExecutionAdapter implements ExecutionAdapter {
  private events: AdapterEvents;`
);

code = code.replace(
  /constructor\(apiKey: string, apiSecret: string\) \{/,
  `constructor(apiKey: string, apiSecret: string, events: AdapterEvents = {}) {
    this.events = events;`
);

const streamRegex = /this\.activeOrders\.set\(order\.id, order\);/;
code = code.replace(streamRegex, `this.activeOrders.set(order.id, order);
      if (this.events.onOrderUpdate) this.events.onOrderUpdate(order);`);

const posRegex = /this\.activePositions\.set\(pos\.s, \{\n            symbol: pos\.s,\n            side: size > 0 \? 'LONG' : 'SHORT',\n            size: Math\.abs\(size\),\n            entryPrice: parseFloat\(pos\.ep\),\n            unrealizedPnl: parseFloat\(pos\.up\),\n            leverage: 1\n          \}\);\n        \}\n      \}/;
code = code.replace(posRegex, `this.activePositions.set(pos.s, {
            symbol: pos.s,
            side: size > 0 ? 'LONG' : 'SHORT',
            size: Math.abs(size),
            entryPrice: parseFloat(pos.ep),
            unrealizedPnl: parseFloat(pos.up),
            leverage: 1
          });
        }
      }
      if (this.events.onPositionUpdate) this.events.onPositionUpdate(Array.from(this.activePositions.values()));`);

fs.writeFileSync('src/backend/execution/binanceTestnet.ts', code);
