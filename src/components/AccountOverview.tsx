import React from 'react';
import { Wallet, TrendingUp, AlertTriangle, Scale, Percent } from 'lucide-react';
import { AccountData } from '../types';

interface AccountOverviewProps {
  account: AccountData;
}

export const AccountOverview: React.FC<AccountOverviewProps> = ({ account }) => {
  const drawdownPct = account?.portfolio_drawdown_pct ?? 0;
  const marginPct = account?.margin_utilization_pct ?? 0;
  const equity = account?.equity ?? 0;
  const balance = account?.balance ?? 0;
  const dailyPnl = account?.daily_pnl ?? 0;
  const leverage = account?.effective_leverage ?? 0;
  const freeMargin = account?.free_margin ?? 0;
  const usedMargin = account?.used_margin ?? 0;

  const isDrawdownCritical = drawdownPct >= 4.0;
  const isMarginHigh = marginPct >= 25.0;

  return (
    <div className="grid grid-cols-2 md:grid-cols-3 lg:grid-cols-6 gap-3">
      {/* Total Equity */}
      <div className="bg-zinc-900/70 border border-zinc-800 p-3.5 rounded-xl">
        <div className="flex items-center justify-between text-zinc-400 text-xs mb-1">
          <span>Portfolio Equity</span>
          <Wallet className="w-3.5 h-3.5 text-zinc-500" />
        </div>
        <div className="text-xl font-bold text-zinc-100 font-mono">
          ${equity.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}
        </div>
        <div className="text-[11px] text-zinc-400 mt-1 flex items-center justify-between">
          <span>Bal: ${balance.toLocaleString('en-US', { maximumFractionDigits: 0 })}</span>
          <span className="text-emerald-400 font-mono">+1.2%</span>
        </div>
      </div>

      {/* Daily PnL */}
      <div className="bg-zinc-900/70 border border-zinc-800 p-3.5 rounded-xl">
        <div className="flex items-center justify-between text-zinc-400 text-xs mb-1">
          <span>24h Net PnL</span>
          <TrendingUp className="w-3.5 h-3.5 text-emerald-400" />
        </div>
        <div className={`text-xl font-bold font-mono ${dailyPnl >= 0 ? 'text-emerald-400' : 'text-rose-400'}`}>
          {dailyPnl >= 0 ? '+' : ''}${dailyPnl.toFixed(2)}
        </div>
        <div className="text-[11px] text-zinc-400 mt-1">
          Net after fees & funding
        </div>
      </div>

      {/* Margin Utilization */}
      <div className="bg-zinc-900/70 border border-zinc-800 p-3.5 rounded-xl">
        <div className="flex items-center justify-between text-zinc-400 text-xs mb-1">
          <span>Margin Utilization</span>
          <Percent className={`w-3.5 h-3.5 ${isMarginHigh ? 'text-amber-400' : 'text-zinc-500'}`} />
        </div>
        <div className={`text-xl font-bold font-mono ${isMarginHigh ? 'text-amber-400' : 'text-zinc-100'}`}>
          {marginPct.toFixed(1)}%
        </div>
        <div className="w-full bg-zinc-800 h-1.5 rounded-full mt-2 overflow-hidden">
          <div
            className={`h-full transition-all duration-500 ${
              marginPct > 30
                ? 'bg-rose-500'
                : marginPct > 20
                ? 'bg-amber-500'
                : 'bg-emerald-500'
            }`}
            style={{ width: `${Math.min(marginPct, 100)}%` }}
          />
        </div>
      </div>

      {/* Effective Leverage */}
      <div className="bg-zinc-900/70 border border-zinc-800 p-3.5 rounded-xl">
        <div className="flex items-center justify-between text-zinc-400 text-xs mb-1">
          <span>Effective Leverage</span>
          <Scale className="w-3.5 h-3.5 text-zinc-500" />
        </div>
        <div className="text-xl font-bold text-zinc-100 font-mono">
          {leverage.toFixed(2)}x
        </div>
        <div className="text-[11px] text-zinc-400 mt-1">
          Hard Cap: 2.00x
        </div>
      </div>

      {/* Portfolio Drawdown */}
      <div className="bg-zinc-900/70 border border-zinc-800 p-3.5 rounded-xl">
        <div className="flex items-center justify-between text-zinc-400 text-xs mb-1">
          <span>Equity Drawdown</span>
          <AlertTriangle className={`w-3.5 h-3.5 ${isDrawdownCritical ? 'text-rose-400' : 'text-zinc-500'}`} />
        </div>
        <div className={`text-xl font-bold font-mono ${isDrawdownCritical ? 'text-rose-400' : 'text-zinc-100'}`}>
          {drawdownPct.toFixed(2)}%
        </div>
        <div className="text-[11px] text-zinc-400 mt-1">
          Caution: 2% | Hard: 8%
        </div>
      </div>

      {/* Free Margin */}
      <div className="bg-zinc-900/70 border border-zinc-800 p-3.5 rounded-xl">
        <div className="flex items-center justify-between text-zinc-400 text-xs mb-1">
          <span>Free Margin</span>
          <Wallet className="w-3.5 h-3.5 text-zinc-500" />
        </div>
        <div className="text-xl font-bold text-zinc-100 font-mono">
          ${freeMargin.toLocaleString('en-US', { maximumFractionDigits: 0 })}
        </div>
        <div className="text-[11px] text-zinc-400 mt-1">
          Used: ${usedMargin.toLocaleString('en-US', { maximumFractionDigits: 0 })}
        </div>
      </div>
    </div>
  );
};
