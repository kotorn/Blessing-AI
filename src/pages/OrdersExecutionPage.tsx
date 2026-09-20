import React, { useState } from 'react';
import {
  Layers,
} from 'lucide-react';
import { AccountData, TradingSystemState } from '../types';
import { ExecutionOrder } from '../types/orders';
import { OrdersTable } from '../components/OrdersTable';
import { ExecutionTraceViewer } from '../components/ExecutionTraceViewer';
import { ExecutionQualityCard } from '../components/ExecutionQualityCard';

interface OrdersExecutionPageProps {
  account: AccountData;
  orders: ExecutionOrder[];
  systemState: TradingSystemState | null;
}

export const OrdersExecutionPage: React.FC<OrdersExecutionPageProps> = ({
  account,
  orders,
  systemState,
}) => {
  const [selectedOrder, setSelectedOrder] = useState<ExecutionOrder | null>(null);
  const executionLabel =
    systemState?.executionMode === 'TESTNET'
      ? systemState.workerResponsive === true && systemState.reconciliationStatus === 'IN_SYNC'
        ? 'Binance USDⓈ-M Testnet / Worker-authorized'
        : 'Binance USDⓈ-M Testnet / UNVERIFIED'
      : systemState?.executionMode === 'PAPER'
        ? 'PAPER / SIMULATED'
        : 'UNKNOWN / FAIL-CLOSED';

  return (
    <div className="space-y-6">
      {/* Page Header */}
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h2 className="text-xl font-bold text-zinc-100 flex items-center space-x-2">
            <Layers className="w-5 h-5 text-cyan-400" />
            <span>Orders & Execution Traceability Pipeline</span>
          </h2>
          <p className="text-xs text-zinc-400 mt-1">
            Deterministic execution chain: Strategy Intent ➔ Opportunity Score ➔ Meta Allocation ➔ Risk Governor ➔ Target Exposure ➔ Binance Order ➔ Exchange Fill.
          </p>
        </div>

        <div className="flex items-center space-x-2">
          <span className="px-2.5 py-1 rounded text-xs font-mono font-bold bg-zinc-900 border border-zinc-700 text-zinc-300">
            {executionLabel}
          </span>
        </div>
      </div>

      {/* Execution Quality & Fail-Closed Safeguards */}
      <ExecutionQualityCard
        account={account}
        orders={orders}
        riskState={account.risk_state}
        killSwitchActive={account.kill_switch_active}
      />

      {/* Selected Order Traceability Chain Viewer (UI-06C) */}
      <ExecutionTraceViewer
        order={selectedOrder}
        onClose={selectedOrder ? () => setSelectedOrder(null) : undefined}
      />

      {/* Orders & Fills Table (UI-06A) */}
      <OrdersTable
        orders={orders}
        selectedOrderId={selectedOrder?.id || null}
        onSelectOrder={(ord) => setSelectedOrder(ord)}
      />
    </div>
  );
};
