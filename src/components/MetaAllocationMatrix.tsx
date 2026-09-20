import React from 'react';
import {
  Boxes,
  Info,
} from 'lucide-react';
import { MetaAllocationWeight } from '../types/strategy';
import { IllustrativeEvidenceBanner } from './IllustrativeEvidenceBanner';

export const MetaAllocationMatrix: React.FC<{ allocationsData?: MetaAllocationWeight[] }> = ({ allocationsData }) => {
  const allocations: MetaAllocationWeight[] = allocationsData || [
    {
      engineId: 'structural_grid',
      strategyName: 'Structural Mean-Reversion Grid',
      baseWeightPct: 35,
      expectedEdgeBps: 45,
      confidence: 0.85,
      regimeFitFactor: 1.15,
      executionQualityFactor: 1.0,
      cryptoBetaDiscount: 0.88,
      finalBudgetFactor: 1.15,
      virtualNotionalCapUsd: 25000,
      status: 'ACTIVE',
    },
    {
      engineId: 'trend_breakout',
      strategyName: 'Trend / Breakout Following',
      baseWeightPct: 25,
      expectedEdgeBps: 65,
      confidence: 0.65,
      regimeFitFactor: 0.80,
      executionQualityFactor: 0.95,
      cryptoBetaDiscount: 0.82,
      finalBudgetFactor: 0.75,
      virtualNotionalCapUsd: 15000,
      status: 'SCALED_DOWN',
    },
    {
      engineId: 'shock_momentum',
      strategyName: 'Shock Momentum',
      baseWeightPct: 15,
      expectedEdgeBps: 80,
      confidence: 0.72,
      regimeFitFactor: 0.90,
      executionQualityFactor: 0.90,
      cryptoBetaDiscount: 0.95,
      finalBudgetFactor: 0.85,
      virtualNotionalCapUsd: 10000,
      status: 'ACTIVE',
    },
    {
      engineId: 'funding_carry',
      strategyName: 'Funding / Basis Carry',
      baseWeightPct: 25,
      expectedEdgeBps: 30,
      confidence: 0.92,
      regimeFitFactor: 1.20,
      executionQualityFactor: 1.0,
      cryptoBetaDiscount: 0.70,
      finalBudgetFactor: 1.25,
      virtualNotionalCapUsd: 20000,
      status: 'ACTIVE',
    },
  ];

  return (
    <div className="bg-zinc-900/90 border border-zinc-800 rounded-2xl p-4 sm:p-5 space-y-4 shadow-xl shadow-black/20">
      {/* Header */}
      <div className="flex flex-wrap items-center justify-between gap-2 border-b border-zinc-800/80 pb-3">
        <div className="flex items-center space-x-2">
          <div className="p-1.5 rounded-lg bg-indigo-950/80 border border-indigo-800/60 text-indigo-400">
            <Boxes className="w-4 h-4" />
          </div>
          <div>
            <h3 className="text-xs font-bold text-zinc-100 uppercase tracking-wider">
              Meta Allocator: Continuous Risk Budgeting Matrix
            </h3>
            <p className="text-[11px] text-zinc-400">
              Continuous multiplier scaling (0.0x–2.0x) accounting for crypto beta correlation and execution quality
            </p>
          </div>
        </div>

        <div className="flex items-center space-x-2 text-[10px] font-mono">
          <span className="text-zinc-500">Allocation Model:</span>
          <span className="px-2 py-0.5 rounded font-bold bg-indigo-950 text-indigo-300 border border-indigo-800/80">
            CONTINUOUS FACTOR SCALED
          </span>
        </div>
      </div>

      <IllustrativeEvidenceBanner message="Expected edge, confidence, and allocation rows are illustrative; Risk Governor and Worker gates remain authoritative." />

      {/* Allocation Table */}
      <div className="overflow-x-auto border border-zinc-800/80 rounded-xl">
        <table className="w-full text-left text-xs font-mono">
          <thead className="bg-zinc-950/80 border-b border-zinc-800 text-[10px] uppercase text-zinc-400">
            <tr>
              <th className="py-2.5 px-3">Alpha Engine</th>
              <th className="py-2.5 px-3 text-right">Base Weight</th>
              <th className="py-2.5 px-3 text-right">Edge (bps)</th>
              <th className="py-2.5 px-3 text-right">Confidence</th>
              <th className="py-2.5 px-3 text-right">Regime Fit</th>
              <th className="py-2.5 px-3 text-right">Beta Discount</th>
              <th className="py-2.5 px-3 text-right">Final Budget Factor</th>
              <th className="py-2.5 px-3 text-right">Virtual Notional Cap</th>
              <th className="py-2.5 px-3 text-center">Status</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-zinc-800/60">
            {allocations.map((alloc) => {
              return (
                <tr key={alloc.engineId} className="hover:bg-zinc-800/30">
                  <td className="py-3 px-3 font-sans font-medium text-zinc-200">
                    <div className="font-bold">{alloc.strategyName}</div>
                    <div className="text-[10px] font-mono text-zinc-500">
                      ID: {alloc.engineId}
                    </div>
                  </td>
                  <td className="py-3 px-3 text-right text-zinc-300">
                    {alloc.baseWeightPct}%
                  </td>
                  <td className="py-3 px-3 text-right text-emerald-400 font-bold">
                    +{alloc.expectedEdgeBps}
                  </td>
                  <td className="py-3 px-3 text-right text-zinc-200">
                    {(alloc.confidence * 100).toFixed(0)}%
                  </td>
                  <td className="py-3 px-3 text-right text-cyan-300">
                    {alloc.regimeFitFactor.toFixed(2)}x
                  </td>
                  <td className="py-3 px-3 text-right text-amber-400">
                    -{((1 - alloc.cryptoBetaDiscount) * 100).toFixed(0)}%
                  </td>
                  <td className="py-3 px-3 text-right">
                    <span
                      className={`font-bold px-1.5 py-0.5 rounded text-xs ${
                        alloc.finalBudgetFactor >= 1.0
                          ? 'text-emerald-400 bg-emerald-950/60 border border-emerald-800/60'
                          : 'text-amber-400 bg-amber-950/60 border border-amber-800/60'
                      }`}
                    >
                      {alloc.finalBudgetFactor.toFixed(2)}x
                    </span>
                  </td>
                  <td className="py-3 px-3 text-right font-bold text-zinc-100">
                    ${alloc.virtualNotionalCapUsd.toLocaleString()}
                  </td>
                  <td className="py-3 px-3 text-center">
                    <span
                      className={`px-2 py-0.5 rounded text-[10px] font-bold border ${
                        alloc.status === 'ACTIVE'
                          ? 'bg-emerald-950 text-emerald-300 border-emerald-800/80'
                          : 'bg-amber-950 text-amber-300 border-amber-800/80'
                      }`}
                    >
                      {alloc.status}
                    </span>
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>

      {/* Factor Invariant Explanation */}
      <div className="p-3 bg-zinc-950 rounded-xl border border-zinc-800/80 flex items-start space-x-2.5 text-xs text-zinc-400">
        <Info className="w-4 h-4 text-indigo-400 shrink-0 mt-0.5" />
        <div>
          <span className="font-semibold text-zinc-200 block">
            Common Crypto Beta & Non-Independence Rule:
          </span>
          <span>
            BTC, ETH, SOL, and BNB share underlying systematic market factors. Signals across multiple assets are never treated as independent; the Meta Allocator applies continuous factor beta discounts to prevent compounding correlated drawdown.
          </span>
        </div>
      </div>
    </div>
  );
};
