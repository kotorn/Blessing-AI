import crypto from 'crypto';
import WebSocket from 'ws';
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

export type AdapterEvents = {
  onOrderUpdate?: (order: ExchangeOrder) => void;
  onPositionUpdate?: (positions: ExchangePosition[]) => void;
};

export class BinanceTestnetExecutionAdapter implements ExecutionAdapter {
  private events: AdapterEvents;
  private apiKey: string;
  private apiSecret: string;
  private futuresBase = 'https://testnet.binancefuture.com';
  private wsBase = 'wss://stream.binancefuture.com/ws';
  
  private listenKey: string | null = null;
  private ws: WebSocket | null = null;
  private keepAliveInterval: NodeJS.Timeout | null = null;
  
  private activeOrders: Map<string, ExchangeOrder> = new Map();
  private activePositions: Map<string, ExchangePosition> = new Map();
  
  private isReconciling = false;

  constructor(apiKey: string, apiSecret: string, events: AdapterEvents = {}) {
    this.events = events;
    this.apiKey = apiKey;
    this.apiSecret = apiSecret;
  }

  async getCapabilities(): Promise<ExecutionCapabilities> {
    return {
      paper: false,
      testnet: true,
      live: false,
      spot: true,
      usdmFutures: true,
      hedgeModeSupported: true,
      liveExecutionReady: true
    };
  }
  
  private sign(queryStr: string): string {
    return crypto.createHmac('sha256', this.apiSecret).update(queryStr).digest('hex');
  }

  private async request(method: string, endpoint: string, params: Record<string, any> = {}): Promise<any> {
    const ts = Date.now();
    const queryObj = { ...params, timestamp: ts };
    const queryStr = Object.entries(queryObj)
      .map(([k, v]) => `${k}=${encodeURIComponent(String(v))}`)
      .join('&');
      
    const signature = this.sign(queryStr);
    const fullQuery = `${queryStr}&signature=${signature}`;
    
    const url = `${this.futuresBase}${endpoint}?${fullQuery}`;
    
    try {
      const response = await fetch(url, {
        method,
        headers: {
          'X-MBX-APIKEY': this.apiKey,
          'Content-Type': 'application/json'
        }
      });
      
      const data = await response.json();
      if (!response.ok) {
        throw new Error(`Binance API error: ${data.msg} (code: ${data.code})`);
      }
      return data;
    } catch (err: any) {
      console.error(`[Testnet Adapter] Request Failed: ${endpoint} - ${err.message}`);
      throw err;
    }
  }

  async startUserStream() {
    try {
      const response = await fetch(`${this.futuresBase}/fapi/v1/listenKey`, {
        method: 'POST',
        headers: { 'X-MBX-APIKEY': this.apiKey }
      });
      const data = await response.json();
      this.listenKey = data.listenKey;
      
      this.ws = new WebSocket(`${this.wsBase}/${this.listenKey}`);
      
      this.ws.on('open', () => {
        console.log('[Testnet Adapter] User stream connected');
      });
      
      this.ws.on('message', (msg: string) => {
        const payload = JSON.parse(msg);
        this.handleStreamMessage(payload);
      });
      
      this.ws.on('close', () => {
        console.log('[Testnet Adapter] User stream disconnected, reconnecting...');
        setTimeout(() => this.startUserStream(), 5000);
      });
      
      if (this.keepAliveInterval) clearInterval(this.keepAliveInterval);
      this.keepAliveInterval = setInterval(async () => {
        await fetch(`${this.futuresBase}/fapi/v1/listenKey`, {
          method: 'PUT',
          headers: { 'X-MBX-APIKEY': this.apiKey }
        });
      }, 30 * 60 * 1000);
    } catch (err) {
      console.error('[Testnet Adapter] Failed to start user stream', err);
    }
  }
  
