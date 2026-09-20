import React from 'react';
import {
  Layers,
  Shield,
  Activity,
  AlertTriangle,
  Compass,
} from 'lucide-react';
import { InstrumentData } from '../types';
import {  StructuralLevel } from '../types/market';

interface MarketStructureCardProps {
  instrument: InstrumentData | undefined;
  symbol: string;
}

export const MarketStructureCard: React.FC<MarketStructureCardProps> = ({
  instrument,
  symbol,
}) => {
  const hasVerifiedData =
    instrument?.verified === true &&
    (instrument.data_source === 'BINANCE_TESTNET' || instrument.data_source === 'BINANCE_MAINNET');
  // A current mark alone is not enough to invent swing levels, pivots, or
  // liquidity-touch counts. Those values must come from a verified OHLCV/
  // market-structure service.
  const structuralLevels: StructuralLevel[] = [];

  return (
    <div className="bg-zinc-900/90 border border-zinc-800 rounded-2xl p-4 sm:p-5 space-y-4 shadow-xl shadow-black/20">
      {/* Header */}
      <div className="flex flex-wrap items-center justify-between gap-2 border-b border-zinc-800/80 pb-3">
        <div className="flex items-center space-x-2">
          <div className="p-1.5 rounded-lg bg-cyan-950/80 border border-cyan-800/60 text-cyan-400">
            <Compass className="w-4 h-4" />
          </div>
          <div>
            <h3 className="text-xs font-bold text-zinc-100 uppercase tracking-wider">
              Price Action Priority & Structural Levels ({symbol})
            </h3>
            <p className="text-[11px] text-zinc-400">
              Measurable swing extrema, liquidity sweep zones, and acceptance/rejection
            </p>
          </div>
        </div>

        <div className="flex items-center space-x-2 text-[10px] font-mono">
          <span className="text-zinc-500">Structural Bias:</span>
          <span className="px-2 py-0.5 rounded font-bold bg-cyan-950 text-cyan-300 border border-cyan-800/80">
            {hasVerifiedData ? 'AWAITING STRUCTURE SNAPSHOT' : 'UNKNOWN (NO VERIFIED DATA)'}
          </span>
        </div>
      </div>

      {/* Primary Structural Gauges */}
      <div className="grid grid-cols-1 sm:grid-cols-3 gap-3 text-xs">
        {/* Metric 1: Raw Price Displacement */}
        <div className="p-3 bg-zinc-950/70 border border-zinc-800/80 rounded-xl space-y-1">
          <div className="flex items-center justify-between text-[10px] font-mono text-zinc-500 uppercase">
            <span>Displacement (24h)</span>
            <Activity className="w-3.5 h-3.5 text-cyan-400" />
          </div>
          <div className="text-sm font-mono font-bold text-emerald-400">
            UNKNOWN
          </div>
          <div className="text-[10px] text-zinc-400">
            Awaiting verified OHLCV structure data
          </div>
        </div>

        {/* Metric 2: Range Expansion Ratio */}
        <div className="p-3 bg-zinc-950/70 border border-zinc-800/80 rounded-xl space-y-1">
          <div className="flex items-center justify-between text-[10px] font-mono text-zinc-500 uppercase">
            <span>Range / ATR(24h)</span>
            <Layers className="w-3.5 h-3.5 text-indigo-400" />
          </div>
          <div className="text-sm font-mono font-bold text-zinc-200">
            UNKNOWN
          </div>
          <div className="text-[10px] text-zinc-400">
            Awaiting verified range and ATR window
          </div>
        </div>

        {/* Metric 3: Order Flow Absorption */}
        <div className="p-3 bg-zinc-950/70 border border-zinc-800/80 rounded-xl space-y-1">
          <div className="flex items-center justify-between text-[10px] font-mono text-zinc-500 uppercase">
            <span>Liquidity Absorption</span>
            <Shield className="w-3.5 h-3.5 text-emerald-400" />
          </div>
          <div className="text-sm font-mono font-bold text-emerald-400 flex items-center space-x-1">
            <AlertTriangle className="w-3.5 h-3.5" />
            <span>UNKNOWN</span>
          </div>
          <div className="text-[10px] text-zinc-400">
            Awaiting verified order-flow and structure data
          </div>
        </div>
      </div>

      {/* Measurable Structural Levels Table */}
      <div className="overflow-x-auto border border-zinc-800/80 rounded-xl">
        <table className="w-full text-left text-xs font-mono">
          <thead className="bg-zinc-950/80 border-b border-zinc-800 text-[10px] uppercase text-zinc-400">
            <tr>
              <th className="py-2 px-3">Structural Zone</th>
              <th className="py-2 px-3 text-right">Price Level</th>
              <th className="py-2 px-3 text-right">Distance to Mark</th>
              <th className="py-2 px-3 text-center">Status</th>
              <th className="py-2 px-3 text-center">Touch Count</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-zinc-800/60">
            {structuralLevels.length === 0 && (
              <tr>
                <td colSpan={5} className="py-6 px-3 text-center text-amber-400">
                  No verified structural snapshot is available for {symbol}.
                </td>
              </tr>
            )}
            {structuralLevels.map((lvl) => (
              <tr key={lvl.label} className="hover:bg-zinc-800/30">
                <td className="py-2.5 px-3 font-sans font-medium text-zinc-200">
                  {lvl.label}
                </td>
                <td className="py-2.5 px-3 text-right font-bold text-zinc-100">
                  ${lvl.price.toLocaleString(undefined, { minimumFractionDigits: 1, maximumFractionDigits: 1 })}
                </td>
                <td className="py-2.5 px-3 text-right text-zinc-400">
                  {lvl.distancePct > 0 ? '+' : ''}{lvl.distancePct.toFixed(2)}%
                </td>
                <td className="py-2.5 px-3 text-center">
                  <span
                    className={`px-2 py-0.5 rounded text-[10px] font-bold border ${
                      lvl.reclaimStatus === 'ACCEPTED'
                        ? 'bg-emerald-950 text-emerald-300 border-emerald-800/80'
                        : lvl.reclaimStatus === 'REJECTED'
                        ? 'bg-rose-950 text-rose-300 border-rose-800/80'
                        : lvl.reclaimStatus === 'SWEPT'
                        ? 'bg-orange-950 text-orange-300 border-orange-800/80'
                        : 'bg-zinc-800 text-zinc-300 border-zinc-700'
                    }`}
                  >
                    {lvl.reclaimStatus}
                  </span>
                </td>
                <td className="py-2.5 px-3 text-center text-zinc-400">
                  {lvl.touchCount}x
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
};
