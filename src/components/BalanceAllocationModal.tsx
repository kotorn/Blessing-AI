import React from 'react';
import { X, Maximize2, Minimize2, PieChart, Layers } from 'lucide-react';
import { BalanceAllocation } from './BalanceAllocation';
import { AccountData } from '../types';

interface BalanceAllocationModalProps {
  isOpen: boolean;
  onClose: () => void;
  account: AccountData;
  onAccountUpdated?: (newAccount: AccountData) => void;
  onNavigateTab?: (tab: 'cockpit' | 'wallet' | 'backtest' | 'copilot' | 'bigquery' | 'architecture') => void;
  onOpenKeyModal?: () => void;
}

export const BalanceAllocationModal: React.FC<BalanceAllocationModalProps> = ({
  isOpen,
  onClose,
  account,
  onAccountUpdated,
  onNavigateTab,
  onOpenKeyModal,
}) => {
  const [isFullScreen, setIsFullScreen] = React.useState(false);

  if (!isOpen) return null;

  const handleSwitchToFullTab = () => {
    onClose();
    if (onNavigateTab) {
      onNavigateTab('wallet');
    }
  };

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center p-3 sm:p-6 bg-black/80 backdrop-blur-sm overflow-y-auto animate-fadeIn">
      <div
        className={`bg-zinc-950 border border-zinc-800 rounded-2xl shadow-2xl flex flex-col transition-all duration-200 overflow-hidden ${
          isFullScreen
            ? 'w-full h-full max-w-none max-h-none rounded-none'
            : 'w-full max-w-6xl max-h-[92vh]'
        }`}
      >
        {/* Modal Top Bar */}
        <div className="px-5 py-3.5 border-b border-zinc-800 bg-zinc-900/90 flex items-center justify-between shrink-0">
          <div className="flex items-center space-x-2.5">
            <div className="p-1.5 rounded-lg bg-cyan-950/80 border border-cyan-800/60 text-cyan-400">
              <PieChart className="w-4 h-4" />
            </div>
            <div>
              <div className="flex items-center space-x-2">
                <span className="text-sm font-bold text-zinc-100">
                  หน้าต่างสรุปกระเป๋าและการจัดสรรเงิน (Balance Allocation Popup)
                </span>
                <span className="px-2 py-0.5 rounded text-[10px] font-bold bg-cyan-950 text-cyan-300 border border-cyan-800/60">
                  POP-UP MODAL
                </span>
              </div>
              <span className="text-[11px] text-zinc-400">
                Binance Sub-Wallets: Trading Bot, Portfolio Margin, Simple Earn & Spot
              </span>
            </div>
          </div>

          <div className="flex items-center space-x-2">
            {/* Open as dedicated page/tab button */}
            <button
              onClick={handleSwitchToFullTab}
              className="flex items-center space-x-1.5 px-3 py-1.5 rounded-lg text-xs font-medium bg-zinc-800 hover:bg-zinc-700 text-cyan-300 border border-zinc-700 transition-colors cursor-pointer"
              title="สลับไปยังแท็บหน้ากระเป๋าเต็มหน้าจอ (Dedicated Tab View)"
            >
              <Layers className="w-3.5 h-3.5" />
              <span className="hidden sm:inline">เปิดแท็บเต็มหน้า</span>
            </button>

            {/* Maximize / Restore */}
            <button
              onClick={() => setIsFullScreen(!isFullScreen)}
              className="p-1.5 rounded-lg bg-zinc-800 hover:bg-zinc-700 text-zinc-400 hover:text-zinc-100 border border-zinc-700 transition-colors cursor-pointer"
              title={isFullScreen ? 'ย่อหน้าต่าง' : 'ขยายเต็มจอ'}
            >
              {isFullScreen ? <Minimize2 className="w-4 h-4" /> : <Maximize2 className="w-4 h-4" />}
            </button>

            {/* Close Button */}
            <button
              onClick={onClose}
              className="p-1.5 rounded-lg bg-zinc-800 hover:bg-rose-950/80 text-zinc-400 hover:text-rose-300 border border-zinc-700 hover:border-rose-800 transition-colors cursor-pointer"
              title="ปิดหน้าต่าง (Close)"
            >
              <X className="w-4 h-4" />
            </button>
          </div>
        </div>

        {/* Modal Body with Scrollable BalanceAllocation Component */}
        <div className="p-4 sm:p-6 overflow-y-auto flex-1">
          <BalanceAllocation
            account={account}
            onAccountUpdated={onAccountUpdated}
            onNavigateTab={(tab) => {
              onClose();
              if (onNavigateTab) onNavigateTab(tab);
            }}
            onOpenKeyModal={() => {
              onClose();
              if (onOpenKeyModal) onOpenKeyModal();
            }}
            isModal={true}
            onCloseModal={onClose}
          />
        </div>
      </div>
    </div>
  );
};
