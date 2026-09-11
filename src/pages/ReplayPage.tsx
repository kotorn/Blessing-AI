import React from 'react';
import { History, Award, Zap, BarChart2, Info, Compass, ShieldCheck } from 'lucide-react';
import { BacktestReplayStudio } from '../components/BacktestReplayStudio';
import { OverfittingControlCard } from '../components/OverfittingControlCard';
import { ExecutionSensitivityCard } from '../components/ExecutionSensitivityCard';

export const ReplayPage: React.FC = () => {
  return (
    <div className="space-y-6">
      {/* Page Header */}
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h2 className="text-xl font-bold text-zinc-100 flex items-center space-x-2">
            <History className="w-5 h-5 text-indigo-400" />
            <span>Research, Backtest & Stress Replay Engine</span>
          </h2>
          <p className="text-xs text-zinc-400 mt-1">
            Tick-by-tick historical scenarios, purged cross-validation, multiple-testing accounting, and execution sensitivity models.
          </p>
        </div>

        <div className="flex items-center space-x-2">
          <span className="px-2.5 py-1 rounded text-xs font-mono font-bold bg-zinc-900 border border-zinc-700 text-zinc-300">
            Engine Mode: EVENT-DRIVEN REPLAY (0ms Lookahead)
          </span>
        </div>
      </div>

      {/* 1. Event-Driven Backtester & Historical Replay Studio */}
      <BacktestReplayStudio />

      {/* 2. Execution Sensitivity & Microstructure Friction Gap */}
      <ExecutionSensitivityCard />

      {/* 3. Overfitting Control & Complexity Budget (DSR / PBO / Purged CV) */}
      <OverfittingControlCard />

      {/* Data Contract Lineage Footer */}
      <div className="p-4 rounded-xl bg-zinc-900/60 border border-zinc-800/80 text-xs text-zinc-400 space-y-2">
        <div className="flex items-center space-x-2 text-zinc-300 font-semibold">
          <Info className="w-4 h-4 text-cyan-400" />
          <span>Research & Backtest Contract Lineage (UI-08)</span>
        </div>
        <div className="grid grid-cols-1 md:grid-cols-3 gap-2 font-mono text-[11px]">
          <div className="p-2 bg-zinc-950 rounded border border-zinc-800">
            <span className="text-emerald-400 font-bold block">EXISTING (/api/quant/backtest/run):</span>
            Historical stress scenarios (COVID, Luna, FTX, Bull), ulcer index, balance/equity divergence.
          </div>
          <div className="p-2 bg-zinc-950 rounded border border-zinc-800">
            <span className="text-cyan-400 font-bold block">DERIVED_FRONTEND:</span>
            Execution sensitivity drag breakdown, DSR/PBO multiple-testing accounting, purged CV folds.
          </div>
          <div className="p-2 bg-zinc-950 rounded border border-zinc-800">
            <span className="text-amber-400 font-bold block">PROPOSED_BACKEND (Epic UI-08):</span>
            Distributed walk-forward runner via BigQuery Lakehouse partitioned Parquet trades feed.
          </div>
        </div>
      </div>
    </div>
  );
};
