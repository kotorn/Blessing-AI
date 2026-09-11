import React, { useState } from 'react';
import {
  LineChart,
  Layers,
  Compass,
  Zap,
  DollarSign,
  Info,
  Clock,
  ChevronRight,
  ShieldCheck,
} from 'lucide-react';
import { InstrumentData } from '../types';
import { InstrumentsPanel } from '../components/InstrumentsPanel';
import { InstrumentWorkspace } from '../components/InstrumentWorkspace';

interface MarketsPageProps {
  instruments: Record<string, InstrumentData>;
}

export const MarketsPage: React.FC<MarketsPageProps> = ({ instruments }) => {
  const [selectedSymbol, setSelectedSymbol] = useState<string>('BTCUSDT');

  const activeInstrument = instruments[selectedSymbol] || instruments['BTCUSDT'];

  return (
    <div className="space-y-6">
      {/* Page Header */}
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h2 className="text-xl font-bold text-zinc-100 flex items-center space-x-2">
            <LineChart className="w-5 h-5 text-cyan-400" />
            <span>Market Opportunity Board & Price Action Architecture</span>
          </h2>
          <p className="text-xs text-zinc-400 mt-1">
            Deterministic price displacement, multi-horizon shock detection, market structure, spot/perp basis, and regime classification.
          </p>
        </div>

        {/* Symbol Switcher Tabs */}
        <div className="flex items-center space-x-1.5 bg-zinc-900 p-1 rounded-xl border border-zinc-800 text-xs">
          <button
            type="button"
            onClick={() => setSelectedSymbol('BTCUSDT')}
            className={`px-3 py-1.5 rounded-lg font-bold font-mono transition-all cursor-pointer ${
              selectedSymbol === 'BTCUSDT'
                ? 'bg-cyan-600 text-white shadow'
                : 'text-zinc-400 hover:text-zinc-200'
            }`}
          >
            BTCUSDT
          </button>
          <button
            type="button"
            onClick={() => setSelectedSymbol('ETHUSDT')}
            className={`px-3 py-1.5 rounded-lg font-bold font-mono transition-all cursor-pointer ${
              selectedSymbol === 'ETHUSDT'
                ? 'bg-cyan-600 text-white shadow'
                : 'text-zinc-400 hover:text-zinc-200'
            }`}
          >
            ETHUSDT
          </button>

          {/* Roadmap Phase 2 Instruments */}
          <span className="h-4 w-px bg-zinc-700/60 mx-1" />
          <span
            title="Roadmap Stage 2: SOLUSDT support"
            className="px-2 py-1 rounded text-[10px] font-mono text-zinc-500 bg-zinc-950 border border-zinc-800 cursor-not-allowed"
          >
            SOLUSDT (Stage 2)
          </span>
          <span
            title="Roadmap Stage 2: BNBUSDT support"
            className="px-2 py-1 rounded text-[10px] font-mono text-zinc-500 bg-zinc-950 border border-zinc-800 cursor-not-allowed"
          >
            BNBUSDT (Stage 2)
          </span>
        </div>
      </div>

      {/* Dedicated Instrument Workspace */}
      <InstrumentWorkspace instrument={activeInstrument} symbol={selectedSymbol} />

      {/* Multi-Asset Research Overview Board */}
      <div className="space-y-3">
        <h3 className="text-xs font-bold text-zinc-400 uppercase tracking-wider font-mono">
          Comparative Cross-Asset Regime Spectrum
        </h3>
        <InstrumentsPanel instruments={instruments} />
      </div>

      {/* Data Contract Transparency Footer */}
      <div className="p-4 rounded-xl bg-zinc-900/60 border border-zinc-800/80 text-xs text-zinc-400 space-y-2">
        <div className="flex items-center space-x-2 text-zinc-300 font-semibold">
          <Info className="w-4 h-4 text-cyan-400" />
          <span>Market State Architecture & Data Lineage (UI-04)</span>
        </div>
        <div className="grid grid-cols-1 md:grid-cols-2 gap-2 font-mono text-[11px]">
          <div className="p-2 bg-zinc-950 rounded border border-zinc-800">
            <span className="text-emerald-400 font-bold block">EXISTING (/api/quant/state & WebSockets):</span>
            Spot price, Perp price, Basis spread, Funding rate (8h/APR), 24h Realized Vol, Regime probabilities. L2/L3 order flow imbalance, aggregate CVD delta feeds, and real-time WebSocket tick book depth.
          </div>
          <div className="p-2 bg-zinc-950 rounded border border-zinc-800">
            <span className="text-cyan-400 font-bold block">DERIVED_FRONTEND:</span>
            Multi-horizon rolling shock percentiles (5s, 15s, 1m, 5m), Velocity/Acceleration, Swing high/low structural levels.
          </div>
        </div>
      </div>
    </div>
  );
};
