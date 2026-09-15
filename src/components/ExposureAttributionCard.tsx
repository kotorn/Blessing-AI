import React from 'react';
import {
  Layers,
  TrendingUp,
  Percent,
  Shield,
  ExternalLink,
  PieChart,
} from 'lucide-react';
import { AccountData, BasketItem } from '../types';
import { AppRoute } from '../contracts/system';

interface ExposureAttributionCardProps {
  account: AccountData;
  baskets: BasketItem[];
  correlationBtcEth: number | null;
  cryptoBetaExposurePct: number | null;
  liquidationDistancePct: number | null;
  onOpenBalanceModal: () => void;
  onNavigate: (route: AppRoute) => void;
}

export const ExposureAttributionCard: React.FC<ExposureAttributionCardProps> = ({
  account,
  baskets,
  correlationBtcEth,
  cryptoBetaExposurePct,
  liquidationDistancePct,
  onOpenBalanceModal,
  onNavigate,
}) => {
  // Calculate net delta per symbol from active baskets
  const btcBaskets = baskets.filter((b) => b.instrument.includes('BTC'));
  const ethBaskets = baskets.filter((b) => b.instrument.includes('ETH'));

  const hasVerifiedAccount = account.verified === true && (account.source === 'BINANCE_TESTNET' || account.source === 'BINANCE_MAINNET');
  const hasVerifiedBaskets = baskets.some(
    (basket) => basket.verified === true && (basket.data_source === 'BINANCE_TESTNET' || basket.data_source === 'BINANCE_MAINNET'),
  );
  const btcNetDelta = hasVerifiedBaskets ? btcBaskets.reduce(
    (sum, b) => sum + (b.direction === 'LONG' ? b.total_size : -b.total_size),
    0,
  ) : null;
  const ethNetDelta = hasVerifiedBaskets ? ethBaskets.reduce(
    (sum, b) => sum + (b.direction === 'LONG' ? b.total_size : -b.total_size),
    0,
  ) : null;

  const marginPct = hasVerifiedAccount ? Math.min(100, Math.max(0, account.margin_utilization_pct)) : null;

  return (
    <div className="bg-zinc-900/90 border border-zinc-800 rounded-2xl p-4 sm:p-5 space-y-4 shadow-xl shadow-black/20">
      <div className="flex items-center justify-between border-b border-zinc-800/80 pb-3">
        <div className="flex items-center space-x-2">
          <div className="p-1.5 rounded-lg bg-indigo-950/80 border border-indigo-800/60 text-indigo-400">
            <Layers className="w-4 h-4" />
          </div>
          <div>
            <h3 className="text-xs font-bold text-zinc-100 uppercase tracking-wider">
              Target vs Actual Exposure & Risk Factor Attribution
            </h3>
            <p className="text-[11px] text-zinc-400">
              Aggregated economic deltas, margin safety gates, and systemic crypto beta
            </p>
          </div>
        </div>

        <div className="flex items-center space-x-2">
          <button
            type="button"
            onClick={onOpenBalanceModal}
            className="flex items-center space-x-1 px-2.5 py-1 rounded text-xs bg-zinc-800 hover:bg-zinc-700 text-zinc-300 transition-colors cursor-pointer"
            title="Open Balance Allocation breakdown modal"
          >
            <PieChart className="w-3 h-3 text-cyan-400" />
            <span className="text-[11px] font-medium">Sub-Wallets</span>
          </button>
          <button
            type="button"
            onClick={() => onNavigate('/risk')}
            className="flex items-center space-x-1 px-2.5 py-1 rounded text-xs bg-indigo-950/60 hover:bg-indigo-900/60 border border-indigo-800/60 text-indigo-300 transition-colors cursor-pointer"
            title="Deep dive into Risk Governor"
          >
            <span className="text-[11px] font-medium">Risk Details</span>
            <ExternalLink className="w-3 h-3" />
          </button>
        </div>
      </div>

      <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-4">
        {/* Metric 1: Net Delta Exposure */}
        <div className="p-3 bg-zinc-950/70 border border-zinc-800/80 rounded-xl space-y-1">
          <span className="text-[10px] font-mono text-zinc-400 uppercase">Physical Net Delta</span>
          <div className="flex items-baseline space-x-2">
            <span className="text-sm font-mono font-bold text-zinc-100">
              {btcNetDelta == null ? 'UNKNOWN' : `${btcNetDelta >= 0 ? '+' : ''}${btcNetDelta.toFixed(3)} BTC`}
            </span>
            <span className="text-xs font-mono text-zinc-400">
              {ethNetDelta == null ? 'UNKNOWN' : `${ethNetDelta >= 0 ? '+' : ''}${ethNetDelta.toFixed(2)} ETH`}
            </span>
          </div>
          <div className="text-[10px] text-zinc-500">
            Effective Leverage: <strong className="text-cyan-300">{hasVerifiedAccount ? `${account.effective_leverage.toFixed(2)}x` : 'UNKNOWN'}</strong>
          </div>
        </div>

        {/* Metric 2: Margin Gate */}
        <div className="p-3 bg-zinc-950/70 border border-zinc-800/80 rounded-xl space-y-1.5">
          <div className="flex justify-between items-center text-[10px] font-mono">
            <span className="text-zinc-400 uppercase">Margin Utilization</span>
            <span className={marginPct == null || marginPct > 20 ? 'text-amber-400 font-bold' : 'text-emerald-400 font-bold'}>
              {marginPct == null ? 'UNKNOWN' : `${account.margin_utilization_pct.toFixed(1)}%`} / 25% max
            </span>
          </div>
          {/* Progress bar */}
          <div className="w-full h-1.5 bg-zinc-800 rounded-full overflow-hidden">
            <div
              className={`h-full transition-all duration-300 ${
                marginPct == null ? 'bg-amber-500/50' : marginPct > 20 ? 'bg-amber-500' : 'bg-emerald-500'
              }`}
              style={{ width: `${marginPct == null ? 0 : Math.min((marginPct / 25) * 100, 100)}%` }}
            />
          </div>
          <div className="flex justify-between text-[10px] text-zinc-500 font-mono">
            <span>Free: {hasVerifiedAccount ? `$${Math.round(account.free_margin).toLocaleString()}` : 'UNKNOWN'}</span>
            <span>Used: {hasVerifiedAccount ? `$${Math.round(account.used_margin).toLocaleString()}` : 'UNKNOWN'}</span>
          </div>
        </div>

        {/* Metric 3: Liquidation Distance Buffer */}
        <div className="p-3 bg-zinc-950/70 border border-zinc-800/80 rounded-xl space-y-1">
          <div className="flex items-center justify-between text-[10px] font-mono text-zinc-400 uppercase">
            <span>Liquidation Distance</span>
            <Shield className={`w-3.5 h-3.5 ${liquidationDistancePct == null ? 'text-amber-400' : 'text-emerald-400'}`} />
          </div>
          <div className={`text-sm font-mono font-bold ${!hasVerifiedAccount || liquidationDistancePct == null ? 'text-amber-400' : 'text-emerald-400'}`}>
            {!hasVerifiedAccount || liquidationDistancePct == null ? 'UNKNOWN' : `+${liquidationDistancePct.toFixed(1)}%`}
          </div>
          <div className="text-[10px] text-zinc-500">
            {!hasVerifiedAccount || liquidationDistancePct == null ? 'Authoritative position risk data unavailable' : 'Survival threshold: &gt; 15.0% required'}
          </div>
        </div>

        {/* Metric 4: Systemic Factor Risk (Beta & Correlation) */}
        <div className="p-3 bg-zinc-950/70 border border-zinc-800/80 rounded-xl space-y-1">
          <div className="flex items-center justify-between text-[10px] font-mono text-zinc-400 uppercase">
            <span>Crypto Beta & Corr</span>
            <Percent className="w-3.5 h-3.5 text-indigo-400" />
          </div>
          <div className="flex items-baseline space-x-2">
            <span className="text-sm font-mono font-bold text-indigo-300">
              {cryptoBetaExposurePct == null ? 'UNKNOWN' : `${cryptoBetaExposurePct.toFixed(1)}% Beta`}
            </span>
            <span className="text-xs font-mono text-zinc-400">
              ρ = {correlationBtcEth == null ? 'UNKNOWN' : correlationBtcEth.toFixed(2)}
            </span>
          </div>
          <div className="text-[10px] text-zinc-500">
            {correlationBtcEth == null ? 'Awaiting verified portfolio factor data' : 'High co-movement: positions not independent'}
          </div>
        </div>
      </div>
    </div>
  );
};
