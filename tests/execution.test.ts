import { describe, it, expect, beforeEach } from 'vitest';
import { executionLedger } from '../src/backend/execution/ledger';
import { ExchangeOrder, ExchangePosition } from '../src/backend/execution/adapter';

describe('Execution Ledger', () => {
  beforeEach(() => {
    executionLedger.reconcileOrders([]);
    executionLedger.reconcilePositions([]);
  });

  it('records an order and maintains state', () => {
    const order: ExchangeOrder = {
      id: 'TEST-123',
      clientOrderId: 'CLI-123',
      symbol: 'BTCUSDT',
      side: 'BUY',
      type: 'MARKET',
      price: 0,
      size: 1,
      filledSize: 1,
      status: 'FILLED',
      createdAt: new Date().toISOString(),
      source: 'BINANCE_TESTNET'
    };

    executionLedger.recordOrder(order);
    
    const orders = executionLedger.getOrders();
    expect(orders.length).toBe(1);
    expect(orders[0].id).toBe('TEST-123');
    
    const history = executionLedger.getHistory();
    const placementEvent = history.find(e => e.type === 'ORDER_PLACED');
    expect(placementEvent).toBeDefined();
    expect(placementEvent?.payload.id).toBe('TEST-123');
  });

  it('updates an existing order', () => {
    const order: ExchangeOrder = {
      id: 'TEST-123',
      clientOrderId: 'CLI-123',
      symbol: 'BTCUSDT',
      side: 'BUY',
      type: 'LIMIT',
      price: 50000,
      size: 1,
      filledSize: 0,
      status: 'NEW',
      createdAt: new Date().toISOString(),
      source: 'BINANCE_TESTNET'
    };

    executionLedger.recordOrder(order);
    executionLedger.updateOrder('TEST-123', { status: 'FILLED', filledSize: 1 });
    
    const orders = executionLedger.getOrders();
    expect(orders[0].status).toBe('FILLED');
    expect(orders[0].filledSize).toBe(1);
    
    const history = executionLedger.getHistory();
    const fillEvent = history.find(e => e.type === 'ORDER_FILLED');
    expect(fillEvent).toBeDefined();
  });
  
  it('reconciles positions', () => {
     const pos: ExchangePosition = {
        symbol: 'BTCUSDT',
        side: 'LONG',
        size: 0.5,
        entryPrice: 50000,
        unrealizedPnl: 100,
        leverage: 1
     };
     
     executionLedger.reconcilePositions([pos]);
     const positions = executionLedger.getPositions();
     expect(positions.length).toBe(1);
     expect(positions[0].symbol).toBe('BTCUSDT');
  });
});
