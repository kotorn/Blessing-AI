import React, { useState } from 'react';
import {
  Layers,
  ArrowRight,
  Clock,
  AlertCircle,
  Cpu,
  ShieldCheck,
  Zap,
  CheckCircle2,
} from 'lucide-react';
import { AccountData } from '../types';
import { ExecutionOrder } from '../types/orders';
import { OrdersTable } from '../components/OrdersTable';
import { ExecutionTraceViewer } from '../components/ExecutionTraceViewer';
import { ExecutionQualityCard } from '../components/ExecutionQualityCard';

interface OrdersExecutionPageProps {
  account: AccountData;
  orders: ExecutionOrder[];
}

export const OrdersExecutionPage: React.FC<OrdersExecutionPageProps> = ({
  account,
  orders,
}) => {
  const [selectedOrder, setSelectedOrder] = useState<ExecutionOrder | null>(null);

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
            Binance Global USDⓈ-M & Spot
          </span>
        </div>
      </div>

      {/* Execution Quality & Fail-Closed Safeguards */}
      <ExecutionQualityCard
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
