import React, { useState, useEffect, useCallback } from 'react';
import { AppRoute, SystemMode } from '../contracts/system';
import { Sidebar } from './Sidebar';
import { StatusBar } from './StatusBar';
import { CopilotDrawer } from './CopilotDrawer';
import { AlertsDrawer } from './AlertsDrawer';
import { AppRoutes } from './Routes';
import { useQuantState } from '../hooks/useQuantState';
import { useAuth } from '../context/AuthContext';
import { BinanceProfileModal } from '../components/BinanceProfileModal';
import { BalanceAllocationModal } from '../components/BalanceAllocationModal';
import { GoogleCloudCenterModal } from '../components/GoogleCloudCenterModal';
import { GoogleWorkspaceModal } from '../components/GoogleWorkspaceModal';
import { StartTradingWizard } from '../components/StartTradingWizard';
import { ConfirmationModal } from '../components/ConfirmationModal';
import { ActionFeedback, ActionToast } from '../components/ActionToast';
import { binanceApi, BinanceKeyStatus } from '../api/binance';
import { Power } from 'lucide-react';

export const AppShell: React.FC = () => {
  const { cloudAudit, firestoreConnected } = useAuth();

  // Central Quant State Management
  const {
    systemState,
    account,
    baskets,
    instruments,
    orders,
    alerts,
    riskRules,
    strategyIntents,
    metaAllocations,
    exposureRecovery,
    correlationBtcEth,
    cryptoBetaExposurePct,
    liquidationDistancePct,
    isLoading,
    isActionLoading,
    lastUpdated,
    error,
    refresh,
    updateAccount,
    toggleKillSwitch,
    armEngine,
    togglePauseNewRisk,
    expandGrid,
    enterRecovery,
    closeBasket,
  } = useQuantState((action, entityId, details) => {
    cloudAudit(action, entityId, details);
  });

  // Navigation State
  const [currentRoute, setCurrentRoute] = useState<AppRoute>(() => {
    const hash = window.location.hash.replace('#', '') as AppRoute;
    if (hash && hash.startsWith('/')) return hash;
    return '/command';
  });

  // Layout states
  const [sidebarCollapsed, setSidebarCollapsed] = useState<boolean>(false);
  const [copilotOpen, setCopilotOpen] = useState<boolean>(false);
  const [alertsOpen, setAlertsOpen] = useState<boolean>(false);
  const [dismissedAlerts, setDismissedAlerts] = useState<Set<string>>(new Set());

  // Modals
  const [showBinanceModal, setShowBinanceModal] = useState<boolean>(false);
  const [showBalanceModal, setShowBalanceModal] = useState<boolean>(false);
  const [showGoogleCloudModal, setShowGoogleCloudModal] = useState<boolean>(false);
  const [showWorkspaceModal, setShowWorkspaceModal] = useState<boolean>(false);
  const [showStartTradingWizard, setShowStartTradingWizard] = useState<boolean>(false);
  const [showDisarmConfirm, setShowDisarmConfirm] = useState<boolean>(false);

  // Safety-critical actions must never fail silently (e.g. 401 while signed out)
  const [actionFeedback, setActionFeedback] = useState<ActionFeedback | null>(null);

  // Binance connection telemetry
  const [binanceStatus, setBinanceStatus] = useState<BinanceKeyStatus | null>(null);

  useEffect(() => {
    binanceApi.verifyKey().then((status) => {
      setBinanceStatus(status);
    }).catch((e) => {
      console.warn('Initial Binance verification check deferred:', e);
    });
  }, []);

  // Hash-based browser history sync
  const handleNavigate = useCallback((route: AppRoute) => {
    setCurrentRoute(route);
    window.location.hash = route;
  }, []);

  useEffect(() => {
    const handleHashChange = () => {
      const hash = window.location.hash.replace('#', '') as AppRoute;
      if (hash && hash.startsWith('/')) {
        setCurrentRoute(hash);
      }
    };
    window.addEventListener('hashchange', handleHashChange);
    return () => window.removeEventListener('hashchange', handleHashChange);
  }, []);

  // Keyboard shortcut: Ctrl/Cmd + B for Copilot
  useEffect(() => {
    const handleKeyDown = (e: KeyboardEvent) => {
      if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'k') {
        e.preventDefault();
        setCopilotOpen((prev) => !prev);
      }
    };
    window.addEventListener('keydown', handleKeyDown);
    return () => window.removeEventListener('keydown', handleKeyDown);
  }, []);

  // Authoritative system execution mode
  const workerUnavailable = !systemState || Boolean(error) || systemState.workerResponsive === false;
  const systemMode: SystemMode = workerUnavailable ? 'UNKNOWN' : systemState.executionMode;
  const pauseNewRiskActive = systemState?.pauseNewRisk || false;
  const killSwitchActive = systemState?.killSwitchActive || workerUnavailable;
  const engineState = workerUnavailable ? 'DEGRADED' : systemState.engineState;

  const describeActionError = useCallback((err: unknown): string => {
    if (err && typeof err === 'object' && 'status' in err) {
      const status = (err as { status?: number }).status;
      if (status === 401) {
        return 'Sign-in required: authenticate with a Google account that has operator permissions before changing system state.';
      }
      if (status === 403) {
        return 'Not authorized: your account does not have the required role for this action.';
      }
    }
    return err instanceof Error ? err.message : 'Action failed due to an unexpected error.';
  }, []);

  // Wraps system mutations so failures surface as a visible toast instead of
  // an unhandled rejection; returns whether the action succeeded.
  const runSafeAction = useCallback(async (action: () => Promise<unknown>, successText?: string): Promise<boolean> => {
    try {
      await action();
      if (successText) {
        setActionFeedback({ type: 'success', text: successText });
      }
      return true;
    } catch (err) {
      console.warn('System action failed:', err);
      setActionFeedback({ type: 'error', text: describeActionError(err) });
      return false;
    }
  }, [describeActionError]);

  const handleToggleKillSwitch = useCallback(() => {
    void runSafeAction(() => toggleKillSwitch());
  }, [runSafeAction, toggleKillSwitch]);

  const handleTogglePauseNewRisk = useCallback((active: boolean) => {
    void runSafeAction(
      () => togglePauseNewRisk(active),
      active ? 'New-risk pause enabled.' : 'New-risk pause lifted.'
    );
  }, [runSafeAction, togglePauseNewRisk]);

  return (
    <div className="flex h-screen w-screen overflow-hidden bg-zinc-950 text-zinc-100 font-sans selection:bg-cyan-500/30 selection:text-cyan-200">
      {/* 1. Left Persistent Trading OS Sidebar */}
      <Sidebar
        currentRoute={currentRoute}
        onNavigate={handleNavigate}
        collapsed={sidebarCollapsed}
        onToggleCollapse={() => setSidebarCollapsed(!sidebarCollapsed)}
        copilotOpen={copilotOpen}
        onToggleCopilot={() => setCopilotOpen(!copilotOpen)}
        riskState={account.risk_state}
        systemMode={systemMode}
      />

      {/* 2. Main Viewport (StatusBar + Routed Content) */}
      <div className="flex-1 flex flex-col min-w-0 h-full overflow-hidden">
        {/* Global Slim StatusBar */}
        <StatusBar
            engineState={engineState}
          riskState={account.risk_state}
          killSwitchActive={killSwitchActive}
          onToggleKillSwitch={handleToggleKillSwitch}
          systemMode={systemMode}
          lastUpdated={lastUpdated}
          onRefresh={refresh}
          isRefreshing={isLoading}
          binanceStatus={binanceStatus}
          onOpenBinanceModal={() => setShowBinanceModal(true)}
          openBasketsCount={baskets.length}
          pauseNewRiskActive={pauseNewRiskActive}
          onTogglePauseNewRisk={handleTogglePauseNewRisk}
          onOpenStartTradingWizard={() => setShowStartTradingWizard(true)}
        />

        {/* Emergency Kill Switch Persistent Warning Banner */}
        {account.kill_switch_active && (
          <div className="bg-rose-950 border-b border-rose-800 px-4 py-2 flex items-center justify-between text-xs font-semibold text-rose-200 shrink-0">
            <div className="flex items-center space-x-2">
              <Power className="w-4 h-4 text-rose-400 animate-pulse" />
              <span>EMERGENCY KILL SWITCH ENGAGED — Trading engines halted. All incoming orders vetoed.</span>
            </div>
            <button
              type="button"
              onClick={() => setShowDisarmConfirm(true)}
              className="px-2.5 py-1 bg-rose-800 hover:bg-rose-700 text-white rounded text-[11px] font-bold cursor-pointer"
            >
              DISARM KILL SWITCH
            </button>
          </div>
        )}

        {/* Scrollable Main Content Area */}
        <main className="flex-1 overflow-y-auto p-4 sm:p-6 lg:p-8">
          <div className="max-w-7xl mx-auto">
            <AppRoutes
              currentRoute={currentRoute}
              onNavigate={handleNavigate}
              systemState={systemState}
              account={account}
              onAccountUpdated={updateAccount}
              instruments={instruments}
              baskets={baskets}
              orders={orders}
              riskRules={riskRules}
              strategyIntents={strategyIntents}
              metaAllocations={metaAllocations}
              exposureRecovery={exposureRecovery}
              killSwitchActive={killSwitchActive}
              onToggleKillSwitch={handleToggleKillSwitch}
              correlationBtcEth={correlationBtcEth}
              cryptoBetaExposurePct={cryptoBetaExposurePct}
              liquidationDistancePct={liquidationDistancePct}
              isActionLoading={isActionLoading}
              onExpandGrid={expandGrid}
              onEnterRecovery={enterRecovery}
              onCloseBasket={closeBasket}
              onOpenBalanceModal={() => setShowBalanceModal(true)}
              binanceStatus={binanceStatus}
              onOpenBinanceModal={() => setShowBinanceModal(true)}
              onOpenGoogleCloudModal={() => setShowGoogleCloudModal(true)}
              onOpenWorkspaceModal={() => setShowWorkspaceModal(true)}
            />
          </div>
        </main>
      </div>

      {/* 3. Global AI Quant Copilot Drawer */}
      {/* 4. Global Alerts Drawer */}
      <AlertsDrawer
        isOpen={alertsOpen}
        onClose={() => setAlertsOpen(false)}
        alerts={alerts?.filter(a => !dismissedAlerts.has(a.id)) || []}
        onDismiss={(id) => setDismissedAlerts(prev => new Set(prev).add(id))}
        onClearAll={() => setDismissedAlerts(new Set(alerts?.map(a => a.id) || []))}
      />

      <CopilotDrawer
        isOpen={copilotOpen}
        onClose={() => setCopilotOpen(false)}
      />

      {/* 4. Global Modals (Preserving existing workflows) */}
      <BinanceProfileModal
        isOpen={showBinanceModal}
        onClose={() => setShowBinanceModal(false)}
        onStatusChanged={(status) => setBinanceStatus(status)}
      />

      <BalanceAllocationModal
        isOpen={showBalanceModal}
        onClose={() => setShowBalanceModal(false)}
        account={account}
        onAccountUpdated={updateAccount}
        onNavigateTab={(tab) => {
          if (tab === 'cockpit') handleNavigate('/command');
          else if (tab === 'wallet') handleNavigate('/portfolio');
          else if (tab === 'backtest') handleNavigate('/research/replay');
          else if (tab === 'bigquery') handleNavigate('/analytics');
          else if (tab === 'architecture') handleNavigate('/settings');
        }}
      />

      <GoogleCloudCenterModal
        isOpen={showGoogleCloudModal}
        onClose={() => setShowGoogleCloudModal(false)}
        firestoreConnected={firestoreConnected}
      />

      <GoogleWorkspaceModal
        isOpen={showWorkspaceModal}
        onClose={() => setShowWorkspaceModal(false)}
        baskets={baskets}
        portfolioState={account}
      />

      {showStartTradingWizard && (
        <StartTradingWizard
          onComplete={async (params) => {
            const armed = await runSafeAction(() => armEngine(params), 'Trading engine armed.');
            if (armed) {
              setShowStartTradingWizard(false);
            }
          }}
          onCancel={() => setShowStartTradingWizard(false)}
        />
      )}

      {/* Disarm Kill Switch Confirmation */}
      <ConfirmationModal
        isOpen={showDisarmConfirm}
        title="CONFIRM KILL SWITCH DISARM"
        message={
          <div className="space-y-3">
            <p>Disarming will resume normal operation:</p>
            <ul className="list-disc pl-4 space-y-1 text-zinc-400">
              <li>Restart strategy order generation loops</li>
              <li>Lift the veto on all incoming orders</li>
              <li>Allow grid expansion on {baskets.length} basket(s)</li>
            </ul>
            <p className="text-amber-300">Only disarm after verifying that positions and market state are safe.</p>
          </div>
        }
        confirmText="DISARM KILL SWITCH"
        isDestructive={false}
        onConfirm={() => {
          setShowDisarmConfirm(false);
          void runSafeAction(() => toggleKillSwitch());
        }}
        onCancel={() => setShowDisarmConfirm(false)}
      />

      {/* Global action feedback (never fail silently) */}
      <ActionToast feedback={actionFeedback} onDismiss={() => setActionFeedback(null)} />
    </div>
  );
};
