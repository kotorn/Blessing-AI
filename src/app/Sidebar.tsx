import React from 'react';
import {
  Activity,
  LineChart,
  Cpu,
  Layers,
  Boxes,
  ShieldCheck,
  PieChart,
  RotateCcw,
  Database,
  Network,
  Terminal,
  Sliders,
  Bot,
  ChevronLeft,
  ChevronRight,
  ShieldAlert,
  Zap,
} from 'lucide-react';
import { AppRoute, SystemMode } from '../contracts/system';
import { NAVIGATION_ITEMS, NavigationItem } from './navigation';
import { RiskState } from '../types';

interface SidebarProps {
  currentRoute: AppRoute;
  onNavigate: (route: AppRoute) => void;
  collapsed: boolean;
  onToggleCollapse: () => void;
  copilotOpen: boolean;
  onToggleCopilot: () => void;
  riskState: RiskState;
  systemMode: SystemMode;
}

const ICON_MAP: Record<string, React.ReactNode> = {
  Activity: <Activity className="w-4 h-4 shrink-0" />,
  LineChart: <LineChart className="w-4 h-4 shrink-0" />,
  Cpu: <Cpu className="w-4 h-4 shrink-0" />,
  Layers: <Layers className="w-4 h-4 shrink-0" />,
  Boxes: <Boxes className="w-4 h-4 shrink-0" />,
  ShieldCheck: <ShieldCheck className="w-4 h-4 shrink-0" />,
  PieChart: <PieChart className="w-4 h-4 shrink-0" />,
  RotateCcw: <RotateCcw className="w-4 h-4 shrink-0" />,
  Database: <Database className="w-4 h-4 shrink-0" />,
  Network: <Network className="w-4 h-4 shrink-0" />,
  Terminal: <Terminal className="w-4 h-4 shrink-0" />,
  Sliders: <Sliders className="w-4 h-4 shrink-0" />,
};

