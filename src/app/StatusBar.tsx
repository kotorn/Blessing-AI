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

interface StatusBarProps {
  riskState: RiskState;
  killSwitchActive: boolean;
  onToggleKillSwitch: () => void;
  systemMode: SystemMode;
  lastUpdated: Date | null;
  onRefresh: () => void;
  isRefreshing: boolean;
  binanceStatus?: BinanceKeyStatus | null;
  onOpenBinanceModal: () => void;
  onNavigateToAlerts?: () => void;
  openBasketsCount?: number;
  pauseNewRiskActive?: boolean;
  onTogglePauseNewRisk?: () => void;
}

export const StatusBar: React.FC<StatusBarProps> = ({
  riskState,
  killSwitchActive,
  onToggleKillSwitch,
  systemMode,
  lastUpdated,
  onRefresh,
  isRefreshing,
  binanceStatus,
  onOpenBinanceModal,
  onNavigateToAlerts,
  openBasketsCount = 0,
  pauseNewRiskActive = false,
  onTogglePauseNewRisk,
}) => {
  const { user, signIn, signOut, firestoreConnected } = useAuth();
  const [showKillSwitchConfirm, setShowKillSwitchConfirm] = useState(false);
  const [typedConfirm, setTypedConfirm] = useState('');

  const isEmergency = riskState === 'EMERGENCY' || killSwitchActive;

  const handleConfirmKillSwitch = () => {
    onToggleKillSwitch();
    setShowKillSwitchConfirm(false);
    setTypedConfirm('');
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

          {/* Binance Status Badge */}
          <button
            type="button"
            onClick={onOpenBinanceModal}
            className={`flex items-center space-x-1.5 px-2.5 py-1 rounded text-xs border transition-colors cursor-pointer ${
              binanceStatus?.spot?.authenticated
                ? binanceStatus.futures?.authenticated
                  ? 'bg-emerald-950/40 text-emerald-400 border-emerald-800/50 hover:bg-emerald-900/30'
                  : 'bg-amber-950/40 text-amber-400 border-amber-800/50 hover:bg-amber-900/30'
                : 'bg-zinc-900 text-zinc-400 border-zinc-800 hover:bg-zinc-800'
            }`}
            title="Binance Global API Key Diagnostics & Profiles"
          >
            <Key className="w-3.5 h-3.5" />
            <span className="hidden sm:inline font-mono font-medium text-[11px]">
              {binanceStatus?.spot?.authenticated ? 'Binance Connected' : 'Binance Setup'}
            </span>
            {binanceStatus?.spot?.authenticated && (
              <span className="w-1.5 h-1.5 rounded-full bg-emerald-400" />
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
      {showKillSwitchConfirm && (
        <div className="fixed inset-0 z-50 flex items-center justify-center p-4 bg-black/80 backdrop-blur-sm animate-fadeIn">
          <div className="bg-zinc-950 border border-rose-800/80 rounded-2xl max-w-md w-full p-5 shadow-2xl space-y-4">
            <div className="flex items-center space-x-3 text-rose-400">
              <div className="p-2 rounded-xl bg-rose-950 border border-rose-800/80">
                <AlertTriangle className="w-6 h-6" />
              </div>
              <div>
                <h3 className="text-sm font-bold text-zinc-100">CONFIRM EMERGENCY KILL SWITCH</h3>
                <p className="text-xs text-rose-400">Level 3 Critical Safety Action</p>
              </div>
            </div>

            <div className="text-xs text-zinc-300 space-y-2 bg-rose-950/20 border border-rose-900/40 p-3 rounded-lg">
              <p>Triggering the Kill Switch will immediately:</p>
              <ul className="list-disc pl-4 space-y-1 text-zinc-400">
                <li>Halt all active strategy order generation loops</li>
                <li>Request cancellation of all open limit and grid orders on Binance</li>
                <li>Transition portfolio risk state to <strong className="text-rose-300">EMERGENCY</strong></li>
                <li>{openBasketsCount} active basket(s) will be locked from grid expansion</li>
              </ul>
            </div>

            {systemMode === 'LIVE' && (
              <div className="space-y-1">
                <label className="text-[11px] text-zinc-400">
                  Type <strong className="text-rose-400 font-mono">KILL</strong> to confirm:
                </label>
                <input
                  type="text"
                  value={typedConfirm}
                  onChange={(e) => setTypedConfirm(e.target.value)}
                  placeholder="KILL"
                  className="w-full px-3 py-1.5 bg-zinc-900 border border-zinc-700 rounded text-xs font-mono text-zinc-100 uppercase focus:border-rose-500 focus:outline-none"
                />
              </div>
            )}

            <div className="flex items-center justify-end space-x-2 pt-2 border-t border-zinc-800">
              <button
                type="button"
                onClick={() => {
                  setShowKillSwitchConfirm(false);
                  setTypedConfirm('');
                }}
                className="px-3 py-1.5 rounded-lg text-xs font-medium bg-zinc-800 hover:bg-zinc-700 text-zinc-300 cursor-pointer"
              >
                Cancel
              </button>
              <button
                type="button"
                onClick={handleConfirmKillSwitch}
                disabled={systemMode === 'LIVE' && typedConfirm !== 'KILL'}
                className="px-4 py-1.5 rounded-lg text-xs font-bold bg-rose-600 hover:bg-rose-500 text-white disabled:opacity-50 transition-colors cursor-pointer"
              >
                ENGAGE KILL SWITCH
              </button>
            </div>
          </div>
        </div>
      )}
    </>
  );
};
