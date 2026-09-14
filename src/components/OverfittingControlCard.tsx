import React from 'react';
import {
  ShieldAlert,
  CheckCircle2,
  AlertTriangle,
  Flame,
  Award,
  BarChart3,
  Layers,
  Info,
  Calendar,
} from 'lucide-react';
import { OverfittingMetrics, PurgedCVFold } from '../types/research';
import { IllustrativeEvidenceBanner } from './IllustrativeEvidenceBanner';

export const OverfittingControlCard: React.FC = () => {
  const metrics: OverfittingMetrics = {
    complexityBudget: {
      marketStatesUsed: 5,
      marketStatesMax: 5,
      alphaEnginesUsed: 4,
      alphaEnginesMax: 4,
      gridLevelsUsed: 5,
      gridLevelsMax: 5,
      featuresPerEngineAvg: 12,
      featuresBudgetMax: 15,
      budgetStatus: 'COMPLIANT',
    },
    deflatedSharpeRatio: 1.94,
    probabilityOfBacktestOverfittingPct: 6.2,
    parameterPlateauStabilityPct: 88.5,
    totalParameterCombinationsTested: 1420,
    walkForwardEfficiencyRatio: 0.82,
  };

  const cvFolds: PurgedCVFold[] = [
    {
      foldId: 1,
      trainWindow: '2023-Q1 - Q2',
      purgeWindowHours: 4,
      testWindow: '2023-Q3 (OOS)',
      embargoWindowHours: 8,
      isSharpe: 2.35,
      oosSharpe: 1.98,
      oosReturnPct: 14.2,
      status: 'STABLE',
    },
    {
      foldId: 2,
      trainWindow: '2023-Q2 - Q3',
      purgeWindowHours: 4,
      testWindow: '2023-Q4 (OOS)',
      embargoWindowHours: 8,
      isSharpe: 2.48,
      oosSharpe: 2.05,
      oosReturnPct: 18.6,
      status: 'STABLE',
    },
    {
      foldId: 3,
      trainWindow: '2023-Q3 - Q4',
      purgeWindowHours: 4,
      testWindow: '2024-Q1 (OOS)',
      embargoWindowHours: 8,
      isSharpe: 2.70,
      oosSharpe: 2.21,
      oosReturnPct: 22.4,
      status: 'STABLE',
    },
    {
      foldId: 4,
      trainWindow: '2023-Q4 - 2024-Q1',
      purgeWindowHours: 4,
      testWindow: '2024-Q2 (OOS)',
      embargoWindowHours: 8,
      isSharpe: 2.20,
      oosSharpe: 1.84,
      oosReturnPct: 11.8,
      status: 'STABLE',
    },
  ];

  return (
    <div className="bg-zinc-900/90 border border-zinc-800 rounded-2xl p-4 sm:p-5 space-y-4 shadow-xl shadow-black/20">
      {/* Header */}
      <div className="flex flex-wrap items-center justify-between gap-2 border-b border-zinc-800/80 pb-3">
        <div className="flex items-center space-x-2">
          <div className="p-1.5 rounded-lg bg-emerald-950/80 border border-emerald-800/60 text-emerald-400">
            <Award className="w-4 h-4" />
          </div>
          <div>
            <h3 className="text-xs font-bold text-zinc-100 uppercase tracking-wider">
              Overfitting Control & Complexity Budget (Section 22 Mandate)
            </h3>
            <p className="text-[11px] text-zinc-400">
              Multiple-testing accounting, Deflated Sharpe Ratio (DSR), Probability of Backtest Overfitting (PBO), and Purged CV
            </p>
          </div>
        </div>

        <div className="flex items-center space-x-2 font-mono text-[10px]">
          <span className="text-zinc-500">Complexity Budget:</span>
          <span className="px-2 py-0.5 rounded font-bold bg-emerald-950 text-emerald-300 border border-emerald-800/80">
            ILLUSTRATIVE ONLY
          </span>
        </div>
      </div>

      <IllustrativeEvidenceBanner message="No timestamped, current-build backtest artifact is loaded in this UI session." />

      {/* 4 Core Overfitting Metrics */}
      <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-3 text-xs font-mono">
        {/* DSR */}
        <div className="p-3.5 bg-zinc-950/80 border border-zinc-800/80 rounded-xl space-y-1">
          <div className="flex items-center justify-between text-[10px] text-zinc-400 uppercase">
            <span>Deflated Sharpe Ratio</span>
            <CheckCircle2 className="w-3.5 h-3.5 text-emerald-400" />
          </div>
          <div className="text-lg font-bold text-emerald-400">
            {metrics.deflatedSharpeRatio.toFixed(2)} DSR
          </div>
          <div className="text-[10px] text-zinc-400 font-sans">
            Target &gt; 1.50 (Passes {metrics.totalParameterCombinationsTested} search tests)
          </div>
        </div>

        {/* PBO */}
        <div className="p-3.5 bg-zinc-950/80 border border-zinc-800/80 rounded-xl space-y-1">
          <div className="flex items-center justify-between text-[10px] text-zinc-400 uppercase">
            <span>Prob. Overfitting (PBO)</span>
            <CheckCircle2 className="w-3.5 h-3.5 text-emerald-400" />
          </div>
          <div className="text-lg font-bold text-emerald-400">
            {metrics.probabilityOfBacktestOverfittingPct.toFixed(1)}%
          </div>
          <div className="text-[10px] text-zinc-400 font-sans">
            Ceiling: &lt; 15.0% Bailey-López de Prado standard
          </div>
        </div>

        {/* Plateau Stability */}
        <div className="p-3.5 bg-zinc-950/80 border border-zinc-800/80 rounded-xl space-y-1">
          <div className="flex items-center justify-between text-[10px] text-zinc-400 uppercase">
            <span>Parameter Plateau</span>
            <CheckCircle2 className="w-3.5 h-3.5 text-emerald-400" />
          </div>
          <div className="text-lg font-bold text-cyan-300">
            {metrics.parameterPlateauStabilityPct.toFixed(1)}% Stable
          </div>
          <div className="text-[10px] text-zinc-400 font-sans">
            Wide robust plateau vs. isolated peak spike
          </div>
        </div>

        {/* Walk-Forward Efficiency */}
        <div className="p-3.5 bg-zinc-950/80 border border-zinc-800/80 rounded-xl space-y-1">
          <div className="flex items-center justify-between text-[10px] text-zinc-400 uppercase">
            <span>Walk-Forward Efficiency</span>
            <CheckCircle2 className="w-3.5 h-3.5 text-emerald-400" />
          </div>
          <div className="text-lg font-bold text-zinc-100">
            {(metrics.walkForwardEfficiencyRatio * 100).toFixed(0)}% (OOS/IS)
          </div>
          <div className="text-[10px] text-zinc-400 font-sans">
            Healthy baseline (&gt; 65% target)
          </div>
        </div>
      </div>

      {/* Complexity Budget Consumption Matrix */}
      <div className="p-3.5 bg-zinc-950 rounded-xl border border-zinc-800/80 space-y-2.5">
        <div className="text-[10px] font-mono uppercase text-zinc-400 font-bold flex justify-between">
          <span>Enforced Complexity Budget Allocation</span>
          <span className="text-emerald-400">MLflow Experiment Registry: #EXP-024</span>
        </div>
        <div className="grid grid-cols-2 sm:grid-cols-4 gap-2 text-xs font-mono">
          <div className="p-2 bg-zinc-900/80 rounded-lg border border-zinc-800 flex justify-between items-center">
            <span className="text-zinc-400">Market States:</span>
            <span className="font-bold text-zinc-200">
              {metrics.complexityBudget.marketStatesUsed} / {metrics.complexityBudget.marketStatesMax} Max
            </span>
          </div>
          <div className="p-2 bg-zinc-900/80 rounded-lg border border-zinc-800 flex justify-between items-center">
            <span className="text-zinc-400">Alpha Engines:</span>
            <span className="font-bold text-zinc-200">
              {metrics.complexityBudget.alphaEnginesUsed} / {metrics.complexityBudget.alphaEnginesMax} Max
            </span>
          </div>
          <div className="p-2 bg-zinc-900/80 rounded-lg border border-zinc-800 flex justify-between items-center">
            <span className="text-zinc-400">Grid Levels:</span>
            <span className="font-bold text-zinc-200">
              {metrics.complexityBudget.gridLevelsUsed} / {metrics.complexityBudget.gridLevelsMax} Max
            </span>
          </div>
          <div className="p-2 bg-zinc-900/80 rounded-lg border border-zinc-800 flex justify-between items-center">
            <span className="text-zinc-400">Features/Engine:</span>
            <span className="font-bold text-zinc-200">
              {metrics.complexityBudget.featuresPerEngineAvg} / {metrics.complexityBudget.featuresBudgetMax} Max
            </span>
          </div>
        </div>
      </div>

      {/* Purged & Embargoed Cross-Validation (PCV) Table */}
      <div className="space-y-2">
        <div className="text-[10px] font-mono uppercase text-zinc-400 font-bold flex items-center justify-between">
          <span>Purged & Embargoed Out-of-Sample Folds (No Lookahead Leakage)</span>
          <span className="text-zinc-500 font-sans">Purge: 4h | Embargo: 8h</span>
        </div>
        <div className="overflow-x-auto border border-zinc-800/80 rounded-xl">
          <table className="w-full text-left text-xs font-mono">
            <thead className="bg-zinc-950/80 border-b border-zinc-800 text-[10px] uppercase text-zinc-400">
              <tr>
                <th className="py-2 px-3">Fold</th>
                <th className="py-2 px-3">Training Window</th>
                <th className="py-2 px-3">Purge</th>
                <th className="py-2 px-3">Test Window (OOS)</th>
                <th className="py-2 px-3">Embargo</th>
                <th className="py-2 px-3 text-right">IS Sharpe</th>
                <th className="py-2 px-3 text-right">OOS Sharpe</th>
                <th className="py-2 px-3 text-right">OOS Return</th>
                <th className="py-2 px-3 text-center">Stability</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-zinc-800/60">
              {cvFolds.map((fold) => (
                <tr key={fold.foldId} className="hover:bg-zinc-800/30">
                  <td className="py-2.5 px-3 text-zinc-200 font-bold">Fold #{fold.foldId}</td>
                  <td className="py-2.5 px-3 text-zinc-400">{fold.trainWindow}</td>
                  <td className="py-2.5 px-3 text-cyan-400">{fold.purgeWindowHours}h</td>
                  <td className="py-2.5 px-3 text-zinc-200 font-medium">{fold.testWindow}</td>
                  <td className="py-2.5 px-3 text-indigo-400">{fold.embargoWindowHours}h</td>
                  <td className="py-2.5 px-3 text-right text-zinc-400">{fold.isSharpe.toFixed(2)}</td>
                  <td className="py-2.5 px-3 text-right text-emerald-400 font-bold">{fold.oosSharpe.toFixed(2)}</td>
                  <td className="py-2.5 px-3 text-right text-emerald-300 font-bold">+{fold.oosReturnPct.toFixed(1)}%</td>
                  <td className="py-2.5 px-3 text-center">
                    <span className="px-1.5 py-0.5 rounded text-[10px] font-bold bg-emerald-950 text-emerald-300 border border-emerald-800/80">
                      {fold.status}
                    </span>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>
    </div>
  );
};
