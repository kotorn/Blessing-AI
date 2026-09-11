const fs = require('fs');
let code = fs.readFileSync('src/backend/execution/manager.ts', 'utf-8');

code = code.replace(
  /import \{ TradingSystemState \} from '\.\.\/types';/,
  `import { TradingSystemState } from '../types';
import { executionLedger } from './ledger';`
);

code = code.replace(
  /const adapter = new BinanceTestnetExecutionAdapter\(apiKey, apiSecret\);/,
  `const adapter = new BinanceTestnetExecutionAdapter(apiKey, apiSecret, {
      onOrderUpdate: (order) => {
        executionLedger.updateOrder(order.id, order);
      },
      onPositionUpdate: (positions) => {
        executionLedger.reconcilePositions(positions);
      }
    });`
);

const placeRegex = /return await adapter\.placeOrder\(request\);/;
code = code.replace(placeRegex, `const result = await adapter.placeOrder(request);
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
    return result;`);

const recRegex = /return await adapter\.reconcile\(\);/;
code = code.replace(recRegex, `const result = await adapter.reconcile();
    if (result.status === 'IN_SYNC') {
       const orders = await adapter.getOpenOrders();
       const positions = await adapter.getPositions();
       executionLedger.reconcileOrders(orders);
       executionLedger.reconcilePositions(positions);
    }
    return result;`);

fs.writeFileSync('src/backend/execution/manager.ts', code);
