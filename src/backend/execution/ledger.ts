import { ExchangeOrder, ExchangePosition } from './adapter';

export interface LedgerEntry {
  id: string;
  timestamp: string;
  type: 'ORDER_PLACED' | 'ORDER_FILLED' | 'ORDER_CANCELED' | 'POSITION_UPDATED' | 'RECONCILIATION';
  payload: any;
}

class ExecutionLedger {
  private orders: Map<string, ExchangeOrder> = new Map();
  private positions: Map<string, ExchangePosition> = new Map();
  private entries: LedgerEntry[] = [];

  recordOrder(order: ExchangeOrder) {
    this.orders.set(order.id, order);
    this.appendEntry('ORDER_PLACED', order);
  }

  updateOrder(orderId: string, updates: Partial<ExchangeOrder>) {
    const existing = this.orders.get(orderId);
    if (existing) {
      const updated = { ...existing, ...updates };
      this.orders.set(orderId, updated);
      this.appendEntry(updates.status === 'FILLED' ? 'ORDER_FILLED' : 'ORDER_CANCELED', updated);
    }
  }
  
  updatePosition(position: ExchangePosition) {
    this.positions.set(position.symbol, position);
    this.appendEntry('POSITION_UPDATED', position);
  }
  
  reconcilePositions(positions: ExchangePosition[]) {
    this.positions.clear();
    for (const p of positions) {
      this.positions.set(p.symbol, p);
    }
    this.appendEntry('RECONCILIATION', { positions });
  }

  reconcileOrders(orders: ExchangeOrder[]) {
    this.orders.clear();
    for (const o of orders) {
      this.orders.set(o.id, o);
    }
    this.appendEntry('RECONCILIATION', { orders });
  }

  getOrders(): ExchangeOrder[] {
    return Array.from(this.orders.values());
  }

  getPositions(): ExchangePosition[] {
    return Array.from(this.positions.values());
  }
  
  getHistory(): LedgerEntry[] {
    return [...this.entries];
  }

  private appendEntry(type: LedgerEntry['type'], payload: any) {
    this.entries.push({
      id: `LEDGER-${Date.now()}-${Math.floor(Math.random() * 10000)}`,
      timestamp: new Date().toISOString(),
      type,
      payload
    });
  }
}

export const executionLedger = new ExecutionLedger();
