import React from 'react';
import { MultiHorizonShockMonitor } from './MultiHorizonShockMonitor';
import { MicrostructureMonitor } from './MicrostructureMonitor';
import { MarketStructureCard } from './MarketStructureCard';
import { BasisFundingCarryMonitor } from './BasisFundingCarryMonitor';
import { InstrumentData } from '../types';
import { Activity, Focus, LineChart } from 'lucide-react';

interface InstrumentWorkspaceProps {
  instrument: InstrumentData | undefined;
  symbol: string;
}

export const InstrumentWorkspace: React.FC<InstrumentWorkspaceProps> = ({
  instrument,
  symbol,
}) => {
  if (!instrument) {
    return (
      <div className="flex flex-col items-center justify-center py-20 bg-zinc-900/50 rounded-2xl border border-zinc-800 border-dashed">
        <LineChart className="w-8 h-8 text-zinc-600 mb-3" />
        <h3 className="text-zinc-400 font-medium">Instrument Data Unavailable</h3>
        <p className="text-xs text-zinc-500 mt-1">No data for {symbol}</p>
      </div>
    );
  }

  return (
    <div className="space-y-4">
      <div className="flex items-center space-x-2 pb-2 border-b border-zinc-800/80">
        <Focus className="w-5 h-5 text-cyan-400" />
        <h3 className="text-sm font-bold text-zinc-100 uppercase tracking-wider">
          {symbol} Instrument Workspace
        </h3>
        <span className="px-2 py-0.5 rounded text-[10px] font-bold bg-cyan-950 text-cyan-300 border border-cyan-800/80">
          DEEP DIVE
        </span>
      </div>

      <div className="grid grid-cols-1 xl:grid-cols-12 gap-4">
        {/* Left Column: Flow & Microstructure */}
        <div className="xl:col-span-7 space-y-4">
          <MicrostructureMonitor symbol={symbol} />
          
          <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
            <BasisFundingCarryMonitor instrument={instrument} symbol={symbol} />
            {/* Can add another component here if needed, for now MultiHorizon fits well below */}
          </div>
        </div>

        {/* Right Column: Structure & Shock */}
        <div className="xl:col-span-5 space-y-4">
          <MultiHorizonShockMonitor instrument={instrument} symbol={symbol} />
          <MarketStructureCard instrument={instrument} symbol={symbol} />
        </div>
      </div>
    </div>
  );
};
