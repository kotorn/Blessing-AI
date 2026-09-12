import React from 'react';
import { ShieldCheck, ShieldAlert, CheckCircle2, AlertTriangle, XCircle, Activity, Lock } from 'lucide-react';
import { RiskRuleItem, RiskState } from '../types';

interface RiskGovernorMonitorProps {
  riskState: RiskState;
  rules: RiskRuleItem[];
  correlationBtcEth: number;
  cryptoBetaExposurePct: number;
  liquidationDistancePct: number | null;
}

export const RiskGovernorMonitor: React.FC<RiskGovernorMonitorProps> = ({
  riskState,
  rules,
  correlationBtcEth,
  cryptoBetaExposurePct,
  liquidationDistancePct,
}) => {
  return (
    <div className="bg-zinc-900/80 border border-zinc-800 rounded-xl p-5 space-y-4">
      <div className="flex flex-wrap items-center justify-between gap-2 border-b border-zinc-800/80 pb-3">
        <div className="flex items-center space-x-2.5">
          <div className="p-2 rounded-lg bg-indigo-500/10 text-indigo-400">
            <Lock className="w-4 h-4" />
          </div>
          <div>
            <h3 className="font-bold text-zinc-100 text-sm">Portfolio Risk Governor (Sections 15 - 17)</h3>
            <p className="text-xs text-zinc-400">Hard rules are strictly deterministic and cannot be overridden by AI</p>
          </div>
        </div>

        <div className="flex items-center space-x-2">
          <span className="text-xs text-zinc-400">Governor State:</span>
          <span className="px-2.5 py-0.5 rounded-full text-xs font-bold border bg-zinc-800 text-zinc-200">
            {riskState}
          </span>
        </div>
      </div>

      {/* Cross-Instrument Correlation & Beta Exposure */}
      <div className="grid grid-cols-1 sm:grid-cols-3 gap-3 bg-zinc-950/60 p-3 rounded-lg border border-zinc-800/60 text-xs">
        <div>
          <div className="text-zinc-400 text-[11px]">BTC-ETH Rolling Correlation (30d)</div>
          <div className="text-base font-bold font-mono text-zinc-200 mt-0.5">
            {(correlationBtcEth ?? 0).toFixed(2)}
          </div>
          <div className="text-[10px] text-zinc-400">
            {(correlationBtcEth ?? 0) > 0.85 ? 'High Co-movement Alert' : 'Normal Cross-Beta'}
          </div>
        </div>

        <div>
          <div className="text-zinc-400 text-[11px]">Aggregate Crypto Beta Exposure</div>
          <div className="text-base font-bold font-mono text-zinc-200 mt-0.5">
            {(cryptoBetaExposurePct ?? 0).toFixed(1)}%
          </div>
          <div className="text-[10px] text-zinc-400">Target Ceiling: &le; 65.0%</div>
        </div>

        <div>
          <div className="text-zinc-400 text-[11px]">Liquidation Distance Buffer</div>
          <div className={`text-base font-bold font-mono ${liquidationDistancePct == null ? 'text-amber-400' : 'text-emerald-400'} mt-0.5`}>
            {liquidationDistancePct == null ? 'UNKNOWN' : `+${liquidationDistancePct.toFixed(1)}%`}
          </div>
          <div className="text-[10px] text-zinc-400">
            {liquidationDistancePct == null ? 'Awaiting verified position risk snapshot' : 'Hard Safety Floor: 25.0%'}
          </div>
        </div>
      </div>

      {/* Rules Invariant Matrix */}
      <div className="space-y-2">
        <div className="text-xs font-semibold text-zinc-400 uppercase tracking-wider">
          Runtime Invariants & Hard Safety Bounds
        </div>
        <div className="grid grid-cols-1 md:grid-cols-2 gap-2">
          {(rules || []).map((item, idx) => {
            const isPass = item.status === 'PASS';
            const isWarn = item.status === 'WARN';
            const isUnknown = item.status === 'UNKNOWN';
            return (
              <div
                key={idx}
                className="flex items-center justify-between p-2.5 rounded-lg bg-zinc-950/40 border border-zinc-800/80 text-xs"
              >
                <div className="flex items-center space-x-2">
                  {isPass ? (
                    <CheckCircle2 className="w-3.5 h-3.5 text-emerald-400 shrink-0" />
                  ) : isWarn || isUnknown ? (
                    <AlertTriangle className="w-3.5 h-3.5 text-amber-400 shrink-0" />
                  ) : (
                    <XCircle className="w-3.5 h-3.5 text-rose-500 shrink-0" />
                  )}
                  <span className="text-zinc-300 font-medium">{item.rule}</span>
                </div>
                <div className="flex items-center space-x-2">
                  <span className="font-mono text-zinc-400 text-[11px]">{item.current}</span>
                  <span
                    className={`px-1.5 py-0.5 rounded text-[10px] font-bold ${
                      isPass
                        ? 'bg-emerald-500/10 text-emerald-400'
                        : isWarn || isUnknown
                        ? 'bg-amber-500/10 text-amber-400'
                        : 'bg-rose-500/10 text-rose-400'
                    }`}
                  >
                    {item.status}
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
