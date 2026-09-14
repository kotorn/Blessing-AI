import React from 'react';
import { AccountOverview } from '../components/AccountOverview';
import { CommandDecisionFlow } from '../components/CommandDecisionFlow';
import { ExposureAttributionCard } from '../components/ExposureAttributionCard';
import { InstrumentsPanel } from '../components/InstrumentsPanel';
import { BasketManager } from '../components/BasketManager';
import { RiskGovernorMonitor } from '../components/RiskGovernorMonitor';
import { AccountData, BasketItem, InstrumentData, RiskRuleItem, TradingSystemState } from '../types';
import { AppRoute } from '../contracts/system';
import { AlertCircle, ArrowUpRight, Flame } from 'lucide-react';

interface CommandCenterPageProps {
  account: AccountData;
  systemState: TradingSystemState | null;
  onAccountUpdated: (account: AccountData) => void;
  instruments: Record<string, InstrumentData>;
  baskets: BasketItem[];
  riskRules: RiskRuleItem[];
  correlationBtcEth: number | null;
  cryptoBetaExposurePct: number | null;
  liquidationDistancePct: number | null;
  isActionLoading: boolean;
  onExpandGrid: (basketId: string) => Promise<void>;
  onEnterRecovery: (basketId: string) => Promise<void>;
  onCloseBasket: (basketId: string) => Promise<void>;
  onOpenBalanceModal: () => void;
  onNavigate: (route: AppRoute) => void;
}

export const CommandCenterPage: React.FC<CommandCenterPageProps> = ({
  account,
  systemState,
  onAccountUpdated,
  instruments,
  baskets,
  riskRules,
  correlationBtcEth,
  cryptoBetaExposurePct,
  liquidationDistancePct,
  isActionLoading,
  onExpandGrid,
  onEnterRecovery,
  onCloseBasket,
  onOpenBalanceModal,
  onNavigate,
}) => {
  // Operational triage: check if any basket requires fast intervention
  const deepBaskets = baskets.filter((b) => b.grid_depth >= 4);
  const recoveringBaskets = baskets.filter((b) => b.state === 'RECOVERY' || b.state === 'DELEVERAGING');

  return (
    <div className="space-y-6">
      {/* 1. Primary KPI Strip */}
      <AccountOverview
        account={account}
        onAccountUpdated={onAccountUpdated}
        onOpenBalanceModal={onOpenBalanceModal}
        onNavigateTab={(tab) => {
          if (tab === 'wallet') onNavigate('/portfolio');
          else if (tab === 'cockpit') onNavigate('/command');
          else if (tab === 'backtest') onNavigate('/research/replay');
          else if (tab === 'bigquery') onNavigate('/analytics');
          else if (tab === 'architecture') onNavigate('/settings');
        }}
      />

      {/* 2. Interactive Operational Decision Pipeline */}
      <CommandDecisionFlow
        account={account}
        systemState={systemState}
        instruments={instruments}
        baskets={baskets}
        riskRules={riskRules}
        liquidationDistancePct={liquidationDistancePct}
        onNavigate={onNavigate}
      />

      {/* 3. Operational Attention Triage Banner (if deep grid or recovery active) */}
      {(deepBaskets.length > 0 || recoveringBaskets.length > 0) && (
        <div className="p-3.5 bg-amber-950/40 border border-amber-800/80 rounded-xl flex flex-wrap items-center justify-between gap-3 text-xs text-amber-200">
          <div className="flex items-center space-x-2.5">
            <AlertCircle className="w-4 h-4 text-amber-400 shrink-0" />
            <div>
              <span className="font-bold text-amber-300">Operator Attention Required: </span>
              <span>
                {deepBaskets.length > 0 && `${deepBaskets.length} basket(s) reaching deep grid levels (≥4). `}
                {recoveringBaskets.length > 0 && `${recoveringBaskets.length} basket(s) in active counter-trend recovery.`}
              </span>
            </div>
          </div>
          <div className="flex items-center space-x-2">
            <button
              type="button"
              onClick={() => onNavigate('/positions')}
              className="flex items-center space-x-1 px-3 py-1.5 bg-amber-900/60 hover:bg-amber-800/80 border border-amber-700/80 rounded-lg text-amber-100 font-semibold cursor-pointer"
            >
              <span>Manage Baskets</span>
              <ArrowUpRight className="w-3.5 h-3.5" />
            </button>
          </div>
        </div>
      )}

      {/* 4. Target vs Actual Exposure & Risk Attribution */}
      <ExposureAttributionCard
        account={account}
        baskets={baskets}
        correlationBtcEth={correlationBtcEth}
        cryptoBetaExposurePct={cryptoBetaExposurePct}
        liquidationDistancePct={liquidationDistancePct}
        onOpenBalanceModal={onOpenBalanceModal}
        onNavigate={onNavigate}
      />

      {/* 5. Market Opportunity & Regimes */}
      <div className="space-y-2">
        <div className="flex items-center justify-between">
          <h4 className="text-xs font-bold text-zinc-300 uppercase tracking-wider">
            Market State & Price Action Board
          </h4>
          <button
            type="button"
            onClick={() => onNavigate('/markets')}
            className="text-xs text-cyan-400 hover:text-cyan-300 flex items-center space-x-1 font-medium cursor-pointer"
          >
            <span>Expanded Scanner</span>
            <ArrowUpRight className="w-3 h-3" />
          </button>
        </div>
        <InstrumentsPanel instruments={instruments} />
      </div>

      {/* 6. Active Geometric Baskets & Risk Governor */}
      <div className="grid grid-cols-1 lg:grid-cols-3 gap-6">
        <div className="lg:col-span-2 space-y-2">
          <div className="flex items-center justify-between">
            <h4 className="text-xs font-bold text-zinc-300 uppercase tracking-wider">
              Active Geometric Grid Baskets ({baskets.length})
            </h4>
            <button
              type="button"
              onClick={() => onNavigate('/positions')}
              className="text-xs text-cyan-400 hover:text-cyan-300 flex items-center space-x-1 font-medium cursor-pointer"
            >
              <span>Positions & Baskets Workspace</span>
              <ArrowUpRight className="w-3 h-3" />
            </button>
          </div>
          <BasketManager
            baskets={baskets}
            onExpandGrid={onExpandGrid}
            onEnterRecovery={onEnterRecovery}
            onCloseBasket={onCloseBasket}
            isActionLoading={isActionLoading}
          />
        </div>

        <div className="lg:col-span-1 space-y-2">
          <div className="flex items-center justify-between">
            <h4 className="text-xs font-bold text-zinc-300 uppercase tracking-wider">
              Risk Governor & Invariants
            </h4>
            <button
              type="button"
              onClick={() => onNavigate('/risk')}
              className="text-xs text-indigo-400 hover:text-indigo-300 flex items-center space-x-1 font-medium cursor-pointer"
            >
              <span>Risk & Recovery Workspace</span>
              <ArrowUpRight className="w-3 h-3" />
            </button>
          </div>
          <RiskGovernorMonitor
            riskState={account.risk_state}
            rules={riskRules}
            correlationBtcEth={correlationBtcEth}
            cryptoBetaExposurePct={cryptoBetaExposurePct}
            liquidationDistancePct={liquidationDistancePct}
          />
        </div>
      </div>
    </div>
  );
};

