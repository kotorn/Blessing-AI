import React from 'react';
import {
  Cpu,
  Layers,
  Boxes,
  GitMerge,
  Gauge,
  Info,
  CheckCircle2,
  ShieldCheck,
  TrendingUp,
  Zap,
  Clock,
} from 'lucide-react';
import { BasketItem, InstrumentData, AccountData } from '../types';
import { StrategyIntentStream } from '../components/StrategyIntentStream';
import { MetaAllocationMatrix } from '../components/MetaAllocationMatrix';
import { StrategyConflictResolver } from '../components/StrategyConflictResolver';
import { ConservatismControlCard } from '../components/ConservatismControlCard';

interface StrategiesPageProps {
  baskets?: BasketItem[];
  instruments?: Record<string, InstrumentData>;
  account?: AccountData;
}

export const StrategiesPage: React.FC<StrategiesPageProps> = ({
  baskets = [],
  instruments = {},
  account,
}) => {
  const strategies = [
    {
      id: 'structural_grid',
      name: 'Structural Mean-Reversion Grid',
      category: 'Alpha Engine 1',
      status: 'ACTIVE',
      fit: 'R0/R1 Regimes (Range & Mean Reversion)',
      description: 'Adaptive geometric spacing based on ATR and swing levels. Never aggressive Martingale.',
      contractStatus: 'Authoritative in BasketManager',
    },
    {
      id: 'trend_breakout',
      name: 'Trend / Breakout Following',
      category: 'Alpha Engine 2',
      status: 'ACTIVE',
      fit: 'R2/R3/R4 Regimes (Breakout & Momentum)',
      description: 'Asymmetric convex winners. Pyramids winning positions, never losers.',
      contractStatus: 'Intent Modeled in Meta Allocator',
    },
    {
      id: 'shock_momentum',
      name: 'Shock Momentum',
      category: 'Alpha Engine 3',
      status: 'ACTIVE',
      fit: 'R5/R6 Regimes (Fast Liquidity Displacement)',
      description: 'Micro-horizon velocity & acceleration detection with strict time-stop transitions.',
      contractStatus: 'Intent Modeled in Meta Allocator',
    },
    {
      id: 'funding_carry',
      name: 'Funding / Basis Carry',
      category: 'Alpha Engine 4',
      status: 'ACTIVE',
      fit: 'Persistent Positive Basis & High Annualized Funding',
      description: 'Calculates net expected carry after fees, spread, slippage, and capital cost.',
      contractStatus: 'Tracked in Instruments & Intent Matrix',
    },
  ];

  return (
    <div className="space-y-6">
      {/* Page Header */}
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h2 className="text-xl font-bold text-zinc-100 flex items-center space-x-2">
            <Cpu className="w-5 h-5 text-cyan-400" />
            <span>Strategy Intent, Meta Allocation & Conflict Resolution</span>
          </h2>
          <p className="text-xs text-zinc-400 mt-1">
            Independent alpha engines produce StrategyIntents; the Meta Allocator sets continuous risk budgets; Target Exposure nets physical execution.
          </p>
        </div>

        <div className="flex items-center space-x-2">
          <span className="px-2.5 py-1 rounded text-xs font-mono font-bold bg-zinc-900 border border-zinc-700 text-zinc-300">
            Multi-Strategy Architecture (v0.2)
          </span>
        </div>
      </div>

      {/* Conservatism Control Telemetry */}
      <ConservatismControlCard />

      {/* Live Strategy Intent Stream */}
      <StrategyIntentStream baskets={baskets} instruments={instruments} />

      {/* Meta Allocation Continuous Budgeting Matrix */}
      <MetaAllocationMatrix />

      {/* Target Exposure & Conflict Resolution Panel */}
      <StrategyConflictResolver />

      {/* 4 Independent Alpha Engines Specifications */}
      <div className="space-y-3">
        <h3 className="text-xs font-bold text-zinc-400 uppercase tracking-wider font-mono">
          Independent Alpha Engine Specifications
        </h3>
        <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
          {strategies.map((strat) => (
            <div
              key={strat.id}
              className="bg-zinc-900/90 border border-zinc-800 rounded-xl p-4 space-y-3 hover:border-zinc-700 transition-colors"
            >
              <div className="flex items-center justify-between">
                <span className="text-[10px] font-mono uppercase text-cyan-400 font-bold tracking-wider">
                  {strat.category}
                </span>
                <span
                  className={`px-2 py-0.5 rounded text-[10px] font-bold border ${
                    strat.status === 'ACTIVE'
                      ? 'bg-emerald-950 text-emerald-300 border-emerald-800'
                      : 'bg-zinc-800 text-zinc-400 border-zinc-700'
                  }`}
                >
                  {strat.status}
                </span>
              </div>

              <div>
                <h3 className="text-sm font-bold text-zinc-100">{strat.name}</h3>
                <p className="text-xs text-zinc-400 mt-1 leading-relaxed">
                  {strat.description}
                </p>
              </div>

              <div className="text-xs space-y-1 text-zinc-300 bg-zinc-950 p-2.5 rounded-lg border border-zinc-800/80 font-mono">
                <div className="flex justify-between">
                  <span className="text-zinc-500">Target Regime:</span>
                  <span className="text-zinc-200">{strat.fit}</span>
                </div>
                <div className="flex justify-between">
                  <span className="text-zinc-500">Contract State:</span>
                  <span className="text-emerald-400 font-sans text-[11px] font-medium">
                    {strat.contractStatus}
                  </span>
                </div>
              </div>
            </div>
          ))}
        </div>
      </div>

      {/* Data Contract Lineage Footer */}
      <div className="p-4 rounded-xl bg-zinc-900/60 border border-zinc-800/80 text-xs text-zinc-400 space-y-2">
        <div className="flex items-center space-x-2 text-zinc-300 font-semibold">
          <Info className="w-4 h-4 text-cyan-400" />
          <span>Meta Allocation & Intent Lineage (UI-05)</span>
        </div>
        <div className="grid grid-cols-1 md:grid-cols-3 gap-2 font-mono text-[11px]">
          <div className="p-2 bg-zinc-950 rounded border border-zinc-800">
            <span className="text-emerald-400 font-bold block">EXISTING (/api/quant/state):</span>
            Active Basket state, grid safety score, realized volatility, margin utilization.
          </div>
          <div className="p-2 bg-zinc-950 rounded border border-zinc-800">
            <span className="text-cyan-400 font-bold block">DERIVED_FRONTEND:</span>
            Continuous Meta Allocator weights, conflict resolution net target delta, conservatism ratios.
          </div>
          <div className="p-2 bg-zinc-950 rounded border border-zinc-800">
            <span className="text-amber-400 font-bold block">PROPOSED_BACKEND (Epic UI-05):</span>
            Live NATS JetStream / WebSocket `strategy.intent.*` event feed from Python execution core.
          </div>
        </div>
      </div>
    </div>
  );
};
