import React, { useState } from 'react';
import { Layers, ArrowUpRight, ArrowDownRight, RefreshCw, XCircle, Shield, PlusCircle, FileSpreadsheet, ExternalLink } from 'lucide-react';
import { BasketItem, BasketState } from '../types';
import { useAuth } from '../context/AuthContext';
import { ConfirmationModal } from './ConfirmationModal';
import { IllustrativeEvidenceBanner } from './IllustrativeEvidenceBanner';

interface BasketManagerProps {
  baskets: BasketItem[];
  onExpandGrid: (basketId: string) => void;
  onEnterRecovery: (basketId: string) => void;
  onCloseBasket: (basketId: string) => void;
  isActionLoading?: boolean;
}

export const BasketManager: React.FC<BasketManagerProps> = ({
  baskets,
  onExpandGrid,
  onEnterRecovery,
  onCloseBasket,
  isActionLoading,
}) => {
  const { user, signIn, exportToGoogleSheet, isSigningIn } = useAuth();
  const [isExporting, setIsExporting] = useState<boolean>(false);
  const [lastExportUrl, setLastExportUrl] = useState<string | null>(null);

  // Safety Confirmation State
  const [pendingAction, setPendingAction] = useState<{
    type: 'EXPAND' | 'RECOVERY' | 'CLOSE';
    basketId: string;
    instrument: string;
  } | null>(null);

  const handleQuickExport = async () => {
    if (isSigningIn) return;
    if (!user) {
      await signIn();
      return;
    }
    setIsExporting(true);
    try {
      const res = await exportToGoogleSheet(baskets, {});
      setLastExportUrl(res.spreadsheetUrl);
    } catch (err: any) {
      alert(`Google Sheets export error: ${err.message}`);
    } finally {
      setIsExporting(false);
    }
  };

  const getStateBadge = (state: BasketState) => {
    switch (state) {
      case 'ACTIVE':
        return 'bg-emerald-500/10 text-emerald-400 border-emerald-500/30';
      case 'PROFITABLE':
        return 'bg-cyan-500/10 text-cyan-400 border-cyan-500/30 animate-pulse';
      case 'RECOVERY':
        return 'bg-amber-500/10 text-amber-400 border-amber-500/30';
      case 'NO_NEW_GRID':
        return 'bg-orange-500/10 text-orange-400 border-orange-500/30';
      case 'DELEVERAGING':
        return 'bg-purple-500/10 text-purple-400 border-purple-500/30';
      case 'EMERGENCY_EXIT':
      case 'CLOSING':
        return 'bg-rose-500/10 text-rose-400 border-rose-500/30';
      default:
        return 'bg-zinc-800 text-zinc-400 border-zinc-700';
    }
  };

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h2 className="text-sm font-semibold uppercase tracking-wider text-zinc-400 flex items-center space-x-2">
          <Layers className="w-4 h-4 text-emerald-400" />
          <span>Active Baskets (Recovery & Progression)</span>
        </h2>
        <div className="flex items-center space-x-2">
          {lastExportUrl ? (
            <a
              href={lastExportUrl}
              target="_blank"
              rel="noopener noreferrer"
              className="flex items-center space-x-1 px-2.5 py-1 bg-emerald-950/60 hover:bg-emerald-900/60 text-emerald-300 border border-emerald-800/60 rounded-lg text-xs font-medium transition-colors"
              title="Open the generated Google Sheet"
            >
              <span>Sheet Created</span>
              <ExternalLink className="w-3 h-3" />
            </a>
          ) : null}
          <button
            type="button"
            onClick={handleQuickExport}
            disabled={isExporting}
            className="flex items-center space-x-1.5 px-2.5 py-1 bg-zinc-900 hover:bg-zinc-800 text-zinc-300 hover:text-emerald-400 border border-zinc-800 rounded-lg text-xs font-medium transition-colors cursor-pointer"
            title="Export active baskets to a newly created Google Sheet"
          >
            <FileSpreadsheet className="w-3.5 h-3.5 text-emerald-400" />
            <span>{isExporting ? 'Exporting...' : 'Export to Sheets'}</span>
          </button>
        </div>
      </div>

      {baskets.some((basket) => basket.verified !== true || (basket.data_source !== 'BINANCE_TESTNET' && basket.data_source !== 'BINANCE_MAINNET')) && (
        <IllustrativeEvidenceBanner message="Displayed basket rows are not verified against the Python worker ledger; mutation controls stay hidden." />
      )}

      <div className="grid grid-cols-1 gap-4">
        {baskets.map((basket) => {
          const isLong = basket.direction === 'LONG';
          const hasVerifiedData = basket.verified === true && (basket.data_source === 'BINANCE_TESTNET' || basket.data_source === 'BINANCE_MAINNET');
          return (
            <div
              key={basket.basket_id}
              className="bg-zinc-900/80 border border-zinc-800 rounded-xl p-5 space-y-4"
            >
              {/* Top Row: Symbol, Direction, State, ID */}
              <div className="flex flex-wrap items-center justify-between gap-3 border-b border-zinc-800/80 pb-3">
                <div className="flex items-center space-x-3">
                  <div className={`p-2 rounded-lg ${isLong ? 'bg-emerald-500/10 text-emerald-400' : 'bg-rose-500/10 text-rose-400'}`}>
                    {isLong ? <ArrowUpRight className="w-5 h-5" /> : <ArrowDownRight className="w-5 h-5" />}
                  </div>
                  <div>
                    <div className="flex items-center space-x-2">
                      <span className="font-bold text-zinc-100 text-lg">{basket.instrument}</span>
                      <span className={`text-xs px-2 py-0.5 rounded font-bold ${isLong ? 'bg-emerald-500/20 text-emerald-300' : 'bg-rose-500/20 text-rose-300'}`}>
                        {basket.direction}
                      </span>
                      <span className={`text-xs px-2.5 py-0.5 rounded-full border font-semibold ${getStateBadge(basket.state)}`}>
                        {basket.state}
                      </span>
                    </div>
                    <div className="text-xs font-mono text-zinc-400 mt-0.5">
                      ID: {basket.basket_id} • Created: {new Date(basket.created_at).toLocaleTimeString()}
                    </div>
                  </div>
                  {!hasVerifiedData && (
                    <span className="text-[10px] font-mono text-amber-400">SIMULATED / UNVERIFIED</span>
                  )}
                </div>

                {/* Net Basket PnL */}
                <div className="text-right">
                  <div className="text-xs text-zinc-400">Net Basket PnL</div>
                  <div className={`text-2xl font-bold font-mono ${!hasVerifiedData ? 'text-amber-400' : (basket.net_pnl ?? 0) >= 0 ? 'text-emerald-400' : 'text-rose-400'}`}>
                    {hasVerifiedData ? `${(basket.net_pnl ?? 0) >= 0 ? '+' : ''}$${(basket.net_pnl ?? 0).toFixed(2)}` : 'UNKNOWN'}
                  </div>
                  <div className="text-[11px] text-zinc-400 flex items-center justify-end space-x-2 mt-0.5">
                    <span>Unrealized: {hasVerifiedData ? `$${(basket.unrealized_pnl ?? 0).toFixed(2)}` : 'UNKNOWN'}</span>
                    <span>•</span>
                    <span className={(basket.funding_pnl ?? 0) >= 0 ? 'text-cyan-400' : 'text-amber-400'}>
                      Funding: {hasVerifiedData ? `$${(basket.funding_pnl ?? 0).toFixed(2)}` : 'UNKNOWN'}
                    </span>
                  </div>
                </div>
              </div>

              {/* Basket Geometry & Metrics */}
              <div className="grid grid-cols-2 sm:grid-cols-4 gap-3 bg-zinc-950/60 p-3 rounded-lg border border-zinc-800/60 text-xs">
                <div>
                  <div className="text-zinc-400 text-[11px]">Average Entry</div>
                  <div className="text-sm font-mono font-bold text-zinc-200 mt-0.5">
                    {hasVerifiedData ? `$${(basket.average_entry ?? 0).toLocaleString('en-US', { minimumFractionDigits: 2 })}` : 'UNKNOWN'}
                  </div>
                  <div className="text-[10px] text-zinc-400">Mark: {hasVerifiedData ? `$${(basket.current_mark_price ?? 0).toLocaleString()}` : 'UNKNOWN'}</div>
                </div>

                <div>
                  <div className="text-zinc-400 text-[11px]">Total Position Size</div>
                  <div className="text-sm font-mono font-bold text-zinc-200 mt-0.5">
                    {hasVerifiedData ? `${(basket.total_size ?? 0).toFixed(3)} ${(basket.instrument || '').replace('USDT', '')}` : 'UNKNOWN'}
                  </div>
                  <div className="text-[10px] text-zinc-400">Notional: {hasVerifiedData ? `~$${((basket.total_size ?? 0) * (basket.current_mark_price ?? 0)).toFixed(0)}` : 'UNKNOWN'}</div>
                </div>

                <div>
                  <div className="text-zinc-400 text-[11px]">Grid Depth</div>
                  <div className="text-sm font-mono font-bold text-zinc-200 mt-0.5">
                    {hasVerifiedData ? `Level ${basket.grid_depth ?? 0} of ${basket.max_grid_levels ?? 5}` : 'UNKNOWN'}
                  </div>
                  <div className="text-[10px] text-zinc-400">Progression Cap: L5</div>
                </div>

                <div>
                  <div className="text-zinc-400 text-[11px]">Friction & Fees</div>
                  <div className="text-sm font-mono font-medium text-zinc-300 mt-0.5">
                    {hasVerifiedData ? `-$${(basket.trading_fees ?? 0).toFixed(2)}` : 'UNKNOWN'}
                  </div>
                  <div className="text-[10px] text-zinc-400">Slippage: {hasVerifiedData ? `-$${(basket.slippage_cost ?? 0).toFixed(2)}` : 'UNKNOWN'}</div>
                </div>
              </div>

              {/* Grid Levels Ladder Visualizer */}
              <div className="space-y-2">
                <div className="text-xs font-semibold text-zinc-400 flex items-center justify-between">
                  <span>Basket Grid Ladder (Controlled Anti-Martingale Progression)</span>
                  <span>Distances dynamically calibrated by ATR & Regime</span>
                </div>
                {hasVerifiedData ? <div className="grid grid-cols-2 sm:grid-cols-5 gap-2">
                  {(basket.grid_levels || []).map((lvl) => {
                    const isFilled = lvl.status === 'FILLED';
                    return (
                      <div
                        key={lvl.level}
                        className={`p-2.5 rounded-lg border text-xs space-y-1 transition-all ${
                          isFilled
                            ? 'bg-emerald-950/20 border-emerald-600/40 text-emerald-300'
                            : 'bg-zinc-950/40 border-zinc-800 text-zinc-400'
                        }`}
                      >
                        <div className="flex items-center justify-between text-[11px] font-semibold">
                          <span>Level {lvl.level}</span>
                          <span className={`text-[10px] px-1.5 py-0.2 rounded ${isFilled ? 'bg-emerald-500/20 text-emerald-400' : 'bg-zinc-800 text-zinc-400'}`}>
                            {lvl.status}
                          </span>
                        </div>
                        <div className="font-mono text-zinc-200 font-medium">
                          ${(lvl.price ?? 0).toLocaleString()}
                        </div>
                        <div className="text-[10px] text-zinc-400 flex justify-between">
                          <span>Size: {(lvl.size ?? 0).toFixed(3)}</span>
                          {isFilled && <span className="text-emerald-400">Active</span>}
                        </div>
                      </div>
                    );
                  })}
                </div> : <div className="text-[11px] text-amber-400">Grid ladder is unavailable until a verified Testnet basket snapshot is loaded.</div>}
              </div>

              {/* Basket Lifecycle Action Controls */}
              {hasVerifiedData && <div className="flex flex-wrap items-center justify-end gap-2 pt-2 border-t border-zinc-800/80">
                {basket.state === 'ACTIVE' && basket.grid_depth < basket.max_grid_levels && (
                  <button
                    onClick={() => setPendingAction({ type: 'EXPAND', basketId: basket.basket_id, instrument: basket.instrument })}
                    disabled={isActionLoading}
                    className="flex items-center space-x-1.5 px-3 py-1.5 rounded-md text-xs font-medium bg-zinc-800 hover:bg-zinc-700 text-zinc-200 border border-zinc-700 transition"
                  >
                    <PlusCircle className="w-3.5 h-3.5 text-indigo-400" />
                    <span>Expand Next Grid Step</span>
                  </button>
                )}

                {basket.state !== 'RECOVERY' && basket.state !== 'CLOSED' && (
                  <button
                    onClick={() => setPendingAction({ type: 'RECOVERY', basketId: basket.basket_id, instrument: basket.instrument })}
                    disabled={isActionLoading}
                    className="flex items-center space-x-1.5 px-3 py-1.5 rounded-md text-xs font-medium bg-amber-500/10 hover:bg-amber-500/20 text-amber-300 border border-amber-500/30 transition"
                  >
                    <Shield className="w-3.5 h-3.5" />
                    <span>Trigger Soft Recovery</span>
                  </button>
                )}

                <button
                  onClick={() => setPendingAction({ type: 'CLOSE', basketId: basket.basket_id, instrument: basket.instrument })}
                  disabled={isActionLoading}
                  className="flex items-center space-x-1.5 px-3 py-1.5 rounded-md text-xs font-medium bg-rose-500/10 hover:bg-rose-500/20 text-rose-300 border border-rose-500/30 transition"
                >
                  <XCircle className="w-3.5 h-3.5" />
                  <span>Close Basket (Market)</span>
                </button>
              </div>}
            </div>
          );
        })}
      </div>

      <ConfirmationModal
        isOpen={pendingAction?.type === 'CLOSE'}
        title="Close Basket (Market Order)"
        message={
          <>
            You are about to issue a <strong>Market Order</strong> to flatten basket <span className="font-mono text-zinc-200">{pendingAction?.basketId}</span> for <span className="font-bold">{pendingAction?.instrument}</span>. This will realize all PnL and close open grid levels.
          </>
        }
        confirmText="Execute Market Close"
        isDestructive={true}
        requireTypedConfirmation="CLOSE"
        isLoading={isActionLoading}
        onConfirm={() => {
          if (pendingAction) {
            onCloseBasket(pendingAction.basketId);
            setPendingAction(null);
          }
        }}
        onCancel={() => setPendingAction(null)}
      />

      <ConfirmationModal
        isOpen={pendingAction?.type === 'RECOVERY'}
        title="Trigger Soft Recovery"
        message={
          <>
            You are moving <span className="font-bold">{pendingAction?.instrument}</span> to RECOVERY state. New grid entries will be paused, and the Exposure Recovery Engine will manage deleveraging.
          </>
        }
        confirmText="Engage Recovery"
        isDestructive={false}
        isLoading={isActionLoading}
        onConfirm={() => {
          if (pendingAction) {
            onEnterRecovery(pendingAction.basketId);
            setPendingAction(null);
          }
        }}
        onCancel={() => setPendingAction(null)}
      />

      <ConfirmationModal
        isOpen={pendingAction?.type === 'EXPAND'}
        title="Expand Grid Progression"
        message={
          <>
            Manually overriding the Portfolio Risk Governor to force the next grid progression step for <span className="font-bold">{pendingAction?.instrument}</span>. This increases gross exposure.
          </>
        }
        confirmText="Expand Grid"
        isDestructive={false}
        isLoading={isActionLoading}
        onConfirm={() => {
          if (pendingAction) {
            onExpandGrid(pendingAction.basketId);
            setPendingAction(null);
          }
        }}
        onCancel={() => setPendingAction(null)}
      />
    </div>
  );
};
