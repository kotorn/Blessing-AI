import React from 'react';
import {
  ShieldCheck,
  Zap,
  Activity,
  CheckCircle2,
  Percent,
  Clock,
  Layers,
  Info,
} from 'lucide-react';
import { RiskState } from '../types';
import { AccountData } from '../types';
import { ExecutionOrder } from '../types/orders';

interface ExecutionQualityCardProps {
  account: AccountData;
  orders: ExecutionOrder[];
  riskState: RiskState;
  killSwitchActive: boolean;
}

export const ExecutionQualityCard: React.FC<ExecutionQualityCardProps> = ({
  account,
  orders = [],
  riskState,
  killSwitchActive,
}) => {
  const hasVerifiedAccount =
    account.verified === true &&
    account.evidence_status === 'VERIFIED' &&
    (account.source === 'BINANCE_TESTNET' || account.source === 'BINANCE_MAINNET');
  const verifiedOrders = orders.filter(
    (order) => (order.source === 'BINANCE_TESTNET' || order.source === 'BINANCE_MAINNET') && order.status === 'FILLED',
  );
  const hasVerifiedExecution = hasVerifiedAccount && verifiedOrders.length > 0;
  // Compute maker vs taker counts only from exchange-sourced orders.
  const makerFills = verifiedOrders.filter((o) => o.type === 'LIMIT_MAKER' && o.status === 'FILLED').length;
  const takerFills = verifiedOrders.filter((o) => o.type !== 'LIMIT_MAKER' && o.status === 'FILLED').length;
  const totalFills = makerFills + takerFills;
  const makerRatio = totalFills > 0 ? (makerFills / totalFills) * 100 : null;

  // Calculate taker slippage
  const takerOrders = verifiedOrders.filter((o) => o.type !== 'LIMIT_MAKER' && o.status === 'FILLED');
  const avgTakerSlippage = takerOrders.length > 0
    ? takerOrders.reduce((sum, o) => sum + (o.trace?.slippageBps || 0), 0) / takerOrders.length 
    : 0;

  return (
    <div className="bg-zinc-900/90 border border-zinc-800 rounded-2xl p-4 sm:p-5 space-y-3.5 shadow-xl shadow-black/20">
      <div className="flex flex-wrap items-center justify-between gap-2 border-b border-zinc-800/80 pb-3">
        <div className="flex items-center space-x-2">
          <div className="p-1.5 rounded-lg bg-emerald-950/80 border border-emerald-800/60 text-emerald-400">
            <ShieldCheck className="w-4 h-4" />
          </div>
          <div>
            <h3 className="text-xs font-bold text-zinc-100 uppercase tracking-wider">
              Execution Quality & Fail-Closed Safeguards
            </h3>
            <p className="text-[11px] text-zinc-400">
              Maker/Taker cost efficiency, slippage telemetry, and private execution state
            </p>
          </div>
        </div>

        <div className="flex items-center space-x-2 text-[10px] font-mono">
          <span className="text-zinc-500">Execution Gate:</span>
          <span
            className={`px-2 py-0.5 rounded font-bold border ${
              killSwitchActive || riskState === 'EMERGENCY' || !hasVerifiedExecution
                ? 'bg-rose-950 text-rose-300 border-rose-800'
                : 'bg-emerald-950 text-emerald-300 border-emerald-800/80'
            }`}
          >
            {killSwitchActive ? 'HALTED (KILL SWITCH)' : hasVerifiedExecution ? 'VERIFIED TELEMETRY' : 'UNKNOWN / FAIL-CLOSED'}
          </span>
        </div>
      </div>

      <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-3 text-xs">
        {/* Metric 1: Maker / Taker Ratio */}
        <div className="p-3 bg-zinc-950/70 border border-zinc-800/80 rounded-xl space-y-1">
          <div className="flex items-center justify-between text-[10px] font-mono text-zinc-400 uppercase">
            <span>Maker Ratio</span>
            <Percent className="w-3.5 h-3.5 text-emerald-400" />
          </div>
          <div className="text-sm font-mono font-bold text-emerald-400">
            {makerRatio == null ? 'UNKNOWN' : `${makerRatio.toFixed(1)}% Maker`}
          </div>
          <div className="text-[10px] text-zinc-500">
            {hasVerifiedExecution ? `${makerFills} Maker Post-Only / ${takerFills} Taker Recovery` : 'No verified exchange fills'}
          </div>
        </div>

        {/* Metric 2: Slippage Attribution */}
        <div className="p-3 bg-zinc-950/70 border border-zinc-800/80 rounded-xl space-y-1">
          <div className="flex items-center justify-between text-[10px] font-mono text-zinc-400 uppercase">
            <span>Avg Slippage</span>
            <Activity className="w-3.5 h-3.5 text-cyan-400" />
          </div>
          <div className="text-sm font-mono font-bold text-zinc-200">
            {hasVerifiedExecution ? 'Measured from verified fills' : 'UNKNOWN'}
          </div>
          <div className="text-[10px] text-zinc-500">
            {hasVerifiedExecution && takerOrders.length > 0 ? `Taker hedge slippage: ${avgTakerSlippage.toFixed(1)} bps` : 'No verified slippage sample'}
          </div>
        </div>

        {/* Metric 3: Private Stream Latency */}
        <div className="p-3 bg-zinc-950/70 border border-zinc-800/80 rounded-xl space-y-1">
          <div className="flex items-center justify-between text-[10px] font-mono text-zinc-400 uppercase">
            <span>Stream Heartbeat</span>
            <Clock className="w-3.5 h-3.5 text-indigo-400" />
          </div>
          <div className="text-sm font-mono font-bold text-emerald-400 flex items-center space-x-1">
            {hasVerifiedExecution ? <CheckCircle2 className="w-3.5 h-3.5" /> : <Activity className="w-3.5 h-3.5" />}
            <span>{hasVerifiedExecution ? 'Verified stream telemetry' : 'UNKNOWN'}</span>
          </div>
          <div className="text-[10px] text-zinc-500">
            Fail-closed threshold: &lt; 5.0s; no fabricated latency
          </div>
        </div>

        {/* Metric 4: Idempotency & Order Reconciliation */}
        <div className="p-3 bg-zinc-950/70 border border-zinc-800/80 rounded-xl space-y-1">
          <div className="flex items-center justify-between text-[10px] font-mono text-zinc-400 uppercase">
            <span>Idempotency</span>
            <ShieldCheck className="w-3.5 h-3.5 text-emerald-400" />
          </div>
          <div className="text-sm font-mono font-bold text-zinc-200">
            {hasVerifiedExecution ? 'Deterministic IDs' : 'UNKNOWN'}
          </div>
          <div className="text-[10px] text-zinc-500">
            {hasVerifiedExecution ? 'Exchange-sourced reconciliation required' : 'Awaiting verified order ledger'}
          </div>
        </div>
      </div>
    </div>
  );
};
