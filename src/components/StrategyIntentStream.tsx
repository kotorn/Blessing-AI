import React from 'react';
import {
  Cpu,
} from 'lucide-react';
import { StrategyIntentItem } from '../types/strategy';
import { BasketItem, InstrumentData } from '../types';

interface StrategyIntentStreamProps {
  intentsData?: StrategyIntentItem[];
  baskets: BasketItem[];
  instruments: Record<string, InstrumentData>;
}

export const StrategyIntentStream: React.FC<StrategyIntentStreamProps> = ({
  intentsData,
}) => {
  // Strategy intents are authoritative only when supplied by the Python
  // worker. A missing feed is an empty/unknown state, never a UI fixture.
  const intents: StrategyIntentItem[] = Array.isArray(intentsData) ? intentsData : [];

  return (
    <div className="bg-zinc-900/90 border border-zinc-800 rounded-2xl p-4 sm:p-5 space-y-4 shadow-xl shadow-black/20">
      {/* Header */}
      <div className="flex flex-wrap items-center justify-between gap-2 border-b border-zinc-800/80 pb-3">
        <div className="flex items-center space-x-2">
          <div className="p-1.5 rounded-lg bg-cyan-950/80 border border-cyan-800/60 text-cyan-400">
            <Cpu className="w-4 h-4" />
          </div>
          <div>
            <h3 className="text-xs font-bold text-zinc-100 uppercase tracking-wider">
              Strategy Intent Stream (ML Calibrated)
            </h3>
            <p className="text-[11px] text-zinc-400">
              Strategies emit unconstrained economic intents. Strategies never place exchange orders directly.
            </p>
          </div>
        </div>

        <div className="flex items-center space-x-2 text-[10px] font-mono">
          <span className="text-zinc-500">Pipeline Invariant:</span>
          <span className="px-2 py-0.5 rounded font-bold bg-cyan-950 text-cyan-300 border border-cyan-800/80">
            INTENT ➔ META ALLOCATOR ➔ GOVERNOR
          </span>
        </div>
      </div>

      {/* Intents Grid */}
      <div className="grid grid-cols-1 md:grid-cols-2 gap-3.5 text-xs">
        {intents.length === 0 && (
          <div className="md:col-span-2 p-5 rounded-xl border border-amber-900/60 bg-amber-950/20 text-center text-amber-300">
            No verified strategy-intent stream is available. Intents are informational and cannot authorize orders.
          </div>
        )}
        {intents.map((intent) => {
          const isLong = intent.direction === 'LONG';
          return (
            <div
              key={intent.id}
              className="bg-zinc-950/80 border border-zinc-800/80 rounded-xl p-3.5 space-y-2.5 transition-all hover:border-zinc-700"
            >
              {/* Intent Header */}
              <div className="flex items-center justify-between">
                <div className="flex items-center space-x-2">
                  <span className="font-bold text-zinc-100 font-mono text-sm">
                    {intent.symbol}
                  </span>
                  <span
                    className={`px-1.5 py-0.5 rounded font-mono font-bold text-[10px] border ${
                      isLong
                        ? 'bg-emerald-950 text-emerald-300 border-emerald-800/80'
                        : 'bg-rose-950 text-rose-300 border-rose-800/80'
                    }`}
                  >
                    {intent.direction} ({isLong ? '+' : ''}{intent.rawTargetDelta})
                  </span>
                </div>

                <div className="flex items-center space-x-2 font-mono text-[10px]">
                  <span className="text-zinc-500">Urgency:</span>
                  <span
                    className={`px-1.5 py-0.5 rounded border ${
                      intent.urgency === 'HIGH'
                        ? 'bg-rose-950/60 text-rose-300 border-rose-800'
                        : intent.urgency === 'MEDIUM'
                        ? 'bg-amber-950/60 text-amber-300 border-amber-800'
                        : 'bg-zinc-800 text-zinc-400 border-zinc-700'
                    }`}
                  >
                    {intent.urgency}
                  </span>
                </div>
              </div>

              {/* Strategy Name & Hypothesis */}
              <div>
                <div className="font-semibold text-zinc-200 text-xs">
                  {intent.strategyName}
                </div>
                <p className="text-[11px] text-zinc-400 mt-1 leading-relaxed">
                  {intent.hypothesis}
                </p>
              </div>

              {/* Quantitative Metrics Bar */}
              <div className="bg-zinc-900/90 p-2.5 rounded-lg border border-zinc-800/60 grid grid-cols-3 gap-2 text-center font-mono text-[11px]">
                <div>
                  <div className="text-[10px] text-zinc-500 font-sans flex items-center justify-center gap-1"><Cpu className="w-3 h-3 text-cyan-500/70" /> ML Score</div>
                  <div className="text-cyan-300 font-bold mt-0.5">
                    {intent.opportunityScore.toFixed(1)}/100
                  </div>
                </div>
                <div>
                  <div className="text-[10px] text-zinc-500 font-sans">Confidence</div>
                  <div className="text-zinc-200 font-bold mt-0.5">
                    {(intent.confidence * 100).toFixed(0)}%
                  </div>
                </div>
                <div>
                  <div className="text-[10px] text-zinc-500 font-sans">Horizon</div>
                  <div className="text-zinc-300 font-medium mt-0.5">
                    {intent.timeHorizon}
                  </div>
                </div>
              </div>

              {/* Regime Fit & Notional Footnote */}
              <div className="flex items-center justify-between text-[10px] text-zinc-500 font-mono pt-1">
                <span>Fit: <strong className="text-zinc-400">{intent.regimeFit}</strong></span>
                <span>Cap: <strong className="text-zinc-400">${intent.proposedMaxNotionalUsd.toLocaleString()}</strong></span>
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
};
