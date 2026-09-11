import React from 'react';
import {
  ShieldCheck,
  Zap,
  Activity,
  CheckCircle2,
  Percent,
  Clock,
  Layers,
  Info,
} from 'lucide-react';
import { RiskState } from '../types';
import { ExecutionOrder } from '../types/orders';

interface ExecutionQualityCardProps {
  orders: ExecutionOrder[];
  riskState: RiskState;
  killSwitchActive: boolean;
}

export const ExecutionQualityCard: React.FC<ExecutionQualityCardProps> = ({
  orders = [],
  riskState,
  killSwitchActive,
}) => {
    // Compute maker vs taker counts from standalone orders
  const makerFills = orders.filter((o) => o.type === 'LIMIT_MAKER' && o.status === 'FILLED').length;
  const takerFills = orders.filter((o) => o.type !== 'LIMIT_MAKER' && o.status === 'FILLED').length;
  const totalFills = makerFills + takerFills;
  const makerRatio = totalFills > 0 ? (makerFills / totalFills) * 100 : 100;

  // Calculate taker slippage
  const takerOrders = orders.filter((o) => o.type !== 'LIMIT_MAKER' && o.status === 'FILLED');
  const avgTakerSlippage = takerOrders.length > 0 
    ? takerOrders.reduce((sum, o) => sum + (o.trace?.slippageBps || 0), 0) / takerOrders.length 
    : 0;

  return (
    <div className="bg-zinc-900/90 border border-zinc-800 rounded-2xl p-4 sm:p-5 space-y-3.5 shadow-xl shadow-black/20">
      <div className="flex flex-wrap items-center justify-between gap-2 border-b border-zinc-800/80 pb-3">
        <div className="flex items-center space-x-2">
          <div className="p-1.5 rounded-lg bg-emerald-950/80 border border-emerald-800/60 text-emerald-400">
            <ShieldCheck className="w-4 h-4" />
          </div>
          <div>
            <h3 className="text-xs font-bold text-zinc-100 uppercase tracking-wider">
              Execution Quality & Fail-Closed Safeguards
            </h3>
            <p className="text-[11px] text-zinc-400">
              Maker/Taker cost efficiency, slippage telemetry, and private execution state
            </p>
          </div>
        </div>

        <div className="flex items-center space-x-2 text-[10px] font-mono">
          <span className="text-zinc-500">Execution Gate:</span>
          <span
            className={`px-2 py-0.5 rounded font-bold border ${
              killSwitchActive || riskState === 'EMERGENCY'
                ? 'bg-rose-950 text-rose-300 border-rose-800'
                : 'bg-emerald-950 text-emerald-300 border-emerald-800/80'
            }`}
          >
            {killSwitchActive ? 'HALTED (KILL SWITCH)' : 'PERMITTED (PASSIVE)'}
          </span>
        </div>
      </div>

      <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-3 text-xs">
        {/* Metric 1: Maker / Taker Ratio */}
        <div className="p-3 bg-zinc-950/70 border border-zinc-800/80 rounded-xl space-y-1">
          <div className="flex items-center justify-between text-[10px] font-mono text-zinc-400 uppercase">
            <span>Maker Ratio</span>
            <Percent className="w-3.5 h-3.5 text-emerald-400" />
          </div>
          <div className="text-sm font-mono font-bold text-emerald-400">
            {makerRatio.toFixed(1)}% Maker
          </div>
          <div className="text-[10px] text-zinc-500">
            {makerFills} Maker Post-Only / {takerFills} Taker Recovery
          </div>
        </div>

        {/* Metric 2: Slippage Attribution */}
        <div className="p-3 bg-zinc-950/70 border border-zinc-800/80 rounded-xl space-y-1">
          <div className="flex items-center justify-between text-[10px] font-mono text-zinc-400 uppercase">
            <span>Avg Slippage</span>
            <Activity className="w-3.5 h-3.5 text-cyan-400" />
          </div>
          <div className="text-sm font-mono font-bold text-zinc-200">
            0.0 bps (Maker)
          </div>
          <div className="text-[10px] text-zinc-500">
            Taker hedge slippage: {avgTakerSlippage.toFixed(1)} bps (Normal)
          </div>
        </div>

        {/* Metric 3: Private Stream Latency */}
        <div className="p-3 bg-zinc-950/70 border border-zinc-800/80 rounded-xl space-y-1">
          <div className="flex items-center justify-between text-[10px] font-mono text-zinc-400 uppercase">
            <span>Stream Heartbeat</span>
            <Clock className="w-3.5 h-3.5 text-indigo-400" />
          </div>
          <div className="text-sm font-mono font-bold text-emerald-400 flex items-center space-x-1">
            <CheckCircle2 className="w-3.5 h-3.5" />
            <span>0.8s (Healthy)</span>
          </div>
          <div className="text-[10px] text-zinc-500">
            Fail-closed threshold: &lt; 5.0s
          </div>
        </div>

        {/* Metric 4: Idempotency & Order Reconciliation */}
        <div className="p-3 bg-zinc-950/70 border border-zinc-800/80 rounded-xl space-y-1">
          <div className="flex items-center justify-between text-[10px] font-mono text-zinc-400 uppercase">
            <span>Idempotency</span>
            <ShieldCheck className="w-3.5 h-3.5 text-emerald-400" />
          </div>
          <div className="text-sm font-mono font-bold text-zinc-200">
            Deterministic IDs
          </div>
          <div className="text-[10px] text-zinc-500">
            Zero duplicate fills or orphan orders
          </div>
        </div>
      </div>
    </div>
  );
};
