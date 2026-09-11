const fs = require('fs');
let code = fs.readFileSync('src/backend/execution/binanceTestnet.ts', 'utf-8');

const regex = /async cancelOrder\(request: OrderCancellationRequest\): Promise<OrderCancellationResult> \{/;

const replaceCode = `async cancelReplaceOrder(cancelRequest: OrderCancellationRequest, placeRequest: OrderPlacementRequest): Promise<OrderPlacementResult> {
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

  async cancelOrder(request: OrderCancellationRequest): Promise<OrderCancellationResult> {`;

code = code.replace(regex, replaceCode);

fs.writeFileSync('src/backend/execution/binanceTestnet.ts', code);
