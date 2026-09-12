import React, { useState } from 'react';
import { RiskRuleItem, RiskState, BasketItem } from '../types';
import { ShieldCheck, Lock, LifeBuoy, ShieldAlert, Info, AlertTriangle, Power } from 'lucide-react';
import { HardRiskBoundsCard } from '../components/HardRiskBoundsCard';
import { ExposureRecoveryEngineCard } from '../components/ExposureRecoveryEngineCard';
import { FailClosedSafeguardsCard } from '../components/FailClosedSafeguardsCard';
import { RiskGovernorMonitor } from '../components/RiskGovernorMonitor';

interface RiskRecoveryPageProps {
  riskState: RiskState;
  rules: RiskRuleItem[];
  correlationBtcEth: number;
  cryptoBetaExposurePct: number;
  liquidationDistancePct: number;
  baskets?: BasketItem[];
  recoveryData?: any;
  killSwitchActive: boolean;
  onToggleKillSwitch: () => void;
}

export const RiskRecoveryPage: React.FC<RiskRecoveryPageProps> = ({
  riskState,
  rules,
  correlationBtcEth,
  cryptoBetaExposurePct,
  liquidationDistancePct,
  baskets = [],
  recoveryData,
  killSwitchActive,
  onToggleKillSwitch,
}) => {

  return (
    <div className="space-y-6">
      {/* Page Header with Emergency Kill Switch */}
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h2 className="text-xl font-bold text-zinc-100 flex items-center space-x-2">
            <ShieldCheck className="w-5 h-5 text-emerald-400" />
            <span>Portfolio Risk Governor & Capital Preservation</span>
          </h2>
          <p className="text-xs text-zinc-400 mt-1">
            Deterministic hard boundaries, dynamic exposure recovery, fail-closed guards, and emergency capital protection.
          </p>
        </div>

        <div className="flex items-center space-x-2.5">
          <button
            onClick={onToggleKillSwitch}
            className={`px-3 py-1.5 rounded-xl text-xs font-mono font-bold border transition-all flex items-center space-x-1.5 ${
              killSwitchActive
                ? 'bg-rose-600 text-white border-rose-500 shadow-lg shadow-rose-900/40'
                : 'bg-rose-950/40 hover:bg-rose-900/60 text-rose-300 border-rose-800/80'
            }`}
          >
            <Power className="w-3.5 h-3.5" />
            <span>{killSwitchActive ? 'EMERGENCY KILL SWITCH ENGAGED' : 'ENGAGE KILL SWITCH'}</span>
          </button>
        </div>
      </div>

      {/* Hard Constraint Priority Banner */}
      <div className="p-3.5 rounded-xl bg-indigo-950/50 border border-indigo-800/60 text-xs text-indigo-200 flex items-start space-x-3">
        <Lock className="w-4 h-4 text-indigo-400 shrink-0 mt-0.5" />
        <div className="space-y-0.5">
          <span className="font-bold text-zinc-100">
            Mandatory Governance Hierarchy (PLAN.md Section 15-17):
          </span>
          <p className="text-[11px] text-zinc-300 leading-relaxed">
            The Risk Governor sits strictly above all Alpha Strategies, ML Models, Meta Allocators, and Exposure Recovery algorithms. No strategy or machine learning output may ever override, expand, or waive hard risk constraints.
          </p>
        </div>
      </div>

      {/* 1. Deterministic Hard Risk Bounds */}
      <HardRiskBoundsCard
        riskState={riskState}
        rules={rules}
        liquidationDistancePct={liquidationDistancePct}
      />

      {/* 2. Dynamic Exposure Recovery Decision Engine */}
      <ExposureRecoveryEngineCard baskets={baskets} recoveryData={recoveryData || ((window as any).quantState as any)?.exposure_recovery} />

      {/* 3. Cross-Instrument Correlation & Beta Exposure Monitor */}
      <RiskGovernorMonitor
        riskState={riskState}
        rules={rules}
        correlationBtcEth={correlationBtcEth}
        cryptoBetaExposurePct={cryptoBetaExposurePct}
        liquidationDistancePct={liquidationDistancePct}
      />

      {/* 4. Fail-Closed Infrastructure Telemetry */}
      <FailClosedSafeguardsCard
        killSwitchActive={killSwitchActive}
        onTriggerKillSwitch={onToggleKillSwitch}
      />

      {/* Data Contract Lineage Footer */}
      <div className="p-4 rounded-xl bg-zinc-900/60 border border-zinc-800/80 text-xs text-zinc-400 space-y-2">
        <div className="flex items-center space-x-2 text-zinc-300 font-semibold">
          <Info className="w-4 h-4 text-cyan-400" />
          <span>Risk & Recovery Contract Lineage (UI-07)</span>
        </div>
        <div className="grid grid-cols-1 md:grid-cols-3 gap-2 font-mono text-[11px]">
          <div className="p-2 bg-zinc-950 rounded border border-zinc-800">
            <span className="text-emerald-400 font-bold block">EXISTING (/api/quant/state):</span>
            `risk_state`, deterministic rules invariant checklist, correlation, beta exposure.
          </div>
          <div className="p-2 bg-zinc-950 rounded border border-zinc-800">
            <span className="text-cyan-400 font-bold block">DERIVED_FRONTEND:</span>
            Exposure recovery comparative decision matrix ('Reduce' vs 'Hedge'), fail-closed guards.
          </div>
          <div className="p-2 bg-zinc-950 rounded border border-zinc-800">
            <span className="text-amber-400 font-bold block">PROPOSED_BACKEND (Epic UI-07):</span>
            Async emergency kill switch NATS topic (`risk.kill_switch`) and private stream heartbeat.
          </div>
        </div>
      </div>
    </div>
  );
};
