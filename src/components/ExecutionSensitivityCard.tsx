import React from 'react';
import {
  Zap,
  TrendingDown,
  AlertTriangle,
  Scale,
  DollarSign,
  Activity,
  CheckCircle2,
  XCircle,
  Info,
} from 'lucide-react';
import { ExecutionFrictionAnalysis } from '../types/research';
import { IllustrativeEvidenceBanner } from './IllustrativeEvidenceBanner';

export const ExecutionSensitivityCard: React.FC = () => {
  const analysis: ExecutionFrictionAnalysis = {
    naiveCandleCloseReturnPct: 32.4,
    realisticMicrostructureReturnPct: 18.4,
    unmodeledFrictionGapPct: 14.0,
    frictionBreakdown: {
      makerTakerFeeDragPct: 5.8,
      fundingSettlementDragPct: 3.4,
      bidAskSpreadDragPct: 2.6,
      latencySlippageDragPct: 1.5,
      partialFillDecayPct: 0.7,
    },
  };

  return (
    <div className="bg-zinc-900/90 border border-zinc-800 rounded-2xl p-4 sm:p-5 space-y-4 shadow-xl shadow-black/20">
      {/* Header */}
      <div className="flex flex-wrap items-center justify-between gap-2 border-b border-zinc-800/80 pb-3">
        <div className="flex items-center space-x-2">
          <div className="p-1.5 rounded-lg bg-amber-950/80 border border-amber-800/60 text-amber-400">
            <Zap className="w-4 h-4" />
          </div>
          <div>
            <h3 className="text-xs font-bold text-zinc-100 uppercase tracking-wider">
              Execution Sensitivity & Friction Realism Gap
            </h3>
            <p className="text-[11px] text-zinc-400">
              Candle-close-only simulations are strictly rejected; models trade events, spread, partial fills, and latency
            </p>
          </div>
        </div>

        <div className="flex items-center space-x-2 font-mono text-[10px]">
          <span className="text-zinc-500">Simulation Model:</span>
          <span className="px-2 py-0.5 rounded font-bold bg-amber-950 text-amber-300 border border-amber-800/80">
            TICK-LEVEL EVENT SIMULATOR
          </span>
        </div>
      </div>

      <IllustrativeEvidenceBanner message="The friction comparison is a static example until a verified backtest/testnet evidence artifact is attached." />

      {/* Comparison: Naive vs Realistic */}
      <div className="grid grid-cols-1 md:grid-cols-3 gap-3 text-xs font-mono">
        {/* Naive Candle Close */}
        <div className="p-3.5 bg-zinc-950/80 border border-rose-900/40 rounded-xl space-y-1.5">
          <div className="flex items-center justify-between text-[10px] text-zinc-400 uppercase">
            <span>Naive Candle-Close</span>
            <XCircle className="w-3.5 h-3.5 text-rose-500" />
          </div>
          <div className="text-lg font-bold text-zinc-300">
            +{analysis.naiveCandleCloseReturnPct.toFixed(1)}% ROI
          </div>
          <div className="text-[10px] text-rose-400 font-sans">
            Unrealistic assumption of zero slippage & perfect fill price
          </div>
        </div>

        {/* Realistic Tick Replay */}
        <div className="p-3.5 bg-zinc-950/80 border border-emerald-800/60 rounded-xl space-y-1.5">
          <div className="flex items-center justify-between text-[10px] text-zinc-400 uppercase">
            <span>Realistic Tick Replay</span>
            <CheckCircle2 className="w-3.5 h-3.5 text-emerald-400" />
          </div>
          <div className="text-lg font-bold text-emerald-400">
            +{analysis.realisticMicrostructureReturnPct.toFixed(1)}% ROI
          </div>
          <div className="text-[10px] text-zinc-400 font-sans">
            Static illustrative fixture; no current-build artifact is attached
          </div>
        </div>

        {/* Unmodeled Friction Gap */}
        <div className="p-3.5 bg-zinc-950/80 border border-amber-800/60 rounded-xl space-y-1.5">
          <div className="flex items-center justify-between text-[10px] text-zinc-400 uppercase">
            <span>Friction Drag Gap</span>
            <TrendingDown className="w-3.5 h-3.5 text-amber-400" />
          </div>
          <div className="text-lg font-bold text-amber-400">
            -{analysis.unmodeledFrictionGapPct.toFixed(1)}% Drag
          </div>
          <div className="text-[10px] text-zinc-400 font-sans">
            The difference between paper illusions and real market PnL
          </div>
        </div>
      </div>

      {/* Breakdown of Friction Elements */}
      <div className="space-y-2">
        <div className="text-[10px] font-mono uppercase text-zinc-400 font-bold">
          Microstructure Friction Attribution Breakdown
        </div>
        <div className="grid grid-cols-2 sm:grid-cols-5 gap-2 text-xs font-mono">
          <div className="p-2.5 bg-zinc-950 rounded-lg border border-zinc-800/80 space-y-1">
            <div className="text-[10px] text-zinc-500">Maker/Taker Fees</div>
            <div className="font-bold text-rose-400">-{analysis.frictionBreakdown.makerTakerFeeDragPct.toFixed(1)}%</div>
            <div className="text-[9px] text-zinc-400 font-sans">Binance VIP0 tier</div>
          </div>
          <div className="p-2.5 bg-zinc-950 rounded-lg border border-zinc-800/80 space-y-1">
            <div className="text-[10px] text-zinc-500">Funding Rate Drag</div>
            <div className="font-bold text-rose-400">-{analysis.frictionBreakdown.fundingSettlementDragPct.toFixed(1)}%</div>
            <div className="text-[9px] text-zinc-400 font-sans">8h settlement cycle</div>
          </div>
          <div className="p-2.5 bg-zinc-950 rounded-lg border border-zinc-800/80 space-y-1">
            <div className="text-[10px] text-zinc-500">Bid-Ask Spread</div>
            <div className="font-bold text-amber-400">-{analysis.frictionBreakdown.bidAskSpreadDragPct.toFixed(1)}%</div>
            <div className="text-[9px] text-zinc-400 font-sans">Crossing book edge</div>
          </div>
          <div className="p-2.5 bg-zinc-950 rounded-lg border border-zinc-800/80 space-y-1">
            <div className="text-[10px] text-zinc-500">Latency Slippage</div>
            <div className="font-bold text-amber-400">-{analysis.frictionBreakdown.latencySlippageDragPct.toFixed(1)}%</div>
            <div className="text-[9px] text-zinc-400 font-sans">50-150ms queue shift</div>
          </div>
          <div className="p-2.5 bg-zinc-950 rounded-lg border border-zinc-800/80 space-y-1">
            <div className="text-[10px] text-zinc-500">Partial Fill Decay</div>
            <div className="font-bold text-zinc-300">-{analysis.frictionBreakdown.partialFillDecayPct.toFixed(1)}%</div>
            <div className="text-[9px] text-zinc-400 font-sans">Unfilled limit quotes</div>
          </div>
        </div>
      </div>
    </div>
  );
};
