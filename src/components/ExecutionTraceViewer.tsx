import React from 'react';
import {
  Layers,
  ArrowRight,
  ShieldCheck,
  CheckCircle2,
  AlertTriangle,
  Info,
  ExternalLink,
  Clock,
  Zap,
  Check,
  Cpu,
  Boxes,
} from 'lucide-react';
import { DerivedOrder } from '../types/orders';

interface ExecutionTraceViewerProps {
  order: DerivedOrder | null;
  onClose?: () => void;
}

export const ExecutionTraceViewer: React.FC<ExecutionTraceViewerProps> = ({
  order,
  onClose,
}) => {
  if (!order) {
    return (
      <div className="bg-zinc-900/70 border border-zinc-800 rounded-2xl p-6 text-center space-y-2">
        <Layers className="w-8 h-8 text-zinc-600 mx-auto" />
        <h4 className="text-xs font-bold text-zinc-300 uppercase tracking-wider">
          No Order Selected for Traceability
        </h4>
        <p className="text-[11px] text-zinc-500 max-w-sm mx-auto">
          Click on any order in the table below to inspect its full deterministic decision chain from Strategy Intent to Exchange Fill.
        </p>
      </div>
    );
  }

  const { trace } = order;

  return (
    <div className="bg-zinc-900/90 border border-zinc-800 rounded-2xl p-5 space-y-4 shadow-xl shadow-black/20">
      {/* Header */}
      <div className="flex flex-wrap items-center justify-between gap-3 border-b border-zinc-800/80 pb-3">
        <div className="flex items-center space-x-2.5">
          <div className="p-1.5 rounded-lg bg-cyan-950/80 border border-cyan-800/60 text-cyan-400">
            <Zap className="w-4 h-4" />
          </div>
          <div>
            <div className="flex items-center space-x-2">
              <h3 className="text-xs font-bold text-zinc-100 uppercase tracking-wider font-mono">
                {order.clientOrderId}
              </h3>
              <span
                className={`px-1.5 py-0.2 rounded text-[9px] font-bold font-mono border ${
                  order.status === 'FILLED'
                    ? 'bg-emerald-950 text-emerald-300 border-emerald-800/80'
                    : 'bg-amber-950 text-amber-300 border-amber-800/80'
                }`}
              >
                {order.status}
              </span>
              <span className="px-1.5 py-0.2 rounded text-[9px] font-mono bg-zinc-800 text-zinc-400 border border-zinc-700">
                {trace.sourceClassification}
              </span>
            </div>
            <p className="text-[11px] text-zinc-400 mt-0.5">
              Basket: <span className="text-zinc-200 font-mono">{order.basketId}</span> • Venue:{' '}
              <span className="text-cyan-400 uppercase font-mono">{order.venue.replace('_', ' ')}</span>
            </p>
          </div>
        </div>

        {onClose && (
          <button
            type="button"
            onClick={onClose}
            className="text-xs text-zinc-400 hover:text-zinc-200 px-2.5 py-1 rounded bg-zinc-800 hover:bg-zinc-700 transition-colors cursor-pointer"
          >
            Clear Selection
          </button>
        )}
      </div>

      {/* 6-Stage Deterministic Traceability Pipeline */}
      <div className="grid grid-cols-1 md:grid-cols-6 gap-2 text-xs font-mono">
        {/* Stage 1: Strategy Intent */}
        <div className="p-3 bg-zinc-950/80 rounded-xl border border-zinc-800/80 space-y-1.5">
          <div className="flex items-center justify-between text-[10px] text-zinc-500 uppercase">
            <span>1. Intent</span>
            <Cpu className="w-3 h-3 text-cyan-400" />
          </div>
          <div className="font-bold text-zinc-200 truncate">{order.strategy}</div>
          <div className="text-[10px] text-zinc-400">{trace.strategyIntent}</div>
        </div>

        {/* Stage 2: Opportunity Score */}
        <div className="p-3 bg-zinc-950/80 rounded-xl border border-zinc-800/80 space-y-1.5">
          <div className="flex items-center justify-between text-[10px] text-zinc-500 uppercase">
            <span>2. Score</span>
            <Layers className="w-3 h-3 text-indigo-400" />
          </div>
          <div className="font-bold text-indigo-300">
            {trace.opportunityScore.toFixed(1)} / 100
          </div>
          <div className="text-[10px] text-zinc-400">Regime Fit: Verified</div>
        </div>

        {/* Stage 3: Meta Allocator */}
        <div className="p-3 bg-zinc-950/80 rounded-xl border border-zinc-800/80 space-y-1.5">
          <div className="flex items-center justify-between text-[10px] text-zinc-500 uppercase">
            <span>3. Allocation</span>
            <Boxes className="w-3 h-3 text-cyan-400" />
          </div>
          <div className="font-bold text-cyan-300">
            {trace.metaBudgetFactor.toFixed(2)}x Budget
          </div>
          <div className="text-[10px] text-zinc-400">Factor Beta Scaled</div>
        </div>

        {/* Stage 4: Risk Governor */}
        <div className="p-3 bg-zinc-950/80 rounded-xl border border-emerald-900/40 space-y-1.5">
          <div className="flex items-center justify-between text-[10px] text-zinc-500 uppercase">
            <span>4. Governor</span>
            <ShieldCheck className="w-3 h-3 text-emerald-400" />
          </div>
          <div className="font-bold text-emerald-400 flex items-center space-x-1">
            <CheckCircle2 className="w-3 h-3" />
            <span>APPROVED</span>
          </div>
          <div className="text-[10px] text-zinc-400 truncate" title={trace.governorRule}>
            {trace.governorRule}
          </div>
        </div>

        {/* Stage 5: Target Exposure */}
        <div className="p-3 bg-zinc-950/80 rounded-xl border border-zinc-800/80 space-y-1.5">
          <div className="flex items-center justify-between text-[10px] text-zinc-500 uppercase">
            <span>5. Target</span>
            <Layers className="w-3 h-3 text-amber-400" />
          </div>
          <div className="font-bold text-amber-300 truncate">
            {order.side} {order.size} {order.symbol.replace('USDT', '')}
          </div>
          <div className="text-[10px] text-zinc-400">
            ${Math.round(order.valueUsd).toLocaleString()} Notional
          </div>
        </div>

        {/* Stage 6: Binance Execution */}
        <div className="p-3 bg-zinc-950/80 rounded-xl border border-zinc-800/80 space-y-1.5">
          <div className="flex items-center justify-between text-[10px] text-zinc-500 uppercase">
            <span>6. Exchange</span>
            <Check className="w-3 h-3 text-emerald-400" />
          </div>
          <div className="font-bold text-zinc-200 truncate">{order.type}</div>
          <div className="text-[10px] text-zinc-400">
            Fee: {trace.feeTier} • {trace.slippageBps} bps slip
          </div>
        </div>
      </div>

      {/* Contract Gap & Classification Disclosure */}
      <div className="p-3 rounded-xl bg-zinc-950 border border-zinc-800/80 flex flex-wrap items-center justify-between gap-2 text-xs">
        <div className="flex items-center space-x-2 text-zinc-400">
          <Info className="w-3.5 h-3.5 text-cyan-400 shrink-0" />
          <span>
            Traceability metadata: Stages 1–3 use <strong className="text-zinc-200">DERIVED_FRONTEND</strong> from active basket models; Stages 4–6 map directly to <strong className="text-zinc-200">EXISTING</strong> /api/quant/state orders.
          </span>
        </div>
        <span className="text-[11px] font-mono text-zinc-500">
          Backend Contract Gap: Epic UI-06
        </span>
      </div>
    </div>
  );
};
