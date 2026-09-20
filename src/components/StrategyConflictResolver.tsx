import React from 'react';
import {
  GitMerge,
  Info,
} from 'lucide-react';
import { StrategyConflictResolution } from '../types/strategy';

export const StrategyConflictResolver: React.FC = () => {
  // Authoritative architectural conflict resolution example from PLAN.md:
  // Grid: +1.0 BTC, Trend: -0.6 BTC, Shock: -0.3 BTC -> Net: +0.1 BTC
  const resolution: StrategyConflictResolution = {
    symbol: 'BTCUSDT',
    strategyIntents: [
      {
        engineId: 'structural_grid',
        name: 'Structural Mean-Reversion Grid',
        targetDelta: 1.0,
        valueUsd: 64250,
      },
      {
        engineId: 'trend_breakout',
        name: 'Trend / Breakout Following',
        targetDelta: -0.6,
        valueUsd: -38550,
      },
      {
        engineId: 'shock_momentum',
        name: 'Shock Momentum',
        targetDelta: -0.3,
        valueUsd: -19275,
      },
    ],
    grossDisputedDelta: 1.9, // 1.0 + 0.6 + 0.3
    netPhysicalTargetDelta: 0.1, // 1.0 - 0.6 - 0.3 = +0.1 BTC
    nettingFeeSavingsUsd: 115.65, // ~10 bps on 1.8 BTC avoided round-trip churn
    slippageMitigationBps: 3.5,
    virtualPnLAttributionPreserved: true,
  };

  return (
    <div className="bg-zinc-900/90 border border-zinc-800 rounded-2xl p-4 sm:p-5 space-y-4 shadow-xl shadow-black/20">
      {/* Header */}
      <div className="flex flex-wrap items-center justify-between gap-2 border-b border-zinc-800/80 pb-3">
        <div className="flex items-center space-x-2">
          <div className="p-1.5 rounded-lg bg-cyan-950/80 border border-cyan-800/60 text-cyan-400">
            <GitMerge className="w-4 h-4" />
          </div>
          <div>
            <h3 className="text-xs font-bold text-zinc-100 uppercase tracking-wider">
              Target Exposure Netting & Conflict Resolver ({resolution.symbol})
            </h3>
            <p className="text-[11px] text-zinc-400">
              Disagreements resolved at Target Exposure level; prevents unnecessary physical churn & preserves virtual PnL
            </p>
          </div>
        </div>

        <div className="flex items-center space-x-2 text-[10px] font-mono">
          <span className="text-zinc-500">Virtual PnL Accounting:</span>
          <span className="px-2 py-0.5 rounded font-bold bg-amber-950 text-amber-300 border border-amber-800/80 flex items-center space-x-1">
            <Info className="w-3 h-3" />
            <span>ILLUSTRATIVE ONLY</span>
          </span>
        </div>
      </div>

      {/* Conflict Visualizer: Disputed Intents vs Net Physical */}
      <div className="grid grid-cols-1 lg:grid-cols-3 gap-4">
        {/* Left: 3 Disputed Strategy Intents */}
        <div className="lg:col-span-2 space-y-2">
          <div className="text-[10px] font-mono uppercase text-zinc-400 font-bold">
            1. Upstream Strategy Intents (Gross Disputed: {resolution.grossDisputedDelta.toFixed(1)} BTC)
          </div>
          <div className="space-y-2">
            {resolution.strategyIntents.map((item) => {
              const isLong = item.targetDelta > 0;
              return (
                <div
                  key={item.engineId}
                  className="p-3 bg-zinc-950/80 rounded-xl border border-zinc-800/80 flex items-center justify-between text-xs font-mono"
                >
                  <div>
                    <div className="font-bold text-zinc-200 font-sans">
                      {item.name}
                    </div>
                    <div className="text-[10px] text-zinc-500">
                      ID: {item.engineId}
                    </div>
                  </div>

                  <div className="text-right">
                    <div
                      className={`font-bold text-sm ${
                        isLong ? 'text-emerald-400' : 'text-rose-400'
                      }`}
                    >
                      {isLong ? '+' : ''}
                      {item.targetDelta.toFixed(2)} BTC
                    </div>
                    <div className="text-[10px] text-zinc-400">
                      ${Math.abs(item.valueUsd).toLocaleString()} Notional
                    </div>
                  </div>
                </div>
              );
            })}
          </div>
        </div>

        {/* Right: Net Physical Target Delta */}
        <div className="p-4 bg-zinc-950 rounded-xl border border-cyan-800/60 flex flex-col justify-between space-y-3">
          <div>
            <div className="text-[10px] font-mono uppercase text-cyan-400 font-bold">
              2. Net Target Exposure (Python Worker Only — Not Sent)
            </div>
            <div className="mt-3 text-center py-4 bg-zinc-900/80 rounded-lg border border-cyan-900/40 space-y-1">
              <div className="text-2xl font-mono font-bold text-emerald-400">
                +{resolution.netPhysicalTargetDelta.toFixed(2)} BTC
              </div>
              <div className="text-xs text-zinc-300 font-sans font-medium">
                Physical Net Exposure (Long)
              </div>
              <div className="text-[11px] text-amber-400 font-mono">
                UNKNOWN NOTIONAL — requires verified Testnet market price
              </div>
            </div>
          </div>

          {/* Efficiency Savings */}
          <div className="space-y-2 pt-2 border-t border-zinc-800/80 text-xs font-mono">
            <div className="flex justify-between">
              <span className="text-zinc-400">Chumed Delta Avoided:</span>
              <span className="text-amber-400 font-bold">ILLUSTRATIVE</span>
            </div>
            <div className="flex justify-between">
              <span className="text-zinc-400">Fee Churn Saved:</span>
              <span className="text-emerald-400 font-bold">
                UNKNOWN
              </span>
            </div>
            <div className="flex justify-between">
              <span className="text-zinc-400">Slippage Mitigation:</span>
              <span className="text-cyan-300 font-bold">
                UNKNOWN
              </span>
            </div>
          </div>
        </div>
      </div>
    </div>
  );
};