  private handleStreamMessage(payload: any) {
    if (payload.e === 'ORDER_TRADE_UPDATE') {
      const o = payload.o;
      const order: ExchangeOrder = {
        id: o.i.toString(),
        clientOrderId: o.c,
        symbol: o.s,
        side: o.S,
        type: o.o,
        price: parseFloat(o.p),
        size: parseFloat(o.q),
        filledSize: parseFloat(o.z),
        status: o.X,
        createdAt: new Date(o.T).toISOString(),
        source: 'BINANCE_TESTNET'
      };
      
      this.activeOrders.set(order.id, order);
      if (this.events.onOrderUpdate) this.events.onOrderUpdate(order);
      
      if (['FILLED', 'CANCELED', 'REJECTED', 'EXPIRED'].includes(order.status)) {
        // optionally remove from active, but keeping it for a bit might be useful
      }
    }
    
    if (payload.e === 'ACCOUNT_UPDATE') {
      // update positions
      const positions = payload.a.P;
      for (const pos of positions) {
        const size = parseFloat(pos.pa);
        if (size === 0) {
          this.activePositions.delete(pos.s);
        } else {
          this.activePositions.set(pos.s, {
            symbol: pos.s,
            side: size > 0 ? 'LONG' : 'SHORT', // Assuming One-way mode
            size: Math.abs(size),
            entryPrice: parseFloat(pos.ep),
            unrealizedPnl: parseFloat(pos.up),
            leverage: 1 // Need real leverage from account info
          });
        }
      }
    }
  }

  async reconcile(): Promise<ReconciliationResult> {
    if (this.isReconciling) return { status: 'UNKNOWN', timestamp: new Date().toISOString() };
    this.isReconciling = true;
    
    try {
      // Fetch open orders
      const openOrders = await this.request('GET', '/fapi/v1/openOrders');
      this.activeOrders.clear();
      for (const o of openOrders) {
        this.activeOrders.set(o.orderId.toString(), {
          id: o.orderId.toString(),
          clientOrderId: o.clientOrderId,
          symbol: o.symbol,
          side: o.side,
          type: o.type,
          price: parseFloat(o.price),
          size: parseFloat(o.origQty),
          filledSize: parseFloat(o.executedQty),
          status: o.status,
          createdAt: new Date(o.time).toISOString(),
          source: 'BINANCE_TESTNET'
        });
      }
      
      // Fetch positions
      const accountInfo = await this.request('GET', '/fapi/v2/positionRisk');
      this.activePositions.clear();
      for (const pos of accountInfo) {
        const size = parseFloat(pos.positionAmt);
        if (size !== 0) {
          this.activePositions.set(pos.symbol, {
            symbol: pos.symbol,
            side: size > 0 ? 'LONG' : 'SHORT', // Assumes one-way for simplicity unless hedge mapped
            size: Math.abs(size),
            entryPrice: parseFloat(pos.entryPrice),
            unrealizedPnl: parseFloat(pos.unRealizedProfit),
            leverage: parseFloat(pos.leverage)
          });
        }
      }
      
      this.isReconciling = false;
      return { status: 'IN_SYNC', timestamp: new Date().toISOString() };
    } catch (err) {
      this.isReconciling = false;
      console.error('[Testnet Adapter] Reconciliation failed', err);
      return { status: 'MISMATCH', timestamp: new Date().toISOString() };
    }
  }

  async getOpenOrders(): Promise<ExchangeOrder[]> {
    return Array.from(this.activeOrders.values());
  }

  async getPositions(): Promise<ExchangePosition[]> {
    return Array.from(this.activePositions.values());
  }

  private generateClientId(): string {
    return `BAI-${crypto.randomUUID().replace(/-/g, '').substring(0, 28)}`;
  }

