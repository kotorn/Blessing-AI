import React, { useState } from 'react';
import {
  ShieldAlert,
  AlertTriangle,
  CheckCircle2,
  XCircle,
  Activity,
  Sliders,
  TrendingDown,
  TrendingUp,
  BarChart3,
  HelpCircle,
  Eye,
  Percent,
  Clock,
  Info,
} from 'lucide-react';
import { ConservatismMetrics, FilterEfficacyItem, MissedMoveItem } from '../types/analytics';

export const ConservatismControlCard: React.FC = () => {
  const [filterCategory, setFilterCategory] = useState<'ALL' | 'EXCESSIVE' | 'OPTIMAL'>('ALL');

  const metrics: ConservatismMetrics = {
    zeroExposurePct: 22.4, // % of time with zero exposure
    rejectedOpportunityCount: 184,
    avgCapitalUtilizationPct: 28.5,
    peakCapitalUtilizationPct: 46.2,
    riskBudgetUtilizationPct: 34.0,
    returnLostDueToFiltersPct: 3.8,
    tailRiskReductionPct: 21.4,
    netFilterAdvantageRatio: 5.63, // 21.4% / 3.8% = 5.63x ratio
    opportunityScoreDistribution: [
      { range: '0 - 20 (Poor)', count: 412, executedCount: 0, conversionRatePct: 0.0 },
      { range: '21 - 40 (Marginal)', count: 285, executedCount: 18, conversionRatePct: 6.3 },
      { range: '41 - 60 (Fair)', count: 320, executedCount: 142, conversionRatePct: 44.4 },
      { range: '61 - 80 (Good)', count: 210, executedCount: 184, conversionRatePct: 87.6 },
      { range: '81 - 100 (Strong)', count: 98, executedCount: 96, conversionRatePct: 98.0 },
    ],
    filters: [
      {
        filterName: 'Market Volatility Expansion Gate (ATR > 2.5x)',
        description: 'Vetoes wide grid order placement during acute impulse shocks',
        timesTriggered: 54,
        returnForfeitedPct: 1.1,
        tailRiskAvoidedPct: 9.8,
        efficiencyRatio: 8.91,
        status: 'OPTIMAL',
      },
      {
        filterName: 'Stale Market Data Disconnect Filter (>3000ms)',
        description: 'Zero new exposure if public or private WebSocket lags',
        timesTriggered: 12,
        returnForfeitedPct: 0.2,
        tailRiskAvoidedPct: 4.5,
        efficiencyRatio: 22.5,
        status: 'OPTIMAL',
      },
      {
        filterName: 'Structural Range Compression Threshold',
        description: 'Blocks Trend entries inside consolidation sub-ranges',
        timesTriggered: 76,
        returnForfeitedPct: 1.9,
        tailRiskAvoidedPct: 4.8,
        efficiencyRatio: 2.53,
        status: 'CAUTION_TOO_CONSERVATIVE',
      },
      {
        filterName: 'Bid-Ask Spread Expansion Brake (>4 bps)',
        description: 'Prevents crossing wide order books during liquidity voids',
        timesTriggered: 42,
        returnForfeitedPct: 0.6,
        tailRiskAvoidedPct: 2.3,
        efficiencyRatio: 3.83,
        status: 'OPTIMAL',
      },
    ],
    recentMissedMoves: [
      {
        id: 'MM-01',
        timestamp: '2024-09-10 14:22:00',
        symbol: 'BTCUSDT',
        moveMagnitudePct: 2.8,
        vetoingGate: 'Structural Range Compression Threshold',
        gateCategory: 'REGIME',
        postMoveValidation: 'EXCESSIVE_CAUTION_FALSE_ALARM',
        detail: 'Trend breakout vetoed inside 4h compression band; price expanded +2.8% without pullback.',
      },
      {
        id: 'MM-02',
        timestamp: '2024-09-09 08:15:00',
        symbol: 'ETHUSDT',
        moveMagnitudePct: 4.2,
        vetoingGate: 'Market Volatility Expansion Gate (ATR > 2.5x)',
        gateCategory: 'VOLATILITY',
        postMoveValidation: 'VALID_TAIL_RISK_AVOIDED',
        detail: 'Grid entry vetoed during CPI release; initial +1.5% pop reversed into sharp -5.1% liquidation sweep.',
      },
      {
        id: 'MM-03',
        timestamp: '2024-09-07 19:40:00',
        symbol: 'BTCUSDT',
        moveMagnitudePct: 1.9,
        vetoingGate: 'Bid-Ask Spread Expansion Brake (>4 bps)',
        gateCategory: 'SPREAD',
        postMoveValidation: 'VALID_TAIL_RISK_AVOIDED',
        detail: 'Spread spiked to 6.2 bps on Binance Futures; avoided high taker slippage on fill.',
      },
    ],
  };

  const filteredItems = metrics.filters.filter((f) => {
    if (filterCategory === 'EXCESSIVE') return f.status === 'CAUTION_TOO_CONSERVATIVE';
    if (filterCategory === 'OPTIMAL') return f.status === 'OPTIMAL';
    return true;
  });

  return (
    <div className="bg-zinc-900/90 border border-zinc-800 rounded-2xl p-4 sm:p-5 space-y-5 shadow-xl shadow-black/20">
      {/* Header */}
      <div className="flex flex-wrap items-center justify-between gap-3 border-b border-zinc-800/80 pb-3">
        <div className="flex items-center space-x-2.5">
          <div className="p-1.5 rounded-lg bg-amber-950/80 border border-amber-800/60 text-amber-400">
            <Sliders className="w-4 h-4" />
          </div>
          <div>
            <h3 className="text-xs font-bold text-zinc-100 uppercase tracking-wider">
              Conservatism Control & Missed-Move Analysis
            </h3>
            <p className="text-[11px] text-zinc-400">
              Detects excessive caution: measures whether filters prevent catastrophic tail risks or needlessly destroy return
            </p>
          </div>
        </div>

        <div className="flex items-center space-x-2 font-mono text-[10px]">
          <span className="text-zinc-500">Filter Efficiency Ratio:</span>
          <span className="px-2 py-0.5 rounded font-bold bg-emerald-950 text-emerald-300 border border-emerald-800/80">
            {metrics.netFilterAdvantageRatio.toFixed(2)}x (TAIL RISK AVOIDED / RETURN LOST)
          </span>
        </div>
      </div>

      {/* Primary Conservatism Telemetry Metrics */}
      <div className="grid grid-cols-2 sm:grid-cols-4 lg:grid-cols-6 gap-3 text-xs font-mono">
        {/* Zero Exposure Time */}
        <div className="p-3 bg-zinc-950/80 rounded-xl border border-zinc-800 space-y-1">
          <div className="text-[10px] text-zinc-400 uppercase">Zero-Exposure Time</div>
          <div className="text-lg font-bold text-zinc-200">{metrics.zeroExposurePct.toFixed(1)}%</div>
          <div className="text-[10px] text-zinc-500 font-sans">Time standing aside in cash</div>
        </div>

        {/* Rejected Opportunities */}
        <div className="p-3 bg-zinc-950/80 rounded-xl border border-zinc-800 space-y-1">
          <div className="text-[10px] text-zinc-400 uppercase">Rejected Signals</div>
          <div className="text-lg font-bold text-amber-400">{metrics.rejectedOpportunityCount}</div>
          <div className="text-[10px] text-zinc-500 font-sans">Filtered by gates / budget</div>
        </div>

        {/* Capital Utilization */}
        <div className="p-3 bg-zinc-950/80 rounded-xl border border-zinc-800 space-y-1">
          <div className="text-[10px] text-zinc-400 uppercase">Capital Utilization</div>
          <div className="text-lg font-bold text-cyan-300">{metrics.avgCapitalUtilizationPct.toFixed(1)}%</div>
          <div className="text-[10px] text-zinc-500 font-sans">Peak: {metrics.peakCapitalUtilizationPct.toFixed(1)}%</div>
        </div>

        {/* Risk Budget Utilization */}
        <div className="p-3 bg-zinc-950/80 rounded-xl border border-zinc-800 space-y-1">
          <div className="text-[10px] text-zinc-400 uppercase">Risk Budget Active</div>
          <div className="text-lg font-bold text-indigo-300">{metrics.riskBudgetUtilizationPct.toFixed(1)}%</div>
          <div className="text-[10px] text-zinc-500 font-sans">Of allowable portfolio cap</div>
        </div>

        {/* Return Lost to Filters */}
        <div className="p-3 bg-zinc-950/80 rounded-xl border border-rose-900/40 space-y-1">
          <div className="text-[10px] text-zinc-400 uppercase">Return Forfeited</div>
          <div className="text-lg font-bold text-rose-400">-{metrics.returnLostDueToFiltersPct.toFixed(1)}%</div>
          <div className="text-[10px] text-zinc-500 font-sans">Sacrificed by filter vetoes</div>
        </div>

        {/* Tail Risk Avoided */}
        <div className="p-3 bg-zinc-950/80 rounded-xl border border-emerald-800/60 space-y-1">
          <div className="text-[10px] text-emerald-400 uppercase font-bold">Tail Risk Avoided</div>
          <div className="text-lg font-bold text-emerald-400">+{metrics.tailRiskReductionPct.toFixed(1)}%</div>
          <div className="text-[10px] text-emerald-500/80 font-sans">Preserved capital from DD</div>
        </div>
      </div>

      {/* Opportunity Score Distribution Breakdown */}
      <div className="p-4 bg-zinc-950 rounded-xl border border-zinc-800 space-y-2.5">
        <div className="flex items-center justify-between text-[11px] font-mono text-zinc-300">
          <span className="font-bold uppercase text-zinc-400">Opportunity Score Conversion Distribution</span>
          <span className="text-zinc-500">Continuous scoring vs binary vetoes</span>
        </div>

        <div className="grid grid-cols-1 sm:grid-cols-5 gap-2 text-xs font-mono">
          {metrics.opportunityScoreDistribution.map((bucket) => (
            <div key={bucket.range} className="p-2.5 bg-zinc-900/90 rounded-lg border border-zinc-800 space-y-1.5">
              <div className="text-[10px] text-zinc-400 font-medium">{bucket.range}</div>
              <div className="flex items-baseline justify-between">
                <span className="text-base font-bold text-zinc-100">{bucket.count}</span>
                <span className="text-[11px] text-cyan-400">{bucket.conversionRatePct.toFixed(0)}% Exec</span>
              </div>
              <div className="w-full bg-zinc-800 rounded-full h-1.5 overflow-hidden">
                <div
                  className={`h-full rounded-full ${
                    bucket.conversionRatePct > 75
                      ? 'bg-emerald-500'
                      : bucket.conversionRatePct > 25
                      ? 'bg-cyan-500'
                      : 'bg-zinc-600'
                  }`}
                  style={{ width: `${bucket.conversionRatePct}%` }}
                />
              </div>
            </div>
          ))}
        </div>
      </div>

      {/* Individual Filter Efficacy Evaluation Table */}
      <div className="space-y-2">
        <div className="flex flex-wrap items-center justify-between gap-2 text-[11px] font-mono">
          <span className="uppercase text-zinc-400 font-bold">Filter Efficacy & Return/Risk Trade-off Audit</span>
          <div className="flex items-center space-x-1.5 bg-zinc-950 p-0.5 rounded-lg border border-zinc-800 text-[10px]">
            {(['ALL', 'OPTIMAL', 'EXCESSIVE'] as const).map((cat) => (
              <button
                key={cat}
                onClick={() => setFilterCategory(cat)}
                className={`px-2 py-0.5 rounded font-bold transition-all ${
                  filterCategory === cat
                    ? 'bg-amber-600 text-white'
                    : 'text-zinc-400 hover:text-zinc-200'
                }`}
              >
                {cat}
              </button>
            ))}
          </div>
        </div>

        <div className="overflow-x-auto border border-zinc-800 rounded-xl">
          <table className="w-full text-left text-xs font-mono">
            <thead className="bg-zinc-950/90 border-b border-zinc-800 text-[10px] uppercase text-zinc-400">
              <tr>
                <th className="py-2.5 px-3">Filter Guard Name</th>
                <th className="py-2.5 px-3 text-center">Triggers</th>
                <th className="py-2.5 px-3 text-right">Return Lost</th>
                <th className="py-2.5 px-3 text-right">Tail-Risk Avoided</th>
                <th className="py-2.5 px-3 text-center">Benefit Ratio</th>
                <th className="py-2.5 px-3 text-center">Assessment</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-zinc-800/60">
              {filteredItems.map((filter, idx) => (
                <tr key={idx} className="hover:bg-zinc-800/30">
                  <td className="py-2.5 px-3">
                    <div className="font-bold text-zinc-100">{filter.filterName}</div>
                    <div className="text-[10px] text-zinc-500 font-sans">{filter.description}</div>
                  </td>
                  <td className="py-2.5 px-3 text-center text-zinc-300">
                    {filter.timesTriggered}
                  </td>
                  <td className="py-2.5 px-3 text-right text-rose-400 font-semibold">
                    -{filter.returnForfeitedPct.toFixed(1)}%
                  </td>
                  <td className="py-2.5 px-3 text-right text-emerald-400 font-bold">
                    +{filter.tailRiskAvoidedPct.toFixed(1)}%
                  </td>
                  <td className="py-2.5 px-3 text-center text-cyan-300 font-bold">
                    {filter.efficiencyRatio.toFixed(1)}x
                  </td>
                  <td className="py-2.5 px-3 text-center">
                    <span
                      className={`px-2 py-0.5 rounded text-[10px] font-bold ${
                        filter.status === 'OPTIMAL'
                          ? 'bg-emerald-950 text-emerald-300 border border-emerald-800/80'
                          : 'bg-amber-950 text-amber-300 border border-amber-800/80'
                      }`}
                    >
                      {filter.status === 'OPTIMAL' ? 'OPTIMAL' : 'TOO CONSERVATIVE'}
                    </span>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>

      {/* Missed-Move Log Analysis */}
      <div className="space-y-2">
        <div className="text-[11px] font-mono text-zinc-400 font-bold uppercase">
          Recent Missed-Move Telemetry (Veto Post-Mortem)
        </div>
        <div className="space-y-2">
          {metrics.recentMissedMoves.map((mm) => (
            <div
              key={mm.id}
              className="p-3 bg-zinc-950/80 border border-zinc-800 rounded-xl flex flex-wrap items-start justify-between gap-2 text-xs font-mono"
            >
              <div className="space-y-1 max-w-xl">
                <div className="flex items-center space-x-2">
                  <span className="font-bold text-zinc-200">{mm.symbol}</span>
                  <span className="text-zinc-500 text-[10px]">{mm.timestamp}</span>
                  <span className="text-[10px] px-1.5 py-0.2 rounded bg-zinc-900 border border-zinc-800 text-zinc-400">
                    {mm.vetoingGate}
                  </span>
                </div>
                <p className="text-[11px] text-zinc-400 font-sans">{mm.detail}</p>
              </div>

              <div className="text-right space-y-1">
                <div className="text-sm font-bold text-zinc-200">+{mm.moveMagnitudePct.toFixed(1)}% Move</div>
                <span
                  className={`inline-block px-2 py-0.5 rounded text-[10px] font-bold ${
                    mm.postMoveValidation === 'VALID_TAIL_RISK_AVOIDED'
                      ? 'bg-emerald-950 text-emerald-300 border border-emerald-800/80'
                      : 'bg-amber-950 text-amber-300 border border-amber-800/80'
                  }`}
                >
                  {mm.postMoveValidation === 'VALID_TAIL_RISK_AVOIDED'
                    ? 'VALID TAIL RISK AVOIDED'
                    : 'FALSE ALARM (EXCESSIVE CAUTION)'}
                </span>
              </div>
            </div>
          ))}
        </div>
      </div>
    </div>
  );
};
