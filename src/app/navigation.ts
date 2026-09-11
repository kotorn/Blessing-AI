import { AppRoute } from '../contracts/system';

export interface NavigationItem {
  route: AppRoute;
  label: string;
  shortLabel?: string;
  icon: string;
  badge?: string;
  badgeVariant?: 'default' | 'cyan' | 'amber' | 'emerald';
  category: 'MAIN' | 'RESEARCH' | 'SYSTEM';
  description: string;
}

export const NAVIGATION_ITEMS: NavigationItem[] = [
  // MAIN TRADING OS
  {
    route: '/command',
    label: 'Command Center',
    shortLabel: 'Cockpit',
    icon: 'Activity',
    category: 'MAIN',
    description: 'Real-time operational dashboard, live instruments, active baskets, and risk checks',
  },
  {
    route: '/markets',
    label: 'Markets',
    icon: 'LineChart',
    category: 'MAIN',
    description: 'Cross-instrument price action, regimes, shock percentiles, and market structure',
  },
  {
    route: '/strategies',
    label: 'Strategies',
    icon: 'Cpu',
    badge: 'Alpha',
    badgeVariant: 'cyan',
    category: 'MAIN',
    description: 'Independent alpha engines: Grid, Trend, Shock, and Basis Carry',
  },
  {
    route: '/orders',
    label: 'Orders & Execution',
    shortLabel: 'Execution',
    icon: 'Layers',
    category: 'MAIN',
    description: 'Decision pipeline traceability, orders table, latency, and fills',
  },
  {
    route: '/positions',
    label: 'Positions & Baskets',
    shortLabel: 'Positions',
    icon: 'Boxes',
    category: 'MAIN',
    description: 'Active geometric baskets, physical exposure, and net target deltas',
  },
  {
    route: '/risk',
    label: 'Risk & Recovery',
    shortLabel: 'Risk',
    icon: 'ShieldCheck',
    category: 'MAIN',
    description: 'Portfolio Risk Governor, recovery center, liquidation distance, and limits',
  },
  {
    route: '/portfolio',
    label: 'Portfolio & Wallets',
    shortLabel: 'Portfolio',
    icon: 'PieChart',
    category: 'MAIN',
    description: 'Two-layer Binance asset allocation (Trading Bot, Portfolio Margin, Earn, Spot)',
  },

  // RESEARCH & HISTORICAL
  {
    route: '/research/replay',
    label: 'Replay & Backtest',
    shortLabel: 'Replay',
    icon: 'RotateCcw',
    category: 'RESEARCH',
    description: 'Stress replay studio, walk-forward validation, and historical execution simulator',
  },
  {
    route: '/analytics',
    label: 'Analytics Workspace',
    shortLabel: 'Analytics',
    icon: 'Database',
    badge: 'Lakehouse',
    badgeVariant: 'cyan',
    category: 'RESEARCH',
    description: 'Google BigQuery partitioned analytical lakehouse & regime attribution',
  },

  // SYSTEM & INFRASTRUCTURE
  {
    route: '/system/connections',
    label: 'Connections',
    icon: 'Network',
    category: 'SYSTEM',
    description: 'Binance API profile management, Google Cloud platform center, and Workspace integrations',
  },
  {
    route: '/system/audit',
    label: 'Audit & Decision Log',
    shortLabel: 'Audit Log',
    icon: 'Terminal',
    category: 'SYSTEM',
    description: 'Traceable immutable decision logs, risk overrides, and operator triggers',
  },
  {
    route: '/settings',
    label: 'Spec & Settings',
    shortLabel: 'Settings',
    icon: 'Sliders',
    category: 'SYSTEM',
    description: 'Blessing AI architecture review viewer, operational parameters, and configuration',
  },
];
