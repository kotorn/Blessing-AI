import React from 'react';
import { BasketManager } from '../components/BasketManager';
import { BasketItem } from '../types';
import { Boxes } from 'lucide-react';

interface PositionsBasketsPageProps {
  baskets: BasketItem[];
  onExpandGrid: (basketId: string) => Promise<void>;
  onEnterRecovery: (basketId: string) => Promise<void>;
  onCloseBasket: (basketId: string) => Promise<void>;
  isActionLoading: boolean;
}

export const PositionsBasketsPage: React.FC<PositionsBasketsPageProps> = ({
  baskets,
  onExpandGrid,
  onEnterRecovery,
  onCloseBasket,
  isActionLoading,
}) => {
  return (
    <div className="space-y-6">
      <div>
        <h2 className="text-xl font-bold text-zinc-100 flex items-center space-x-2">
          <Boxes className="w-5 h-5 text-cyan-400" />
          <span>Positions & Active Geometric Baskets</span>
        </h2>
        <p className="text-xs text-zinc-400 mt-1">
          Active multi-order grids, volume-weighted average entry prices, and dynamic counter-trend hedges.
        </p>
      </div>

      <BasketManager
        baskets={baskets}
        onExpandGrid={onExpandGrid}
        onEnterRecovery={onEnterRecovery}
        onCloseBasket={onCloseBasket}
        isActionLoading={isActionLoading}
      />
    </div>
  );
};
