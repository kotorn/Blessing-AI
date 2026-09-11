import { 
  ExecutionAdapter, 
  OrderPlacementRequest, 
  OrderPlacementResult, 
  OrderCancellationRequest, 
  OrderCancellationResult,
  ExchangeOrder,
  ExchangePosition,
  ReconciliationResult
} from './adapter';
import { ExecutionCapabilities } from '../types';

export class PaperExecutionAdapter implements ExecutionAdapter {
  private openOrders: ExchangeOrder[] = [];
  private positions: Map<string, ExchangePosition> = new Map();
  private orderCounter = 0;

  async getCapabilities(): Promise<ExecutionCapabilities> {
    return {
      paper: true,
      testnet: false,
      live: false,
      spot: true,
      usdmFutures: true,
      hedgeModeSupported: true,
      liveExecutionReady: true
    };
  }

  async reconcile(): Promise<ReconciliationResult> {
    return {
      status: 'IN_SYNC',
      timestamp: new Date().toISOString()
    };
  }

  async getOpenOrders(): Promise<ExchangeOrder[]> {
    return [...this.openOrders];
  }

  async getPositions(): Promise<ExchangePosition[]> {
    return Array.from(this.positions.values());
  }

  async placeOrder(request: OrderPlacementRequest): Promise<OrderPlacementResult> {
    this.orderCounter++;
    const orderId = `PAPER-ORD-${Date.now()}-${this.orderCounter}`;
    const clientOrderId = `PAPER-CL-${Date.now()}`;
    
    // Simulate immediate fill for MARKET, or add to openOrders for LIMIT
    if (request.type === 'MARKET') {
       return {
         success: true,
         orderId,
         clientOrderId,
         status: 'FILLED',
         filledSize: request.size,
         averagePrice: request.price || 0 // Should fetch current market price in a real paper trader
       };
    } else {
       this.openOrders.push({
         id: orderId,
         clientOrderId,
         symbol: request.symbol,
         side: request.side,
         type: request.type,
         price: request.price || 0,
         size: request.size,
         filledSize: 0,
         status: 'NEW',
         createdAt: new Date().toISOString(),
         source: 'SIMULATED'
       });
       
       return {
         success: true,
         orderId,
         clientOrderId,
         status: 'NEW',
         filledSize: 0
       };
    }
  }

  async cancelOrder(request: OrderCancellationRequest): Promise<OrderCancellationResult> {
    const initialLength = this.openOrders.length;
    this.openOrders = this.openOrders.filter(o => 
      (request.orderId && o.id !== request.orderId) &&
      (request.clientOrderId && o.clientOrderId !== request.clientOrderId)
    );
    
    if (this.openOrders.length < initialLength) {
      return { success: true, status: 'CANCELED' };
    }
    return { success: false, error: 'Order not found' };
  }
}
