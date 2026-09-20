import React, { useState } from 'react';
import {
  PieChart,
  ArrowRightLeft,
} from 'lucide-react';
import { StrategyAttribution } from '../types/analytics';
import { IllustrativeEvidenceBanner } from './IllustrativeEvidenceBanner';

export const StrategyAttributionCard: React.FC = () => {
  const [selectedHorizon, setSelectedHorizon] = useState<'30D' | '90D' | 'ALL'>('30D');

  const strategies: StrategyAttribution[] = [
    {
      strategyId: 'trend',
      name: 'Trend / Breakout Following',
      tag: 'ASYMMETRIC ALPHA',
      grossPnL: 14280.5,
      netPnL: 12640.2,
      feesPaid: 1040.3,
      fundingSettlement: -320.0,
      slippageCost: 280.0,
      tradesCount: 48,
      winRatePct: 44.2,
      profitFactor: 2.35,
      sharpeRatio: 2.18,
      maxDrawdownPct: 3.8,
      capitalAllocPct: 35.0,
      currentVirtualExposure: 45000,
      primaryRegime: 'R1/R3 (Trend Expansion)',
    },
    {
      strategyId: 'grid',
      name: 'Structural Mean-Reversion Grid',
      tag: 'RANGE HARVESTER',
      grossPnL: 8920.0,
      netPnL: 6850.4,
      feesPaid: 1420.6,
      fundingSettlement: 150.0,
      slippageCost: 799.0,
      tradesCount: 264,
      winRatePct: 78.5,
      profitFactor: 1.62,
      sharpeRatio: 1.74,
      maxDrawdownPct: 5.2,
      capitalAllocPct: 25.0,
      currentVirtualExposure: 28000,
      primaryRegime: 'R0/R2 (Ranging / Compression)',
    },
    {
      strategyId: 'shock',
      name: 'Shock Momentum',
      tag: 'VELOCITY EXPANSION',
      grossPnL: 5640.8,
      netPnL: 4780.2,
      feesPaid: 520.4,
      fundingSettlement: 0.0,
      slippageCost: 340.2,
      tradesCount: 36,
      winRatePct: 58.3,
      profitFactor: 1.94,
      sharpeRatio: 2.05,
      maxDrawdownPct: 2.4,
      capitalAllocPct: 15.0,
      currentVirtualExposure: 18000,
      primaryRegime: 'R5 (Volatility Shock)',
    },
    {
      strategyId: 'carry',
      name: 'Basis & Funding Carry',
      tag: 'MARKET NEUTRAL',
      grossPnL: 3420.0,
      netPnL: 2890.5,
      feesPaid: 380.5,
      fundingSettlement: 0.0, // Carry yields directly
      slippageCost: 149.0,
      tradesCount: 18,
      winRatePct: 88.9,
      profitFactor: 3.12,
      sharpeRatio: 2.85,
      maxDrawdownPct: 0.9,
      capitalAllocPct: 25.0,
      currentVirtualExposure: 25000,
      primaryRegime: 'All Regimes (Positive Yield)',
    },
  ];

  const totalGrossPnL = strategies.reduce((acc, s) => acc + s.grossPnL, 0);
  const totalNetPnL = strategies.reduce((acc, s) => acc + s.netPnL, 0);
  const totalFees = strategies.reduce((acc, s) => acc + s.feesPaid, 0);
  const totalSlippage = strategies.reduce((acc, s) => acc + s.slippageCost, 0);
  const totalFunding = strategies.reduce((acc, s) => acc + s.fundingSettlement, 0);

  return (
    <div className="bg-zinc-900/90 border border-zinc-800 rounded-2xl p-4 sm:p-5 space-y-5 shadow-xl shadow-black/20">
      {/* Card Header */}
      <div className="flex flex-wrap items-center justify-between gap-3 border-b border-zinc-800/80 pb-3">
        <div className="flex items-center space-x-2.5">
          <div className="p-1.5 rounded-lg bg-indigo-950/80 border border-indigo-800/60 text-indigo-400">
            <PieChart className="w-4 h-4" />
          </div>
          <div>
            <h3 className="text-xs font-bold text-zinc-100 uppercase tracking-wider">
              Strategy Alpha Attribution & Virtual PnL Accounting
            </h3>
            <p className="text-[11px] text-zinc-400">
              Virtual strategy exposures reconciled at Target Exposure level; eliminates redundant physical offsetting orders
            </p>
          </div>
        </div>

        {/* Horizon selector */}
        <div className="flex items-center space-x-1.5 bg-zinc-950 p-1 rounded-xl border border-zinc-800 text-[11px] font-mono">
          {(['30D', '90D', 'ALL'] as const).map((h) => (
            <button
              key={h}
              onClick={() => setSelectedHorizon(h)}
              className={`px-2.5 py-0.5 rounded-lg font-bold transition-all ${
                selectedHorizon === h
                  ? 'bg-indigo-600 text-white shadow-sm'
                  : 'text-zinc-400 hover:text-zinc-200'
              }`}
            >
              {h}
            </button>
          ))}
        </div>
      </div>

      <IllustrativeEvidenceBanner message="The PnL attribution rows are static research fixtures until a verified ledger artifact is loaded." />

      {/* Aggregate Return & Friction Attribution Summary */}
      <div className="grid grid-cols-2 sm:grid-cols-5 gap-3 text-xs font-mono">
        <div className="p-3 bg-zinc-950/80 rounded-xl border border-zinc-800 space-y-1">
          <div className="text-[10px] uppercase text-zinc-400">Gross Alpha PnL</div>
          <div className="text-lg font-bold text-zinc-100">+${totalGrossPnL.toLocaleString(undefined, { minimumFractionDigits: 1, maximumFractionDigits: 1 })}</div>
          <div className="text-[10px] text-zinc-500 font-sans">Pre-friction alpha</div>
        </div>

        <div className="p-3 bg-zinc-950/80 rounded-xl border border-rose-900/40 space-y-1">
          <div className="text-[10px] uppercase text-zinc-400">Trading Fees Paid</div>
          <div className="text-lg font-bold text-rose-400">-${totalFees.toLocaleString(undefined, { minimumFractionDigits: 1, maximumFractionDigits: 1 })}</div>
          <div className="text-[10px] text-zinc-500 font-sans">Maker & taker commissions</div>
        </div>

        <div className="p-3 bg-zinc-950/80 rounded-xl border border-amber-900/40 space-y-1">
          <div className="text-[10px] uppercase text-zinc-400">Slippage & Impact</div>
          <div className="text-lg font-bold text-amber-400">-${totalSlippage.toLocaleString(undefined, { minimumFractionDigits: 1, maximumFractionDigits: 1 })}</div>
          <div className="text-[10px] text-zinc-500 font-sans">Queue & crossing drag</div>
        </div>

        <div className="p-3 bg-zinc-950/80 rounded-xl border border-indigo-900/40 space-y-1">
          <div className="text-[10px] uppercase text-zinc-400">Funding Settlement</div>
          <div className="text-lg font-bold text-indigo-300">
            {totalFunding >= 0 ? `+$${totalFunding.toFixed(1)}` : `-$${Math.abs(totalFunding).toFixed(1)}`}
          </div>
          <div className="text-[10px] text-zinc-500 font-sans">Perp funding 8h cycle</div>
        </div>

        <div className="p-3 bg-zinc-950/80 rounded-xl border border-emerald-800/80 space-y-1">
          <div className="text-[10px] uppercase text-emerald-400 font-bold">Net Attributed PnL</div>
          <div className="text-lg font-bold text-emerald-400">+${totalNetPnL.toLocaleString(undefined, { minimumFractionDigits: 1, maximumFractionDigits: 1 })}</div>
          <div className="text-[10px] text-emerald-500/80 font-sans">Realized institutional yield</div>
        </div>
      </div>

      {/* Virtual Netting Explanation Banner */}
      <div className="p-3.5 bg-zinc-950 rounded-xl border border-indigo-900/50 flex items-start space-x-3 text-xs text-zinc-300">
        <ArrowRightLeft className="w-4 h-4 text-indigo-400 shrink-0 mt-0.5" />
        <div className="space-y-1">
          <span className="font-bold text-zinc-100 font-mono text-[11px]">
            Virtual Portfolio Netting Engine (PLAN.md Section 14 Mandate):
          </span>
          <p className="text-[11px] text-zinc-400 leading-relaxed font-sans">
            When strategies issue conflicting intents (e.g., Grid requests +1.0 BTC, Trend requests -0.6 BTC, Shock requests -0.3 BTC), the system calculates a net target delta of <strong>+0.1 BTC</strong>. This avoids 1.9 BTC of unnecessary round-trip exchange fees and bid-ask spread crossing, while rigorously tracking individual strategy virtual balance attribution.
          </p>
        </div>
      </div>

      {/* Strategy Attribution Table */}
      <div className="overflow-x-auto border border-zinc-800 rounded-xl">
        <table className="w-full text-left text-xs font-mono">
          <thead className="bg-zinc-950/90 border-b border-zinc-800 text-[10px] uppercase text-zinc-400">
            <tr>
              <th className="py-2.5 px-3">Alpha Engine</th>
              <th className="py-2.5 px-3">Capital Alloc</th>
              <th className="py-2.5 px-3 text-right">Gross PnL</th>
              <th className="py-2.5 px-3 text-right">Fee Drag</th>
              <th className="py-2.5 px-3 text-right">Net PnL</th>
              <th className="py-2.5 px-3 text-center">Win Rate</th>
              <th className="py-2.5 px-3 text-center">Profit Factor</th>
              <th className="py-2.5 px-3 text-center">Sharpe</th>
              <th className="py-2.5 px-3 text-right">Max DD</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-zinc-800/60">
            {strategies.map((strat) => (
              <tr key={strat.strategyId} className="hover:bg-zinc-800/30">
                <td className="py-2.5 px-3">
                  <div className="font-bold text-zinc-100">{strat.name}</div>
                  <div className="text-[10px] text-zinc-500 font-sans">{strat.tag} • {strat.primaryRegime}</div>
                </td>
                <td className="py-2.5 px-3 text-cyan-300 font-medium">
                  {strat.capitalAllocPct.toFixed(1)}% (${strat.currentVirtualExposure.toLocaleString()})
                </td>
                <td className="py-2.5 px-3 text-right text-zinc-300">
                  +${strat.grossPnL.toLocaleString(undefined, { minimumFractionDigits: 1, maximumFractionDigits: 1 })}
                </td>
                <td className="py-2.5 px-3 text-right text-rose-400">
                  -${strat.feesPaid.toFixed(1)}
                </td>
                <td className="py-2.5 px-3 text-right text-emerald-400 font-bold">
                  +${strat.netPnL.toLocaleString(undefined, { minimumFractionDigits: 1, maximumFractionDigits: 1 })}
                </td>
                <td className="py-2.5 px-3 text-center text-zinc-200">
                  {strat.winRatePct.toFixed(1)}%
                </td>
                <td className="py-2.5 px-3 text-center text-zinc-200 font-semibold">
                  {strat.profitFactor.toFixed(2)}
                </td>
                <td className="py-2.5 px-3 text-center text-emerald-300 font-bold">
                  {strat.sharpeRatio.toFixed(2)}
                </td>
                <td className="py-2.5 px-3 text-right text-amber-400 font-medium">
                  -{strat.maxDrawdownPct.toFixed(1)}%
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
};
