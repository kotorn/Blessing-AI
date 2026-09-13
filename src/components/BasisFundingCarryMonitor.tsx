import React from 'react';
import {
  DollarSign,
  TrendingUp,
  Percent,
  Clock,
  ShieldAlert,
  CheckCircle2,
  Info,
  ArrowRight,
} from 'lucide-react';
import { InstrumentData } from '../types';

interface BasisFundingCarryMonitorProps {
  instrument: InstrumentData | undefined;
  symbol: string;
}

export const BasisFundingCarryMonitor: React.FC<BasisFundingCarryMonitorProps> = ({
  instrument,
  symbol,
}) => {
  const hasVerifiedData =
    instrument?.verified === true &&
    instrument.data_source === 'BINANCE_TESTNET';
  const basisUsd = hasVerifiedData ? instrument?.basis ?? null : null;
  const basisZScore = hasVerifiedData ? instrument?.basis_zscore ?? null : null;
  const fundingRate8h = hasVerifiedData ? instrument?.funding_rate ?? null : null;
  const fundingAnnualizedPct = hasVerifiedData ? instrument?.funding_annualized_pct ?? null : null;

  // The UI has no verified, timestamped fee/spread/slippage/financing model
  // attached to this instrument snapshot. Never turn a gross funding print
  // into a positive net-carry claim with a hard-coded cost assumption.
  const netExpectedCarryAnnualized = null;
  const isCarryViable = false;

  return (
    <div className="bg-zinc-900/90 border border-zinc-800 rounded-2xl p-4 sm:p-5 space-y-4 shadow-xl shadow-black/20">
      {/* Header */}
      <div className="flex flex-wrap items-center justify-between gap-2 border-b border-zinc-800/80 pb-3">
        <div className="flex items-center space-x-2">
          <div className="p-1.5 rounded-lg bg-emerald-950/80 border border-emerald-800/60 text-emerald-400">
            <DollarSign className="w-4 h-4" />
          </div>
          <div>
            <h3 className="text-xs font-bold text-zinc-100 uppercase tracking-wider">
              Spot vs. Perpetual Basis & Net Funding Carry ({symbol})
            </h3>
            <p className="text-[11px] text-zinc-400">
              Rigorous net expected carry after trading fees, spread, slippage, and basis mean-reversion
            </p>
          </div>
        </div>

        <div className="flex items-center space-x-2 text-[10px] font-mono">
          <span className="text-zinc-500">Carry Viability:</span>
          <span
            className={`px-2 py-0.5 rounded font-bold border ${
              isCarryViable
                ? 'bg-emerald-950 text-emerald-300 border-emerald-800/80'
                : 'bg-zinc-800 text-zinc-400 border-zinc-700'
            }`}
          >
            {hasVerifiedData && netExpectedCarryAnnualized != null
              ? (isCarryViable ? 'VIABLE (VERIFIED NET MODEL)' : 'MARGINAL (FEES DILUTE)')
              : 'UNKNOWN (VERIFIED COST MODEL REQUIRED)'}
          </span>
        </div>
      </div>

      {/* Primary Metrics Grid */}
      <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-3 text-xs">
        {/* Metric 1: Raw Basis Spread */}
        <div className="p-3 bg-zinc-950/70 border border-zinc-800/80 rounded-xl space-y-1">
          <div className="flex items-center justify-between text-[10px] font-mono text-zinc-500 uppercase">
            <span>Spot vs Perp Basis</span>
            <DollarSign className="w-3.5 h-3.5 text-zinc-400" />
          </div>
          <div className="text-sm font-mono font-bold text-zinc-100">
            {basisUsd == null ? 'UNKNOWN' : `${basisUsd >= 0 ? '+' : ''}$${basisUsd.toFixed(2)}`}
          </div>
          <div className="text-[10px] text-zinc-400 font-mono">
            Z-Score: <span className="text-cyan-300 font-bold">{basisZScore == null ? 'UNKNOWN' : `${basisZScore > 0 ? '+' : ''}${basisZScore.toFixed(2)}σ`}</span>
          </div>
        </div>

        {/* Metric 2: 8h Funding Rate */}
        <div className="p-3 bg-zinc-950/70 border border-zinc-800/80 rounded-xl space-y-1">
          <div className="flex items-center justify-between text-[10px] font-mono text-zinc-500 uppercase">
            <span>8h Funding Rate</span>
            <Percent className="w-3.5 h-3.5 text-amber-400" />
          </div>
          <div className="text-sm font-mono font-bold text-amber-400">
            {fundingRate8h == null ? 'UNKNOWN' : `${(fundingRate8h * 100).toFixed(4)}%`}
          </div>
          <div className="text-[10px] text-zinc-400">
            Next settlement: <span className="font-mono text-zinc-200">{hasVerifiedData ? 'from Testnet stream' : 'UNKNOWN'}</span>
          </div>
        </div>

        {/* Metric 3: Gross Annualized Funding APR */}
        <div className="p-3 bg-zinc-950/70 border border-zinc-800/80 rounded-xl space-y-1">
          <div className="flex items-center justify-between text-[10px] font-mono text-zinc-500 uppercase">
            <span>Gross Funding APR</span>
            <TrendingUp className="w-3.5 h-3.5 text-emerald-400" />
          </div>
          <div className="text-sm font-mono font-bold text-emerald-400">
            {fundingAnnualizedPct == null ? 'UNKNOWN' : `+${fundingAnnualizedPct.toFixed(2)}% APY`}
          </div>
          <div className="text-[10px] text-zinc-400">
            Longs pay shorts in perp
          </div>
        </div>

        {/* Metric 4: Net Expected Carry (Post-Cost) */}
        <div className="p-3 bg-zinc-950/70 border border-zinc-800/80 rounded-xl space-y-1">
          <div className="flex items-center justify-between text-[10px] font-mono text-zinc-500 uppercase">
            <span>Net Post-Fee Carry</span>
            <CheckCircle2 className="w-3.5 h-3.5 text-cyan-400" />
          </div>
          <div className="text-sm font-mono font-bold text-cyan-300">
            {netExpectedCarryAnnualized == null ? 'UNKNOWN' : `+${netExpectedCarryAnnualized.toFixed(2)}% Net`}
          </div>
          <div className="text-[10px] text-zinc-400">
            Requires verified fee, funding, spread, slippage, and financing inputs
          </div>
        </div>
      </div>

      {/* Carry Risk Governance Rule */}
      <div className="p-3 bg-zinc-950 rounded-xl border border-zinc-800/80 flex items-start space-x-2.5 text-xs text-zinc-400">
        <ShieldAlert className="w-4 h-4 text-amber-400 shrink-0 mt-0.5" />
        <div>
          <span className="font-semibold text-zinc-200 block">
            Capital Invariant: Funding Carry Must Never Breach Portfolio Hard Limits
          </span>
          <span>
            Attractive carry does not permit expanding gross margin beyond the $25\%$ utilization ceiling or accumulating uncapped directional basis divergence risk.
          </span>
        </div>
      </div>
    </div>
  );
};
