import React, { useState } from 'react';
import {
  ShieldAlert,
  ShieldCheck,
  Power,
  Key,
  RefreshCw,
  AlertTriangle,
  User as UserIcon,
  LogIn,
  LogOut,
  Bell,
  Cloud,
  CheckCircle2,
  X,
  PauseCircle,
} from 'lucide-react';
import { RiskState } from '../types';
import { SystemMode } from '../contracts/system';
import { useAuth } from '../context/AuthContext';
import { BinanceKeyStatus } from '../api/binance';
import { ConfirmationModal } from '../components/ConfirmationModal';

interface StatusBarProps {
  activeAlertCount?: number;
  onOpenAlerts?: () => void;
  riskState: RiskState;
  killSwitchActive: boolean;
  onToggleKillSwitch: () => void;
  systemMode: SystemMode;
  engineState?: string;
  lastUpdated: Date | null;
  onRefresh: () => void;
  isRefreshing: boolean;
  binanceStatus?: BinanceKeyStatus | null;
  onOpenBinanceModal: () => void;
  onNavigateToAlerts?: () => void;
  openBasketsCount?: number;
  pauseNewRiskActive?: boolean;
  onTogglePauseNewRisk?: () => void;
  onOpenStartTradingWizard?: () => void;
}

export const StatusBar: React.FC<StatusBarProps> = ({
  activeAlertCount = 0,
  onOpenAlerts,
  riskState,
  killSwitchActive,
  onToggleKillSwitch,
  systemMode,
  engineState,
  lastUpdated,
  onRefresh,
  isRefreshing,
  binanceStatus,
  onOpenBinanceModal,
  onNavigateToAlerts,
  openBasketsCount = 0,
  pauseNewRiskActive = false,
  onTogglePauseNewRisk,
  onOpenStartTradingWizard,
}) => {
  const { user, signIn, signOut, firestoreConnected } = useAuth();
  const [showKillSwitchConfirm, setShowKillSwitchConfirm] = useState(false);

  const isEmergency = riskState === 'EMERGENCY' || killSwitchActive;

  const handleConfirmKillSwitch = () => {
    onToggleKillSwitch();
    setShowKillSwitchConfirm(false);
  };

  const getModeBadgeClass = () => {
    switch (systemMode) {
      case 'LIVE':
        return 'bg-rose-950 text-rose-300 border-rose-700 font-black animate-pulse';
      case 'TESTNET':
        return 'bg-amber-950 text-amber-300 border-amber-800 font-bold';
      case 'PAPER':
        return 'bg-cyan-950 text-cyan-300 border-cyan-800 font-semibold';
      default:
        return 'bg-zinc-800 text-zinc-300 border-zinc-700 font-medium';
    }
  };

  const getRiskBadgeClass = () => {
    switch (riskState) {
      case 'NORMAL':
        return 'bg-emerald-950/80 text-emerald-400 border-emerald-800/80';
      case 'CAUTION':
        return 'bg-amber-950/80 text-amber-400 border-amber-800/80';
      case 'NO_NEW_GRID':
      case 'RECOVERY_ONLY':
        return 'bg-orange-950/80 text-orange-400 border-orange-800/80';
      case 'EMERGENCY':
      case 'DELEVERAGE':
        return 'bg-rose-950 text-rose-300 border-rose-800 animate-pulse';
      default:
        return 'bg-zinc-800 text-zinc-400 border-zinc-700';
    }
  };

  return (
    <>
      <header className="h-13 bg-zinc-950/95 border-b border-zinc-800/90 px-3 sm:px-4 flex items-center justify-between gap-2 shrink-0 z-30 select-none">
        {/* Left Section: System Mode, Binance Connection, Risk State */}
        <div className="flex items-center space-x-2 sm:space-x-3 overflow-hidden">
          {/* Mode Badge */}
          <div
            className={`px-2 py-0.5 rounded text-[10px] tracking-wider uppercase border ${getModeBadgeClass()}`}
            title={`Current Execution Mode: ${systemMode}`}
          >
            {systemMode}
          </div>
          {engineState && (
            <div className={`px-2 py-0.5 rounded text-[10px] tracking-wider font-bold uppercase border ${engineState === "ARMED" ? "bg-emerald-950 text-emerald-400 border-emerald-800" : engineState === "DISARMED" ? "bg-zinc-800 text-zinc-400 border-zinc-700" : engineState === "PAUSED_NEW_RISK" ? "bg-amber-950 text-amber-400 border-amber-800" : "bg-rose-950 text-rose-400 border-rose-800"}`}>
              {engineState}
            </div>
          )}
          {/* Binance Status Badge */}
          <button
            type="button"
            onClick={onOpenBinanceModal}
            className={`flex items-center space-x-1.5 px-2.5 py-1 rounded text-xs border transition-colors cursor-pointer ${
              binanceStatus?.spot?.authenticated
                ? 'bg-amber-950/40 text-amber-300 border-amber-800/50 hover:bg-amber-900/30'
                : 'bg-zinc-900 text-zinc-400 border-zinc-800 hover:bg-zinc-800'
            }`}
            title="Binance Global API Key Diagnostics & Profiles"
          >
            <Key className="w-3.5 h-3.5" />
            <span className="hidden sm:inline font-mono font-medium text-[11px]">
              {binanceStatus?.spot?.authenticated ? 'Binance Probe Only' : 'Binance Setup'}
            </span>
            {binanceStatus?.spot?.authenticated && (
              <span className="w-1.5 h-1.5 rounded-full bg-amber-400" />
            )}
          </button>

          {/* Risk State Pill */}
          <div
            className={`hidden md:flex items-center space-x-1 px-2 py-0.5 rounded text-[10px] font-bold border ${getRiskBadgeClass()}`}
          >
            {riskState === 'NORMAL' ? (
              <ShieldCheck className="w-3 h-3" />
            ) : (
              <ShieldAlert className="w-3 h-3" />
            )}
            <span>{riskState}</span>
          </div>

          {/* Market Freshness / Sync indicator */}
          <div className="hidden lg:flex items-center space-x-1.5 text-[11px] text-zinc-500 font-mono pl-1">
            <button
              type="button"
              onClick={onRefresh}
              className="p-1 text-zinc-400 hover:text-zinc-200 transition-colors cursor-pointer"
              title="Manual refresh"
            >
              <RefreshCw className={`w-3 h-3 ${isRefreshing ? 'animate-spin text-cyan-400' : ''}`} />
            </button>
            {lastUpdated && (
              <span title={lastUpdated.toISOString()}>
                {lastUpdated.toLocaleTimeString()}
              </span>
            )}
          </div>
        </div>

        {/* Right Section: Safety Controls (Pause New Risk, Kill Switch), Alerts, User Auth */}
        <div className="flex items-center space-x-2">
          {/* Level 1 Safety: Pause New Risk */}
          {onTogglePauseNewRisk && (
            <button
              type="button"
              onClick={onTogglePauseNewRisk}
              className={`hidden sm:flex items-center space-x-1 px-2.5 py-1 rounded text-xs border font-medium transition-colors cursor-pointer ${
                pauseNewRiskActive
                  ? 'bg-amber-950 text-amber-300 border-amber-700'
                  : 'bg-zinc-900 text-zinc-400 hover:text-amber-300 border-zinc-800 hover:border-zinc-700'
              }`}
              title="Pause opening new risk positions while managing existing baskets"
            >
              <PauseCircle className="w-3.5 h-3.5 text-amber-400" />
              <span className="text-[11px]">
                {pauseNewRiskActive ? 'New Risk Paused' : 'Pause Risk'}
              </span>
            </button>
          )}

          {/* Start Trading Wizard Trigger */}
          {onOpenStartTradingWizard && (
            <button
              type="button"
              onClick={onOpenStartTradingWizard}
              className="flex items-center space-x-1.5 px-3 py-1 rounded text-xs font-bold bg-indigo-600 hover:bg-indigo-500 text-white transition-all cursor-pointer shadow-lg shadow-indigo-900/20"
              title="Launch Start Trading Wizard"
            >
              <Power className="w-3.5 h-3.5" />
              <span className="font-mono text-[11px]">START TRADING</span>
            </button>
          )}

          {/* Level 3 Safety: Kill Switch */}
          <button
            type="button"
            onClick={() => {
              if (!killSwitchActive) {
                setShowKillSwitchConfirm(true);
              } else {
                onToggleKillSwitch();
              }
            }}
            className={`flex items-center space-x-1.5 px-3 py-1 rounded text-xs font-bold transition-all cursor-pointer ${
              killSwitchActive
                ? 'bg-rose-600 text-white shadow-lg shadow-rose-600/40 animate-pulse'
                : 'bg-zinc-900 text-zinc-400 hover:text-rose-400 hover:bg-rose-950/40 border border-zinc-800 hover:border-rose-900'
            }`}
            title="Emergency Kill Switch: Halt trading and cancel all active orders"
          >
            <Power className="w-3.5 h-3.5" />
            <span className="font-mono text-[11px]">
              {killSwitchActive ? 'KILL ENGAGED' : 'KILL SWITCH'}
            </span>
          </button>

                    {/* Alerts Bell */}
          {onOpenAlerts && (
            <button
              type="button"
              onClick={onOpenAlerts}
              className="relative p-1.5 text-zinc-400 hover:text-zinc-200 transition-colors cursor-pointer"
              title="View System Alerts"
            >
              <Bell className="w-4 h-4" />
              {activeAlertCount > 0 && (
                <span className="absolute top-0 right-0 w-2 h-2 rounded-full bg-rose-500 animate-pulse border border-zinc-900" />
              )}
            </button>
          )}

          {/* Firestore Cloud Sync Badge */}
          <div
            className={`hidden xl:flex items-center space-x-1 px-2 py-0.5 rounded text-[10px] border ${
              firestoreConnected
                ? 'bg-cyan-950/40 text-cyan-400 border-cyan-800/40'
                : 'bg-zinc-900 text-zinc-500 border-zinc-800'
            }`}
            title="Google Cloud Firestore & Audit Persistence"
          >
            <Cloud className="w-3 h-3" />
            <span className="font-mono">{firestoreConnected ? 'Cloud Sync' : 'Offline'}</span>
          </div>

          {/* User Auth */}
          {user ? (
            <div className="flex items-center space-x-1.5 pl-1.5 border-l border-zinc-800">
              {user.photoURL ? (
                <img
                  src={user.photoURL}
                  alt={user.displayName || 'User'}
                  className="w-6 h-6 rounded-full border border-zinc-700"
                  referrerPolicy="no-referrer"
                />
              ) : (
                <div className="w-6 h-6 rounded-full bg-indigo-900 flex items-center justify-center text-xs font-bold text-white">
                  {user.displayName?.[0] || 'U'}
                </div>
              )}
              <button
                type="button"
                onClick={signOut}
                className="p-1 text-zinc-400 hover:text-rose-400 transition-colors cursor-pointer"
                title={`Signed in as ${user.email}. Click to Sign Out`}
              >
                <LogOut className="w-3.5 h-3.5" />
              </button>
            </div>
          ) : (
            <button
              type="button"
              onClick={signIn}
              className="flex items-center space-x-1 px-2.5 py-1 rounded text-xs font-semibold bg-indigo-600 hover:bg-indigo-500 text-white transition-colors cursor-pointer"
              title="Sign in with Google Account"
            >
              <LogIn className="w-3 h-3" />
              <span className="text-[11px]">Sign In</span>
            </button>
          )}
        </div>
      </header>

      {/* Kill Switch Safety Confirmation Modal */}
      <ConfirmationModal
        isOpen={showKillSwitchConfirm}
        title="CONFIRM EMERGENCY KILL SWITCH"
        message={
          <div className="space-y-3">
            <p>Triggering the Kill Switch will immediately:</p>
            <ul className="list-disc pl-4 space-y-1 text-zinc-400">
              <li>Halt all active strategy order generation loops</li>
              <li>Request cancellation of all open limit and grid orders on Binance</li>
              <li>Transition portfolio risk state to <strong className="text-rose-300">EMERGENCY</strong></li>
              <li>{openBasketsCount} active basket(s) will be locked from grid expansion</li>
            </ul>
          </div>
        }
        confirmText="ENGAGE KILL SWITCH"
        isDestructive={true}
        requireTypedConfirmation={systemMode === 'LIVE' ? 'KILL' : undefined}
        onConfirm={handleConfirmKillSwitch}
        onCancel={() => setShowKillSwitchConfirm(false)}
      />
    </>
  );
};
