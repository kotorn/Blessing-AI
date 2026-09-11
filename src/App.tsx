import React, { useState, useEffect } from 'react';
import { Header } from './components/Header';
import { AccountOverview } from './components/AccountOverview';
import { InstrumentsPanel } from './components/InstrumentsPanel';
import { BasketManager } from './components/BasketManager';
import { RiskGovernorMonitor } from './components/RiskGovernorMonitor';
import { BacktestReplayStudio } from './components/BacktestReplayStudio';
import { AIQuantCopilot } from './components/AIQuantCopilot';
import { BigQueryLakehouse } from './components/BigQueryLakehouse';
import { ArchitectureReviewViewer } from './components/ArchitectureReviewViewer';
import { BalanceAllocation } from './components/BalanceAllocation';
import { BalanceAllocationModal } from './components/BalanceAllocationModal';
import { AccountData, BasketItem, InstrumentData, RiskRuleItem, RiskState } from './types';
import { useAuth } from './context/AuthContext';

export default function App() {
  const { user, cloudAudit, saveCloudBasket } = useAuth();
  const [activeTab, setActiveTab] = useState<'cockpit' | 'wallet' | 'backtest' | 'copilot' | 'bigquery' | 'architecture'>('cockpit');
  const [showBalanceModal, setShowBalanceModal] = useState<boolean>(false);
  const [account, setAccount] = useState<AccountData>({
    equity: 50720.50,
    balance: 50000.00,
    margin_utilization_pct: 14.2,
    effective_leverage: 0.85,
    free_margin: 43518.19,
    used_margin: 7202.31,
    daily_pnl: 720.50,
    daily_pnl_pct: 1.44,
    portfolio_drawdown_pct: 1.15,
    kill_switch_active: false,
    risk_state: 'NORMAL',
  });

  const [baskets, setBaskets] = useState<BasketItem[]>([]);
  const [instruments, setInstruments] = useState<Record<string, InstrumentData>>({});
  const [riskRules, setRiskRules] = useState<RiskRuleItem[]>([]);
  const [correlationBtcEth, setCorrelationBtcEth] = useState<number>(0.74);
  const [cryptoBetaExposurePct, setCryptoBetaExposurePct] = useState<number>(42.8);
  const [liquidationDistancePct, setLiquidationDistancePct] = useState<number>(38.5);
  const [isActionLoading, setIsActionLoading] = useState<boolean>(false);

  // Fetch live state from mock quant engine
  const fetchState = async () => {
    try {
      const resp = await fetch('/api/quant/state', {
        headers: { Accept: 'application/json' },
      });
      const contentType = resp.headers.get('content-type');
      if (!resp.ok || !contentType || !contentType.includes('application/json')) {
        // Dev server or reverse proxy is initializing or returned HTML
        return;
      }
      const data = await resp.json();
      if (!data || typeof data !== 'object') {
        return;
      }
      if (data.account) {
        setAccount(data.account);
      }
      if (Array.isArray(data.baskets)) {
        setBaskets(data.baskets);
      }
      if (data.instruments) {
        setInstruments(data.instruments);
      }
      const parsedRules = Array.isArray(data.risk_rules)
        ? data.risk_rules
        : [
            ...(data.risk_rules?.hard_rules || []),
            ...(data.risk_rules?.soft_rules || []),
          ];
      if (parsedRules.length > 0) {
        setRiskRules(parsedRules);
      }
      const corr = typeof data.correlation_btc_eth === 'number'
        ? data.correlation_btc_eth
        : (typeof data.correlations?.btc_eth_rolling_corr === 'number'
            ? data.correlations.btc_eth_rolling_corr
            : 0.74);
      setCorrelationBtcEth(corr);

      const beta = typeof data.crypto_beta_exposure_pct === 'number'
        ? data.crypto_beta_exposure_pct
        : (typeof data.correlations?.crypto_beta_exposure_pct === 'number'
            ? data.correlations.crypto_beta_exposure_pct
            : 42.8);
      setCryptoBetaExposurePct(beta);

      const liq = typeof data.liquidation_distance_pct === 'number'
        ? data.liquidation_distance_pct
        : 38.5;
      setLiquidationDistancePct(liq);
    } catch (e) {
      // Defer next polling tick quietly during server restart or reload
      console.warn('Quant state polling deferred:', (e as any)?.message || e);
    }
  };

  useEffect(() => {
    fetchState();
    const interval = setInterval(fetchState, 3000);
    return () => clearInterval(interval);
  }, []);

  const handleToggleKillSwitch = async () => {
    const nextState = !account.kill_switch_active;
    cloudAudit(nextState ? 'KILL_SWITCH_ENGAGED' : 'KILL_SWITCH_DISENGAGED', 'PORTFOLIO', 'User toggled master kill switch');
    try {
      const resp = await fetch('/api/quant/risk/kill-switch', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
        body: JSON.stringify({ active: nextState }),
      });
      if (resp.ok) {
        await fetchState();
      } else {
        setAccount(prev => ({
          ...prev,
          kill_switch_active: !prev.kill_switch_active,
          risk_state: !prev.kill_switch_active ? 'EMERGENCY' : 'NORMAL',
        }));
      }
    } catch {
      setAccount(prev => ({
        ...prev,
        kill_switch_active: !prev.kill_switch_active,
        risk_state: !prev.kill_switch_active ? 'EMERGENCY' : 'NORMAL',
      }));
    }
  };

  const handleExpandGrid = async (basketId: string) => {
    setIsActionLoading(true);
    cloudAudit('EXPAND_GRID', basketId, 'Manual grid level expansion requested');
    try {
      const resp = await fetch('/api/quant/basket/expand', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
        body: JSON.stringify({ basket_id: basketId }),
      });
      if (resp.ok) {
        await fetchState();
      }
    } catch (e) {
      console.warn('Expand grid action deferred:', e);
    } finally {
      setIsActionLoading(false);
    }
  };

  const handleEnterRecovery = async (basketId: string) => {
    setIsActionLoading(true);
    cloudAudit('ENTER_RECOVERY', basketId, 'Initiated dynamic counter-trend exposure recovery');
    try {
      const resp = await fetch('/api/quant/basket/recovery', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
        body: JSON.stringify({ basket_id: basketId }),
      });
      if (resp.ok) {
        await fetchState();
      }
    } catch (e) {
      console.warn('Recovery action deferred:', e);
    } finally {
      setIsActionLoading(false);
    }
  };

  const handleCloseBasket = async (basketId: string) => {
    setIsActionLoading(true);
    cloudAudit('CLOSE_BASKET', basketId, 'Market flatten and close basket requested');
    try {
      const resp = await fetch('/api/quant/basket/close', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
        body: JSON.stringify({ basket_id: basketId }),
      });
      if (resp.ok) {
        await fetchState();
      }
    } catch (e) {
      console.warn('Close basket action deferred:', e);
    } finally {
      setIsActionLoading(false);
    }
  };

  return (
    <div className="min-h-screen bg-zinc-950 text-zinc-100 selection:bg-indigo-500/30 selection:text-indigo-200">
      <Header
        riskState={account.risk_state}
        killSwitchActive={account.kill_switch_active}
        onToggleKillSwitch={handleToggleKillSwitch}
        activeTab={activeTab}
        setActiveTab={setActiveTab}
        onOpenWalletModal={() => setShowBalanceModal(true)}
        baskets={baskets}
        portfolioState={account}
      />

      <main className="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 py-6 space-y-6">
        {/* Account Overview on top of Cockpit and analytical tabs */}
        {activeTab !== 'wallet' && (
          <AccountOverview
            account={account}
            onAccountUpdated={setAccount}
            onOpenBalanceModal={() => setShowBalanceModal(true)}
            onNavigateTab={setActiveTab}
          />
        )}

        {/* Tab-driven workspaces */}
        {activeTab === 'cockpit' && (
          <div className="space-y-6">
            <InstrumentsPanel instruments={instruments} />
            <div className="grid grid-cols-1 lg:grid-cols-3 gap-6">
              <div className="lg:col-span-2">
                <BasketManager
                  baskets={baskets}
                  onExpandGrid={handleExpandGrid}
                  onEnterRecovery={handleEnterRecovery}
                  onCloseBasket={handleCloseBasket}
                  isActionLoading={isActionLoading}
                />
              </div>
              <div className="lg:col-span-1">
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
        )}

        {activeTab === 'wallet' && (
          <BalanceAllocation
            account={account}
            onAccountUpdated={setAccount}
            onNavigateTab={setActiveTab}
          />
        )}

        {activeTab === 'backtest' && <BacktestReplayStudio />}

        {activeTab === 'copilot' && <AIQuantCopilot />}

        {activeTab === 'bigquery' && <BigQueryLakehouse />}

        {activeTab === 'architecture' && <ArchitectureReviewViewer />}
      </main>

      {/* Pop-up Modal for Balance & Sub-Wallet Allocation */}
      <BalanceAllocationModal
        isOpen={showBalanceModal}
        onClose={() => setShowBalanceModal(false)}
        account={account}
        onAccountUpdated={setAccount}
        onNavigateTab={setActiveTab}
      />
    </div>
  );
}
