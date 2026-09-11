import { ExecutionCapabilities } from '../types';

export interface OrderPlacementRequest {
  basketId: string;
  symbol: string;
  side: 'BUY' | 'SELL';
  type: 'MARKET' | 'LIMIT' | 'STOP_MARKET' | 'LIMIT_MAKER';
  price?: number;
  size: number;
  reduceOnly?: boolean;
}

export interface OrderPlacementResult {
  success: boolean;
  orderId?: string;
  clientOrderId?: string;
  status?: string;
  error?: string;
  filledSize?: number;
  averagePrice?: number;
}

export interface OrderCancellationRequest {
  symbol: string;
  orderId?: string;
  clientOrderId?: string;
}

export interface OrderCancellationResult {
  success: boolean;
  status?: string;
  error?: string;
}

export interface ExchangeOrder {
  id: string;
  clientOrderId: string;
  symbol: string;
  side: 'BUY' | 'SELL';
  type: string;
  price: number;
  size: number;
  filledSize: number;
  status: string;
  createdAt: string;
  source: string;
}

export interface ExchangePosition {
  symbol: string;
  side: 'LONG' | 'SHORT' | 'BOTH';
  size: number;
  entryPrice: number;
  unrealizedPnl: number;
  leverage: number;
}

export interface ReconciliationResult {
  status: 'IN_SYNC' | 'MISMATCH' | 'UNKNOWN';
  timestamp: string;
}

export interface ExecutionAdapter {
  getCapabilities(): Promise<ExecutionCapabilities>;
  reconcile(): Promise<ReconciliationResult>;
  getOpenOrders(): Promise<ExchangeOrder[]>;
  getPositions(): Promise<ExchangePosition[]>;
  placeOrder(request: OrderPlacementRequest): Promise<OrderPlacementResult>;
  cancelOrder(request: OrderCancellationRequest): Promise<OrderCancellationResult>;
}
