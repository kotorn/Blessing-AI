import React from 'react';
import {
  LineChart,
  Cpu,
  ShieldCheck,
  ShieldAlert,
  Layers,
  Boxes,
  ArrowRight,
  CheckCircle2,
  AlertTriangle,
  Flame,
  Activity,
  ChevronRight,
} from 'lucide-react';
import { AccountData, BasketItem, InstrumentData, RiskRuleItem } from '../types';
import { AppRoute } from '../contracts/system';

interface CommandDecisionFlowProps {
  account: AccountData;
  instruments: Record<string, InstrumentData>;
  baskets: BasketItem[];
  riskRules: RiskRuleItem[];
  liquidationDistancePct: number;
  onNavigate: (route: AppRoute) => void;
}

export const CommandDecisionFlow: React.FC<CommandDecisionFlowProps> = ({
  account,
  instruments,
  baskets,
  riskRules,
  liquidationDistancePct,
  onNavigate,
}) => {
  // 1. Market State Summary
  const btcData = instruments['BTCUSDT'];
  const ethData = instruments['ETHUSDT'];
  const btcRegime = btcData?.regime?.replace(/_/g, ' ') || 'R0 MEAN REVERSION';
  const ethRegime = ethData?.regime?.replace(/_/g, ' ') || 'R0 MEAN REVERSION';

  // 2. Risk Checks Summary
  const failedRules = riskRules.filter((r) => r.status === 'FAIL');
  const warnRules = riskRules.filter((r) => r.status === 'WARN');
  const isRiskOk = failedRules.length === 0 && account.risk_state === 'NORMAL';

  // 3. Basket & Recovery Summary
  const recoveringBaskets = baskets.filter((b) => b.state === 'RECOVERY' || b.state === 'DELEVERAGING');
  const maxDepthReached = baskets.reduce((max, b) => Math.max(max, b.grid_depth), 0);
  const totalNetPnl = baskets.reduce((sum, b) => sum + b.net_pnl, 0);

  return (
    <div className="bg-zinc-900/90 border border-zinc-800 rounded-2xl p-4 sm:p-5 space-y-3.5 shadow-xl shadow-black/20">
      <div className="flex flex-wrap items-center justify-between gap-2 border-b border-zinc-800/80 pb-3">
        <div className="flex items-center space-x-2">
          <div className="p-1.5 rounded-lg bg-cyan-950/80 border border-cyan-800/60 text-cyan-400">
            <Activity className="w-4 h-4" />
          </div>
          <div>
            <h3 className="text-xs font-bold text-zinc-100 uppercase tracking-wider">
              Operational Decision Pipeline Flow
            </h3>
            <p className="text-[11px] text-zinc-400">
              Deterministic real-time flow: Market State ➔ Strategy Opportunity ➔ Risk Governor ➔ Target Exposure ➔ Baskets
            </p>
          </div>
        </div>

        <div className="flex items-center space-x-2">
          <span className="text-[11px] font-mono text-zinc-500">Pipeline State:</span>
          <span className="px-2 py-0.5 rounded text-[10px] font-bold font-mono bg-emerald-950 text-emerald-300 border border-emerald-800/80 flex items-center space-x-1">
            <CheckCircle2 className="w-3 h-3" />
            <span>AUTHORITATIVE</span>
          </span>
        </div>
      </div>

      {/* Interactive 5-Step Pipeline Grid */}
      <div className="grid grid-cols-1 md:grid-cols-5 gap-3">
        {/* Step 1: Market State */}
        <button
          type="button"
          onClick={() => onNavigate('/markets')}
          className="text-left p-3 rounded-xl bg-zinc-950/70 border border-zinc-800/80 hover:border-cyan-500/50 hover:bg-zinc-900/80 transition-all group cursor-pointer space-y-1.5"
        >
          <div className="flex items-center justify-between text-[10px] font-bold text-zinc-400 uppercase tracking-wider">
            <span className="flex items-center space-x-1 text-cyan-400">
              <LineChart className="w-3.5 h-3.5" />
              <span>1. Market State</span>
            </span>
            <ChevronRight className="w-3.5 h-3.5 text-zinc-600 group-hover:text-cyan-400 transition-colors" />
          </div>
          <div className="text-xs font-semibold text-zinc-200 truncate">
            {btcData ? `BTC: $${btcData.spot_price.toLocaleString()}` : 'BTC: Evaluating'}
          </div>
          <div className="text-[10px] font-mono text-zinc-400 truncate">
            Regime: <span className="text-cyan-300 font-semibold">{btcRegime.slice(0, 14)}</span>
          </div>
          <div className="text-[10px] text-zinc-500 truncate">
            Basis: {btcData ? `${btcData.basis_zscore > 0 ? '+' : ''}${btcData.basis_zscore.toFixed(2)}σ` : '0.00σ'}
          </div>
        </button>

        {/* Step 2: Strategy Intent & Opportunities */}
        <button
          type="button"
          onClick={() => onNavigate('/strategies')}
          className="text-left p-3 rounded-xl bg-zinc-950/70 border border-zinc-800/80 hover:border-indigo-500/50 hover:bg-zinc-900/80 transition-all group cursor-pointer space-y-1.5"
        >
          <div className="flex items-center justify-between text-[10px] font-bold text-zinc-400 uppercase tracking-wider">
            <span className="flex items-center space-x-1 text-indigo-400">
              <Cpu className="w-3.5 h-3.5" />
              <span>2. Strategy Alpha</span>
            </span>
            <ChevronRight className="w-3.5 h-3.5 text-zinc-600 group-hover:text-indigo-400 transition-colors" />
          </div>
          <div className="text-xs font-semibold text-zinc-200 truncate">
            Grid: <span className="text-emerald-400">Active</span> • Trend: <span className="text-zinc-400">Wait</span>
          </div>
          <div className="text-[10px] font-mono text-zinc-400 truncate">
            Carry: <span className="text-emerald-400 font-semibold">+{btcData?.funding_annualized_pct.toFixed(1) || '8.2'}% APY</span>
          </div>
          <div className="text-[10px] text-zinc-500 truncate">
            Continuous Meta-Budget: 1.0x
          </div>
        </button>

        {/* Step 3: Portfolio Risk Governor */}
        <button
          type="button"
          onClick={() => onNavigate('/risk')}
          className={`text-left p-3 rounded-xl bg-zinc-950/70 border hover:bg-zinc-900/80 transition-all group cursor-pointer space-y-1.5 ${
            isRiskOk ? 'border-zinc-800/80 hover:border-emerald-500/50' : 'border-amber-800/80 hover:border-amber-500/50'
          }`}
        >
          <div className="flex items-center justify-between text-[10px] font-bold text-zinc-400 uppercase tracking-wider">
            <span className={`flex items-center space-x-1 ${isRiskOk ? 'text-emerald-400' : 'text-amber-400'}`}>
              {isRiskOk ? <ShieldCheck className="w-3.5 h-3.5" /> : <ShieldAlert className="w-3.5 h-3.5" />}
              <span>3. Risk Governor</span>
            </span>
            <ChevronRight className="w-3.5 h-3.5 text-zinc-600 group-hover:text-emerald-400 transition-colors" />
          </div>
          <div className="text-xs font-semibold text-zinc-200 truncate">
            Status: <span className={isRiskOk ? 'text-emerald-400 font-bold' : 'text-amber-400 font-bold'}>{account.risk_state}</span>
          </div>
          <div className="text-[10px] font-mono text-zinc-400 truncate">
            Margin: <span className="text-zinc-200">{account.margin_utilization_pct.toFixed(1)}%</span> / 25% max
          </div>
          <div className="text-[10px] text-zinc-500 truncate">
            Liq Buffer: <span className="text-emerald-400 font-mono">+{liquidationDistancePct.toFixed(1)}%</span>
          </div>
        </button>

        {/* Step 4: Target vs Physical Exposure */}
        <button
          type="button"
          onClick={() => onNavigate('/orders')}
          className="text-left p-3 rounded-xl bg-zinc-950/70 border border-zinc-800/80 hover:border-cyan-500/50 hover:bg-zinc-900/80 transition-all group cursor-pointer space-y-1.5"
        >
          <div className="flex items-center justify-between text-[10px] font-bold text-zinc-400 uppercase tracking-wider">
            <span className="flex items-center space-x-1 text-cyan-400">
              <Layers className="w-3.5 h-3.5" />
              <span>4. Target Delta</span>
            </span>
            <ChevronRight className="w-3.5 h-3.5 text-zinc-600 group-hover:text-cyan-400 transition-colors" />
          </div>
          <div className="text-xs font-semibold text-zinc-200 truncate">
            Lev: <span className="text-cyan-300 font-mono">{account.effective_leverage.toFixed(2)}x</span> (Safe)
          </div>
          <div className="text-[10px] font-mono text-zinc-400 truncate">
            Used Margin: <span className="text-zinc-200 font-mono">${account.used_margin.toLocaleString()}</span>
          </div>
          <div className="text-[10px] text-zinc-500 truncate">
            Execution: Passive Maker
          </div>
        </button>

        {/* Step 5: Active Baskets & Recovery */}
        <button
          type="button"
          onClick={() => onNavigate('/positions')}
          className={`text-left p-3 rounded-xl bg-zinc-950/70 border hover:bg-zinc-900/80 transition-all group cursor-pointer space-y-1.5 ${
            recoveringBaskets.length > 0
              ? 'border-orange-800/80 hover:border-orange-500/50'
              : 'border-zinc-800/80 hover:border-emerald-500/50'
          }`}
        >
          <div className="flex items-center justify-between text-[10px] font-bold text-zinc-400 uppercase tracking-wider">
            <span className={`flex items-center space-x-1 ${recoveringBaskets.length > 0 ? 'text-orange-400' : 'text-emerald-400'}`}>
              <Boxes className="w-3.5 h-3.5" />
              <span>5. Baskets ({baskets.length})</span>
            </span>
            <ChevronRight className="w-3.5 h-3.5 text-zinc-600 group-hover:text-emerald-400 transition-colors" />
          </div>
          <div className="text-xs font-semibold text-zinc-200 truncate">
            Net PnL: <span className={totalNetPnl >= 0 ? 'text-emerald-400 font-mono' : 'text-rose-400 font-mono'}>
              {totalNetPnl >= 0 ? '+' : ''}${totalNetPnl.toFixed(2)}
            </span>
          </div>
          <div className="text-[10px] font-mono text-zinc-400 truncate">
            Max Depth: <span className="text-zinc-200">{maxDepthReached} / 5</span> levels
          </div>
          <div className="text-[10px] text-zinc-500 truncate">
            Recovery: {recoveringBaskets.length > 0 ? `${recoveringBaskets.length} Active` : 'None (Healthy)'}
          </div>
        </button>
      </div>
    </div>
  );
};
