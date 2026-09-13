import React from 'react';
import { AppRoute } from '../contracts/system';
import { CommandCenterPage } from '../pages/CommandCenterPage';
import { MarketsPage } from '../pages/MarketsPage';
import { StrategiesPage } from '../pages/StrategiesPage';
import { OrdersExecutionPage } from '../pages/OrdersExecutionPage';
import { PositionsBasketsPage } from '../pages/PositionsBasketsPage';
import { RiskRecoveryPage } from '../pages/RiskRecoveryPage';
import { PortfolioPage } from '../pages/PortfolioPage';
import { ReplayPage } from '../pages/ReplayPage';
import { AnalyticsPage } from '../pages/AnalyticsPage';
import { ConnectionsPage } from '../pages/ConnectionsPage';
import { AuditLogPage } from '../pages/AuditLogPage';
import { SettingsPage } from '../pages/SettingsPage';
import { AccountData, BasketItem, InstrumentData, RiskRuleItem, TradingSystemState } from '../types';
import { BinanceKeyStatus } from '../api/binance';

import { ExecutionOrder } from '../types/orders';

interface AppRoutesProps {
  currentRoute: AppRoute;
  onNavigate: (route: AppRoute) => void;
  systemState: TradingSystemState | null;
  account: AccountData;
  onAccountUpdated: (account: AccountData) => void;
  instruments: Record<string, InstrumentData>;
  baskets: BasketItem[];
  orders: ExecutionOrder[];
  riskRules: RiskRuleItem[];
  strategyIntents?: any[];
  metaAllocations?: Record<string, any> | null;
  exposureRecovery?: any | null;
  correlationBtcEth: number | null;
  cryptoBetaExposurePct: number | null;
  killSwitchActive?: boolean;
  onToggleKillSwitch: () => void;
  liquidationDistancePct: number | null;
  isActionLoading: boolean;
  onExpandGrid: (basketId: string) => Promise<void>;
  onEnterRecovery: (basketId: string) => Promise<void>;
  onCloseBasket: (basketId: string) => Promise<void>;
  onOpenBalanceModal: () => void;
  binanceStatus?: BinanceKeyStatus | null;
  onOpenBinanceModal: () => void;
  onOpenGoogleCloudModal: () => void;
  onOpenWorkspaceModal: () => void;
}

export const AppRoutes: React.FC<AppRoutesProps> = ({
  currentRoute,
  onNavigate,
  systemState,
  account,
  onAccountUpdated,
  instruments,
  baskets,
  orders,
  riskRules,
  strategyIntents,
  metaAllocations,
  exposureRecovery,
  correlationBtcEth,
  cryptoBetaExposurePct,
  killSwitchActive = false,
  onToggleKillSwitch,
  liquidationDistancePct,
  isActionLoading,
  onExpandGrid,
  onEnterRecovery,
  onCloseBasket,
  onOpenBalanceModal,
  binanceStatus,
  onOpenBinanceModal,
  onOpenGoogleCloudModal,
  onOpenWorkspaceModal,
}) => {
  switch (currentRoute) {
    case '/command':
      return (
        <CommandCenterPage
          account={account}
          systemState={systemState}
          onAccountUpdated={onAccountUpdated}
          instruments={instruments}
          baskets={baskets}
          riskRules={riskRules}
          correlationBtcEth={correlationBtcEth}
          cryptoBetaExposurePct={cryptoBetaExposurePct}
          liquidationDistancePct={liquidationDistancePct}
          isActionLoading={isActionLoading}
          onExpandGrid={onExpandGrid}
          onEnterRecovery={onEnterRecovery}
          onCloseBasket={onCloseBasket}
          onOpenBalanceModal={onOpenBalanceModal}
          onNavigate={onNavigate}
        />
      );

    case '/markets':
      return <MarketsPage instruments={instruments} />;

    case '/strategies':
      return (
        <StrategiesPage
          strategyIntents={strategyIntents || ((window as any).quantState as any)?.strategy_intents}
          metaAllocations={metaAllocations || ((window as any).quantState as any)?.meta_allocations}
          baskets={baskets}
          instruments={instruments}
          account={account}
        />
      );

    case '/orders':
      return (
        <OrdersExecutionPage
          account={account}
          orders={orders}
          systemState={systemState}
        />
      );

    case '/positions':
      return (
        <PositionsBasketsPage
          baskets={baskets}
          onExpandGrid={onExpandGrid}
          onEnterRecovery={onEnterRecovery}
          onCloseBasket={onCloseBasket}
          isActionLoading={isActionLoading}
        />
      );

    case '/risk':
      return (
        <RiskRecoveryPage
          account={account}
          riskState={account.risk_state}
          rules={riskRules}
          correlationBtcEth={correlationBtcEth}
          cryptoBetaExposurePct={cryptoBetaExposurePct}
          liquidationDistancePct={liquidationDistancePct}
          baskets={baskets}
          recoveryData={exposureRecovery || ((window as any).quantState as any)?.exposure_recovery}
          killSwitchActive={killSwitchActive}
          onToggleKillSwitch={onToggleKillSwitch}
        />
      );

    case '/portfolio':
      return (
        <PortfolioPage
          account={account}
          onAccountUpdated={onAccountUpdated}
          onNavigate={onNavigate}
          onOpenKeyModal={onOpenBinanceModal}
        />
      );

    case '/research/replay':
      return <ReplayPage />;

    case '/analytics':
      return <AnalyticsPage />;

    case '/system/connections':
      return (
        <ConnectionsPage
          binanceStatus={binanceStatus}
          onOpenBinanceModal={onOpenBinanceModal}
          onOpenGoogleCloudModal={onOpenGoogleCloudModal}
          onOpenWorkspaceModal={onOpenWorkspaceModal}
        />
      );

    case '/system/audit':
      return <AuditLogPage />;

    case '/settings':
      return <SettingsPage />;

    default:
      return (
        <CommandCenterPage
          account={account}
          systemState={systemState}
          onAccountUpdated={onAccountUpdated}
          instruments={instruments}
          baskets={baskets}
          riskRules={riskRules}
          correlationBtcEth={correlationBtcEth}
          cryptoBetaExposurePct={cryptoBetaExposurePct}
          liquidationDistancePct={liquidationDistancePct}
          isActionLoading={isActionLoading}
          onExpandGrid={onExpandGrid}
          onEnterRecovery={onEnterRecovery}
          onCloseBasket={onCloseBasket}
          onOpenBalanceModal={onOpenBalanceModal}
          onNavigate={onNavigate}
        />
      );
  }
};