export const Sidebar: React.FC<SidebarProps> = ({
  currentRoute,
  onNavigate,
  collapsed,
  onToggleCollapse,
  copilotOpen,
  onToggleCopilot,
  riskState,
  systemMode,
}) => {
  const mainItems = NAVIGATION_ITEMS.filter((i) => i.category === 'MAIN');
  const researchItems = NAVIGATION_ITEMS.filter((i) => i.category === 'RESEARCH');
  const systemItems = NAVIGATION_ITEMS.filter((i) => i.category === 'SYSTEM');

  const renderNavGroup = (title: string, items: NavigationItem[]) => (
    <div className="space-y-1">
      {!collapsed && (
        <div className="px-3 pt-3 pb-1 text-[10px] font-bold uppercase tracking-wider text-zinc-500">
          {title}
        </div>
      )}
      {items.map((item) => {
        const isActive = currentRoute === item.route;
        const icon = ICON_MAP[item.icon] || <Activity className="w-4 h-4" />;

        return (
          <button
            key={item.route}
            type="button"
            onClick={() => onNavigate(item.route)}
            title={collapsed ? `${item.label}: ${item.description}` : item.description}
            className={`w-full flex items-center transition-colors rounded-lg text-xs font-medium cursor-pointer ${
              collapsed ? 'justify-center p-2.5' : 'px-3 py-2 space-x-2.5'
            } ${
              isActive
                ? 'bg-zinc-800/90 text-cyan-300 font-semibold shadow-sm border border-zinc-700/80'
                : 'text-zinc-400 hover:text-zinc-100 hover:bg-zinc-900/80 border border-transparent'
            }`}
          >
            <span className={isActive ? 'text-cyan-400' : 'text-zinc-400'}>{icon}</span>
            {!collapsed && (
              <div className="flex-1 flex items-center justify-between text-left truncate">
                <span className="truncate">{item.label}</span>
                {item.badge && (
                  <span
                    className={`ml-1.5 px-1.5 py-0.2 rounded text-[9px] font-bold ${
                      item.badgeVariant === 'cyan'
                        ? 'bg-cyan-950 text-cyan-300 border border-cyan-800/60'
                        : 'bg-zinc-800 text-zinc-400'
                    }`}
                  >
                    {item.badge}
                  </span>
                )}
              </div>
            )}
          </button>
        );
      })}
    </div>
  );

  return (
    <aside
      className={`bg-zinc-950 border-r border-zinc-800/90 flex flex-col shrink-0 transition-all duration-200 select-none ${
        collapsed ? 'w-16' : 'w-64'
      }`}
    >
      {/* Brand & App Title */}
      <div className="p-3.5 border-b border-zinc-800/90 flex items-center justify-between shrink-0">
        <div className="flex items-center space-x-2.5 overflow-hidden">
          <div className="w-8 h-8 rounded-lg bg-gradient-to-br from-indigo-500 to-cyan-500 flex items-center justify-center shrink-0 shadow-md shadow-indigo-500/20">
            <Zap className="w-4 h-4 text-white" />
          </div>
          {!collapsed && (
            <div className="truncate">
              <div className="flex items-center space-x-1.5">
                <span className="font-bold text-zinc-100 text-sm tracking-tight">Blessing AI</span>
                <span className="px-1.5 py-0.2 rounded text-[10px] font-mono font-semibold bg-zinc-800 text-zinc-400">
                  v0.2
                </span>
              </div>
              <p className="text-[10px] text-zinc-500 truncate">Trading Operating System</p>
            </div>
          )}
        </div>

        {/* Collapse toggle button */}
        <button
          type="button"
          onClick={onToggleCollapse}
          className="p-1 rounded text-zinc-400 hover:text-zinc-200 hover:bg-zinc-800 transition-colors cursor-pointer"
          title={collapsed ? 'Expand sidebar' : 'Collapse sidebar'}
        >
          {collapsed ? <ChevronRight className="w-4 h-4" /> : <ChevronLeft className="w-4 h-4" />}
        </button>
      </div>

      {/* Navigation Links Scrollable Area */}
      <div className="flex-1 overflow-y-auto px-2 py-2 space-y-3">
        {renderNavGroup('Trading Engines', mainItems)}
        {renderNavGroup('Research & Lakehouse', researchItems)}
        {renderNavGroup('System & Admin', systemItems)}
      </div>

      {/* Quant Copilot Drawer Trigger */}
      <div className="p-2 border-t border-zinc-800/80 shrink-0">
        <button
          type="button"
          onClick={onToggleCopilot}
          className={`w-full flex items-center rounded-lg text-xs font-semibold transition-all cursor-pointer ${
            collapsed ? 'justify-center p-2.5' : 'px-3 py-2 space-x-2.5'
          } ${
            copilotOpen
              ? 'bg-gradient-to-r from-indigo-950 to-cyan-950 text-cyan-300 border border-cyan-700/60 shadow-sm'
              : 'bg-zinc-900/90 text-zinc-300 hover:text-white hover:bg-zinc-800 border border-zinc-800'
          }`}
          title="Toggle Quant Copilot AI Drawer"
        >
          <Bot className="w-4 h-4 text-cyan-400 shrink-0" />
          {!collapsed && (
            <div className="flex-1 flex items-center justify-between text-left">
              <span>Quant Copilot</span>
              <span className="px-1.5 py-0.5 rounded text-[9px] font-bold bg-cyan-950/90 text-cyan-400 border border-cyan-800/50">
                GEMINI
              </span>
            </div>
          )}
        </button>
      </div>

      {/* Footer Status Pill */}
      {!collapsed && (
        <div className="px-3 py-2.5 border-t border-zinc-800/80 bg-zinc-900/40 text-[11px] flex items-center justify-between shrink-0">
          <div className="flex items-center space-x-1.5">
            <span
              className={`w-2 h-2 rounded-full ${
                riskState === 'NORMAL'
                  ? 'bg-emerald-400'
                  : riskState === 'CAUTION'
                  ? 'bg-amber-400'
                  : 'bg-rose-500 animate-pulse'
              }`}
            />
            <span className="font-mono text-zinc-400 text-[10px]">RISK: {riskState}</span>
          </div>
          <span className="font-mono text-zinc-500 text-[10px]">{systemMode}</span>
        </div>
      )}
    </aside>
  );
};