  async placeOrder(request: OrderPlacementRequest): Promise<OrderPlacementResult> {
    const clientOrderId = this.generateClientId();
    const params: any = {
      symbol: request.symbol,
      side: request.side,
      type: request.type,
      quantity: request.size,
      newClientOrderId: clientOrderId
    };
    
    if (['LIMIT', 'STOP', 'TAKE_PROFIT', 'LIMIT_MAKER'].includes(request.type) && request.price) {
      params.price = request.price;
      if (request.type !== 'LIMIT_MAKER') {
        params.timeInForce = 'GTC';
      }
    }
    
    if (request.reduceOnly) {
      params.reduceOnly = 'true';
    }

    try {
      const result = await this.request('POST', '/fapi/v1/order', params);
      
      // Idempotency / timeout ambiguity handling
      // If we got a response, it was successfully placed
      return {
        success: true,
        orderId: result.orderId.toString(),
        clientOrderId: result.clientOrderId,
        status: result.status,
        filledSize: parseFloat(result.executedQty),
        averagePrice: parseFloat(result.avgPrice)
      };
    } catch (err: any) {
      // Ambiguity Check: Did we timeout but order still went through?
      if (err.message.includes('timeout') || err.message.includes('network')) {
        // REST Reconciliation for ambiguous order
        try {
           const check = await this.request('GET', '/fapi/v1/order', {
             symbol: request.symbol,
             origClientOrderId: clientOrderId
           });
           
           if (check && check.orderId) {
              return {
                success: true,
                orderId: check.orderId.toString(),
                clientOrderId: check.clientOrderId,
                status: check.status,
                filledSize: parseFloat(check.executedQty),
                averagePrice: parseFloat(check.avgPrice)
              };
           }
        } catch (checkErr) {
           console.error('[Testnet Adapter] Ambiguity resolution failed', checkErr);
        }
      }
      
      return {
        success: false,
        error: err.message
      };
    }
  }

  async cancelReplaceOrder(cancelRequest: OrderCancellationRequest, placeRequest: OrderPlacementRequest): Promise<OrderPlacementResult> {
    const cancelClientOrderId = cancelRequest.clientOrderId;
    const cancelOrderId = cancelRequest.orderId;
    
    const newClientOrderId = this.generateClientId();
    
    const params: any = {
      symbol: placeRequest.symbol,
      side: placeRequest.side,
      type: placeRequest.type,
      cancelReplaceMode: 'STOP_ON_FAILURE',
      quantity: placeRequest.size,
      newClientOrderId
    };
    
    if (cancelOrderId) params.cancelOrderId = cancelOrderId;
    else if (cancelClientOrderId) params.cancelOrigClientOrderId = cancelClientOrderId;
    else return { success: false, error: 'Must provide orderId or clientOrderId to cancel' };

    if (['LIMIT', 'STOP', 'TAKE_PROFIT', 'LIMIT_MAKER'].includes(placeRequest.type) && placeRequest.price) {
      params.price = placeRequest.price;
      if (placeRequest.type !== 'LIMIT_MAKER') {
        params.timeInForce = 'GTC';
      }
    }
    
    try {
      const result = await this.request('POST', '/fapi/v1/order/cancelReplace', params);
      
      return {
        success: true,
        orderId: result.newOrderResponse?.orderId?.toString(),
        clientOrderId: result.newOrderResponse?.clientOrderId,
        status: result.newOrderResponse?.status,
        filledSize: result.newOrderResponse ? parseFloat(result.newOrderResponse.executedQty) : 0,
        averagePrice: result.newOrderResponse ? parseFloat(result.newOrderResponse.avgPrice) : 0
      };
    } catch (err: any) {
      return { success: false, error: err.message };
    }
  }

  async cancelOrder(request: OrderCancellationRequest): Promise<OrderCancellationResult> {
    const params: any = {
      symbol: request.symbol
    };
    
    if (request.orderId) {
      params.orderId = request.orderId;
    } else if (request.clientOrderId) {
      params.origClientOrderId = request.clientOrderId;
    } else {
      return { success: false, error: 'Must provide orderId or clientOrderId' };
    }
    
    try {
      const result = await this.request('DELETE', '/fapi/v1/order', params);
      return {
        success: true,
        status: result.status
      };
    } catch (err: any) {
      return {
        success: false,
        error: err.message
      };
    }
  }
}
