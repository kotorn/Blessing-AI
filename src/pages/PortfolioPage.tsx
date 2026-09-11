import React from 'react';
import { BalanceAllocation } from '../components/BalanceAllocation';
import { AccountData } from '../types';
import { AppRoute } from '../contracts/system';

interface PortfolioPageProps {
  account: AccountData;
  onAccountUpdated: (account: AccountData) => void;
  onNavigate: (route: AppRoute) => void;
  onOpenKeyModal?: () => void;
}

export const PortfolioPage: React.FC<PortfolioPageProps> = ({
  account,
  onAccountUpdated,
  onNavigate,
  onOpenKeyModal,
}) => {
  return (
    <div className="space-y-6">
      <BalanceAllocation
        account={account}
        onAccountUpdated={onAccountUpdated}
        onNavigateTab={(tab) => {
          if (tab === 'cockpit') onNavigate('/command');
          else if (tab === 'wallet') onNavigate('/portfolio');
          else if (tab === 'backtest') onNavigate('/research/replay');
          else if (tab === 'bigquery') onNavigate('/analytics');
          else if (tab === 'architecture') onNavigate('/settings');
        }}
        onOpenKeyModal={onOpenKeyModal}
      />
    </div>
  );
};
