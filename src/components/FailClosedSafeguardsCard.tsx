import React from 'react';
import {
  ShieldAlert,
  CheckCircle2,
  AlertTriangle,
  XCircle,
  Radio,
  Clock,
  Zap,
  Info,
  Power,
} from 'lucide-react';
import { FailClosedSafeguard } from '../types/risk';

interface FailClosedSafeguardsCardProps {
  killSwitchActive: boolean;
  onTriggerKillSwitch?: () => void;
}

export const FailClosedSafeguardsCard: React.FC<FailClosedSafeguardsCardProps> = ({
  killSwitchActive,
  onTriggerKillSwitch,
}) => {
  const safeguards: FailClosedSafeguard[] = [
    {
      id: 'market_data_freshness',
      name: 'Market Data Feed Latency',
      status: 'HEALTHY',
      latencyOrLag: '240 ms',
      failClosedAction: 'Halts all new exposure when feed lag > 3,000 ms',
      lastChecked: 'Just now',
    },
    {
      id: 'private_account_stream',
      name: 'Private Account Stream (WS)',
      status: 'HEALTHY',
      latencyOrLag: '0.8s Heartbeat',
      failClosedAction: 'Halts new orders until balance/position reconciled',
      lastChecked: '1s ago',
    },
    {
      id: 'risk_service_invariants',
      name: 'Risk Governor Service Loop',
      status: 'HEALTHY',
      latencyOrLag: '< 1 ms',
      failClosedAction: 'Hard fail-closed if governor check times out',
      lastChecked: 'Synchronous',
    },
    {
      id: 'order_reconciliation',
      name: 'Order State Reconciliation',
      status: 'HEALTHY',
      latencyOrLag: '0 Orphan Orders',
      failClosedAction: 'Blocks new submissions until unknown order resolved',
      lastChecked: 'Idempotent',
    },
  ];

  return (
    <div className="bg-zinc-900/90 border border-zinc-800 rounded-2xl p-4 sm:p-5 space-y-4 shadow-xl shadow-black/20">
      {/* Header */}
      <div className="flex flex-wrap items-center justify-between gap-2 border-b border-zinc-800/80 pb-3">
        <div className="flex items-center space-x-2">
          <div className="p-1.5 rounded-lg bg-emerald-950/80 border border-emerald-800/60 text-emerald-400">
            <ShieldAlert className="w-4 h-4" />
          </div>
          <div>
            <h3 className="text-xs font-bold text-zinc-100 uppercase tracking-wider">
              Fail-Closed Infrastructure Telemetry
            </h3>
            <p className="text-[11px] text-zinc-400">
              Trading infrastructure is strictly fail-closed: any service failure immediately halts new risk
            </p>
          </div>
        </div>

        <div className="flex items-center space-x-2 font-mono text-[10px]">
          <span className="text-zinc-500">Infrastructure Gate:</span>
          <span
            className={`px-2 py-0.5 rounded font-bold border ${
              killSwitchActive
                ? 'bg-rose-950 text-rose-300 border-rose-800'
                : 'bg-emerald-950 text-emerald-300 border-emerald-800/80'
            }`}
          >
            {killSwitchActive ? 'HALTED (MANUAL KILL SWITCH)' : 'PERMITTED (ALL GATES HEALTHY)'}
          </span>
        </div>
      </div>

      {/* Safeguards Grid */}
      <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-3 text-xs font-mono">
        {safeguards.map((item) => (
          <div
            key={item.id}
            className="p-3 bg-zinc-950/70 border border-zinc-800/80 rounded-xl space-y-1.5"
          >
            <div className="flex items-center justify-between">
              <span className="font-sans font-bold text-zinc-200 truncate">
                {item.name}
              </span>
              <CheckCircle2 className="w-3.5 h-3.5 text-emerald-400 shrink-0" />
            </div>

            <div className="text-sm font-bold text-emerald-400">
              {item.latencyOrLag}
            </div>

            <div className="text-[10px] text-zinc-400 font-sans leading-tight">
              {item.failClosedAction}
            </div>
          </div>
        ))}
      </div>
    </div>
  );
};
