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
import { PaperExecutionAdapter } from './paper';
import { BinanceTestnetExecutionAdapter } from './binanceTestnet';
import { TradingSystemState } from '../types';
import { executionLedger } from './ledger';

class ExecutionManager {
  private adapters: Map<string, ExecutionAdapter> = new Map();
  
  constructor() {
    this.adapters.set('PAPER', new PaperExecutionAdapter());
  }

  setTestnetCredentials(apiKey: string, apiSecret: string) {
    const adapter = new BinanceTestnetExecutionAdapter(apiKey, apiSecret, {
      onOrderUpdate: (order) => {
        executionLedger.updateOrder(order.id, order);
      },
      onPositionUpdate: (positions) => {
        executionLedger.reconcilePositions(positions);
      }
    });
    adapter.startUserStream();
    this.adapters.set('TESTNET', adapter);
  }

  private getAdapter(state: TradingSystemState): ExecutionAdapter {
    const adapter = this.adapters.get(state.executionMode);
    if (!adapter) {
      throw new Error(`No execution adapter configured for mode: ${state.executionMode}`);
    }
    return adapter;
  }

  async placeOrder(state: TradingSystemState, request: OrderPlacementRequest): Promise<OrderPlacementResult> {
    const adapter = this.getAdapter(state);
    const result = await adapter.placeOrder(request);
    if (result.success && result.orderId) {
       executionLedger.recordOrder({
         id: result.orderId,
         clientOrderId: result.clientOrderId || '',
         symbol: request.symbol,
         side: request.side,
         type: request.type,
         price: request.price || 0,
         size: request.size,
         filledSize: result.filledSize || 0,
         status: result.status || 'NEW',
         createdAt: new Date().toISOString(),
         source: state.executionMode === 'TESTNET' ? 'BINANCE_TESTNET' : 'SIMULATED'
       });
    }
    return result;
  }
  
  async cancelOrder(state: TradingSystemState, request: OrderCancellationRequest): Promise<OrderCancellationResult> {
    const adapter = this.getAdapter(state);
    return await adapter.cancelOrder(request);
  }

  async reconcile(state: TradingSystemState): Promise<ReconciliationResult> {
    const adapter = this.getAdapter(state);
    const result = await adapter.reconcile();
    if (result.status === 'IN_SYNC') {
       const orders = await adapter.getOpenOrders();
       const positions = await adapter.getPositions();
       executionLedger.reconcileOrders(orders);
       executionLedger.reconcilePositions(positions);
    }
    return result;
  }

  async getOpenOrders(state: TradingSystemState): Promise<ExchangeOrder[]> {
    const adapter = this.getAdapter(state);
    return await adapter.getOpenOrders();
  }

  async getPositions(state: TradingSystemState): Promise<ExchangePosition[]> {
    const adapter = this.getAdapter(state);
    return await adapter.getPositions();
  }
  
  async cancelReplaceOrder(state: TradingSystemState, cancelRequest: OrderCancellationRequest, placeRequest: OrderPlacementRequest): Promise<OrderPlacementResult> {
    const adapter = this.getAdapter(state);
    if (state.executionMode === 'TESTNET') {
       return await (adapter as BinanceTestnetExecutionAdapter).cancelReplaceOrder(cancelRequest, placeRequest);
    } else {
       // Paper adapter fallback
       await adapter.cancelOrder(cancelRequest);
       return await adapter.placeOrder(placeRequest);
    }
  }
}

export const executionManager = new ExecutionManager();
