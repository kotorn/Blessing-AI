import React from 'react';
import { ShieldAlert, ShieldCheck, Activity, Terminal, Power } from 'lucide-react';
import { RiskState } from '../types';

interface HeaderProps {
  riskState: RiskState;
  killSwitchActive: boolean;
  onToggleKillSwitch: () => void;
  activeTab: 'cockpit' | 'backtest' | 'copilot' | 'architecture';
  setActiveTab: (tab: 'cockpit' | 'backtest' | 'copilot' | 'architecture') => void;
}

export const Header: React.FC<HeaderProps> = ({
  riskState,
  killSwitchActive,
  onToggleKillSwitch,
  activeTab,
  setActiveTab,
}) => {
  const getBadgeColor = (state: RiskState) => {
    switch (state) {
      case 'NORMAL':
        return 'bg-emerald-500/10 text-emerald-400 border-emerald-500/30';
      case 'CAUTION':
        return 'bg-amber-500/10 text-amber-400 border-amber-500/30';
      case 'NO_NEW_GRID':
      case 'RECOVERY_ONLY':
        return 'bg-orange-500/10 text-orange-400 border-orange-500/30';
      case 'DELEVERAGE':
      case 'EMERGENCY':
        return 'bg-rose-500/10 text-rose-400 border-rose-500/30';
    }
  };

  return (
    <header className="border-b border-zinc-800 bg-zinc-950/80 backdrop-blur sticky top-0 z-50">
      <div className="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 h-16 flex items-center justify-between">
        {/* Logo & System Brand */}
        <div className="flex items-center space-x-3">
          <div className="w-9 h-9 rounded-lg bg-gradient-to-br from-indigo-500 to-cyan-500 flex items-center justify-center shadow-lg shadow-indigo-500/20">
            <Activity className="w-5 h-5 text-white" />
          </div>
          <div>
            <div className="flex items-center space-x-2">
              <span className="font-bold text-zinc-100 tracking-tight text-lg">Blessing AI</span>
              <span className="text-xs px-2 py-0.5 rounded bg-zinc-800 text-zinc-400 font-mono">v0.1-rc</span>
            </div>
            <p className="text-xs text-zinc-400 hidden sm:block">Adaptive Basket Grid & Risk Governor • Binance USDⓈ-M</p>
          </div>
        </div>

        {/* Navigation Tabs */}
        <nav className="flex items-center space-x-1 bg-zinc-900/90 p-1 rounded-lg border border-zinc-800">
          <button
            onClick={() => setActiveTab('cockpit')}
            className={`px-3 py-1.5 rounded-md text-xs font-medium transition-all ${
              activeTab === 'cockpit'
                ? 'bg-zinc-800 text-white shadow-sm'
                : 'text-zinc-400 hover:text-zinc-200 hover:bg-zinc-800/50'
            }`}
          >
            Live Cockpit
          </button>
          <button
            onClick={() => setActiveTab('backtest')}
            className={`px-3 py-1.5 rounded-md text-xs font-medium transition-all ${
              activeTab === 'backtest'
                ? 'bg-zinc-800 text-white shadow-sm'
                : 'text-zinc-400 hover:text-zinc-200 hover:bg-zinc-800/50'
            }`}
          >
            Stress Replay
          </button>
          <button
            onClick={() => setActiveTab('copilot')}
            className={`px-3 py-1.5 rounded-md text-xs font-medium transition-all ${
              activeTab === 'copilot'
                ? 'bg-zinc-800 text-white shadow-sm'
                : 'text-zinc-400 hover:text-zinc-200 hover:bg-zinc-800/50'
            }`}
          >
            Quant Copilot
          </button>
          <button
            onClick={() => setActiveTab('architecture')}
            className={`px-3 py-1.5 rounded-md text-xs font-medium transition-all ${
              activeTab === 'architecture'
                ? 'bg-zinc-800 text-white shadow-sm'
                : 'text-zinc-400 hover:text-zinc-200 hover:bg-zinc-800/50'
            }`}
          >
            Spec & Architecture
          </button>
        </nav>

        {/* Right Status Badges & Kill Switch */}
        <div className="flex items-center space-x-3">
          <div className={`flex items-center space-x-1.5 px-2.5 py-1 rounded-full border text-xs font-semibold ${getBadgeColor(riskState)}`}>
            {riskState === 'NORMAL' ? (
              <ShieldCheck className="w-3.5 h-3.5" />
            ) : (
              <ShieldAlert className="w-3.5 h-3.5 animate-pulse" />
            )}
            <span>{riskState}</span>
          </div>

          <button
            onClick={onToggleKillSwitch}
            className={`flex items-center space-x-1.5 px-3 py-1.5 rounded-md text-xs font-semibold transition-all border ${
              killSwitchActive
                ? 'bg-rose-600 text-white border-rose-500 shadow-lg shadow-rose-600/30'
                : 'bg-zinc-900 text-zinc-300 border-zinc-700 hover:border-rose-500 hover:text-rose-400'
            }`}
            title="Immediate emergency kill switch: cancel all orders and close positions"
          >
            <Power className="w-3.5 h-3.5" />
            <span className="hidden md:inline">{killSwitchActive ? 'EMERGENCY FLATTENED' : 'KILL SWITCH'}</span>
          </button>
        </div>
      </div>
    </header>
  );
};
