import React from 'react';
import {
  Lock,
  ShieldCheck,
  AlertTriangle,
  XCircle,
  CheckCircle2,
  Percent,
  TrendingDown,
  Activity,
  Layers,
} from 'lucide-react';
import { AccountData, RiskState, RiskRuleItem } from '../types';

interface HardRiskBoundsCardProps {
  account: AccountData;
  riskState: RiskState;
  rules: RiskRuleItem[];
  liquidationDistancePct: number | null;
}

export const HardRiskBoundsCard: React.FC<HardRiskBoundsCardProps> = ({
  account,
  riskState,
  rules,
  liquidationDistancePct,
}) => {
  const hasVerifiedAccount =
    account.verified === true && account.source === 'BINANCE_TESTNET';
  const marginUtilization = hasVerifiedAccount ? account.margin_utilization_pct : null;
  const effectiveLeverage = hasVerifiedAccount ? account.effective_leverage : null;
  const drawdown = hasVerifiedAccount ? account.portfolio_drawdown_pct : null;
  const verifiedLiquidationDistance = hasVerifiedAccount ? liquidationDistancePct : null;

  const boundStatus = (value: number | null, limit: number, lowerIsSafer = true) => {
    if (value == null) return 'UNKNOWN';
    return lowerIsSafer ? (value <= limit ? 'PASS' : 'FAIL') : (value >= limit ? 'PASS' : 'FAIL');
  };

  const statusIcon = (status: string) => status === 'PASS'
    ? <CheckCircle2 className="w-3.5 h-3.5 text-emerald-400" />
    : <AlertTriangle className="w-3.5 h-3.5 text-amber-400" />;
  return (
    <div className="bg-zinc-900/90 border border-zinc-800 rounded-2xl p-4 sm:p-5 space-y-4 shadow-xl shadow-black/20">
      {/* Header */}
      <div className="flex flex-wrap items-center justify-between gap-2 border-b border-zinc-800/80 pb-3">
        <div className="flex items-center space-x-2">
          <div className="p-1.5 rounded-lg bg-indigo-950/80 border border-indigo-800/60 text-indigo-400">
            <Lock className="w-4 h-4" />
          </div>
          <div>
            <h3 className="text-xs font-bold text-zinc-100 uppercase tracking-wider">
              Deterministic Hard Risk Gates (Strictly Invariant)
            </h3>
            <p className="text-[11px] text-zinc-400">
              Risk Governor is hierarchically superior to all strategies, Meta Allocators, and ML models
            </p>
          </div>
        </div>

        <div className="flex items-center space-x-2 font-mono text-[10px]">
          <span className="text-zinc-500">Governor Hierarchy:</span>
          <span className="px-2 py-0.5 rounded font-bold bg-indigo-950 text-indigo-300 border border-indigo-800/80">
            RISK GOVERNOR &gt; ALL ENGINES &gt; ML
          </span>
        </div>
      </div>

      {/* 4 Hard Bounds Gauges */}
      <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-3 text-xs font-mono">
        {/* Margin Utilization */}
        <div className="p-3.5 bg-zinc-950/80 border border-zinc-800/80 rounded-xl space-y-1.5">
          <div className="flex items-center justify-between text-[10px] text-zinc-400 uppercase">
            <span>Margin Utilization</span>
            {statusIcon(boundStatus(marginUtilization, 25))}
          </div>
          <div className="text-lg font-bold text-emerald-400">{marginUtilization == null ? 'UNKNOWN' : `${marginUtilization.toFixed(1)}%`}</div>
          <div className="w-full bg-zinc-800 rounded-full h-1.5">
            <div className="bg-emerald-500 h-1.5 rounded-full" style={{ width: `${marginUtilization == null ? 0 : Math.min(marginUtilization, 100)}%` }} />
          </div>
          <div className="flex justify-between text-[10px] text-zinc-500 pt-0.5">
            <span>Current</span>
            <span>Hard Ceiling: 25.0%</span>
          </div>
        </div>

        {/* Effective Leverage */}
        <div className="p-3.5 bg-zinc-950/80 border border-zinc-800/80 rounded-xl space-y-1.5">
          <div className="flex items-center justify-between text-[10px] text-zinc-400 uppercase">
            <span>Effective Leverage</span>
            {statusIcon(boundStatus(effectiveLeverage, 2))}
          </div>
          <div className="text-lg font-bold text-zinc-100">{effectiveLeverage == null ? 'UNKNOWN' : `${effectiveLeverage.toFixed(2)}x`}</div>
          <div className="w-full bg-zinc-800 rounded-full h-1.5">
            <div className="bg-cyan-500 h-1.5 rounded-full" style={{ width: `${effectiveLeverage == null ? 0 : Math.min((effectiveLeverage / 2) * 100, 100)}%` }} />
          </div>
          <div className="flex justify-between text-[10px] text-zinc-500 pt-0.5">
            <span>Current</span>
            <span>Hard Ceiling: 2.00x</span>
          </div>
        </div>

        {/* Portfolio Drawdown */}
        <div className="p-3.5 bg-zinc-950/80 border border-zinc-800/80 rounded-xl space-y-1.5">
          <div className="flex items-center justify-between text-[10px] text-zinc-400 uppercase">
            <span>Portfolio Drawdown</span>
            {statusIcon(boundStatus(drawdown, 8))}
          </div>
          <div className="text-lg font-bold text-emerald-400">{drawdown == null ? 'UNKNOWN' : `${drawdown.toFixed(2)}%`}</div>
          <div className="w-full bg-zinc-800 rounded-full h-1.5">
            <div className="bg-emerald-500 h-1.5 rounded-full" style={{ width: `${drawdown == null ? 0 : Math.min((drawdown / 8) * 100, 100)}%` }} />
          </div>
          <div className="flex justify-between text-[10px] text-zinc-500 pt-0.5">
            <span>Current</span>
            <span>Hard Stop: -10.0%</span>
          </div>
        </div>

        {/* Liquidation Buffer */}
        <div className="p-3.5 bg-zinc-950/80 border border-zinc-800/80 rounded-xl space-y-1.5">
          <div className="flex items-center justify-between text-[10px] text-zinc-400 uppercase">
            <span>Liquidation Buffer</span>
            {verifiedLiquidationDistance == null ? (
              <AlertTriangle className="w-3.5 h-3.5 text-amber-400" />
            ) : (
              <CheckCircle2 className="w-3.5 h-3.5 text-emerald-400" />
            )}
          </div>
          <div className={`text-lg font-bold ${verifiedLiquidationDistance == null ? 'text-amber-400' : 'text-emerald-400'}`}>
            {verifiedLiquidationDistance == null ? 'UNKNOWN' : `+${verifiedLiquidationDistance.toFixed(1)}%`}
          </div>
          <div className="w-full bg-zinc-800 rounded-full h-1.5">
            <div
              className={`${verifiedLiquidationDistance == null ? 'bg-amber-500/50' : 'bg-emerald-500'} h-1.5 rounded-full`}
              style={{ width: verifiedLiquidationDistance == null ? '0%' : `${Math.min(verifiedLiquidationDistance, 100)}%` }}
            />
          </div>
          <div className="flex justify-between text-[10px] text-zinc-500 pt-0.5">
            <span>{verifiedLiquidationDistance == null ? 'No verified distance' : 'Current Distance'}</span>
            <span>Hard Floor: +25.0%</span>
          </div>
        </div>
      </div>

      {/* Rules Invariant Matrix */}
      <div className="space-y-2 pt-1">
        <div className="text-[10px] font-mono uppercase text-zinc-400 font-bold">
          Active Governor Hard Constraints
        </div>
        <div className="grid grid-cols-1 md:grid-cols-2 gap-2">
          {rules.map((rule, idx) => {
            const isPass = rule.status === 'PASS';
            const isWarn = rule.status === 'WARN';
            const isUnknown = rule.status === 'UNKNOWN';
            return (
              <div
                key={idx}
                className="flex items-center justify-between p-2.5 rounded-xl bg-zinc-950/60 border border-zinc-800/80 text-xs font-mono"
              >
                <div className="flex items-center space-x-2">
                  {isPass ? (
                    <CheckCircle2 className="w-3.5 h-3.5 text-emerald-400 shrink-0" />
                  ) : isWarn || isUnknown ? (
                    <AlertTriangle className="w-3.5 h-3.5 text-amber-400 shrink-0" />
                  ) : (
                    <XCircle className="w-3.5 h-3.5 text-rose-500 shrink-0" />
                  )}
                  <span className="text-zinc-300 font-sans">{rule.rule}</span>
                </div>
                <div className="flex items-center space-x-2">
                  <span className="text-zinc-400 text-[11px]">{rule.current}</span>
                  <span
                    className={`px-1.5 py-0.5 rounded text-[10px] font-bold border ${
                      isPass
                        ? 'bg-emerald-950 text-emerald-300 border-emerald-800'
                        : isWarn || isUnknown
                          ? 'bg-amber-950 text-amber-300 border-amber-800'
                        : 'bg-rose-950 text-rose-300 border-rose-800'
                    }`}
                  >
                    {rule.status}
                  </span>
                </div>
              </div>
            );
          })}
        </div>
      </div>
    </div>
  );
};
