import React, { useState } from 'react';
import { BarChart3, PieChart, Sliders, Database, Info, Layers,  } from 'lucide-react';
import { StrategyAttributionCard } from '../components/StrategyAttributionCard';
import { ConservatismControlCard } from '../components/ConservatismControlCard';
import { BigQueryLakehouse } from '../components/BigQueryLakehouse';

export const AnalyticsPage: React.FC = () => {
  const [activeTab, setActiveTab] = useState<'OVERVIEW' | 'ATTRIBUTION' | 'CONSERVATISM' | 'LAKEHOUSE'>('OVERVIEW');

  return (
    <div className="space-y-6">
      {/* Page Header */}
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h2 className="text-xl font-bold text-zinc-100 flex items-center space-x-2">
            <BarChart3 className="w-5 h-5 text-indigo-400" />
            <span>Analytics & Performance Attribution</span>
          </h2>
          <p className="text-xs text-zinc-400 mt-1">
            Strategy virtual PnL attribution, conservatism detection, filter efficiency trade-offs, and BigQuery lakehouse telemetry.
          </p>
        </div>

        {/* Sub-view Navigation Tabs */}
        <div className="flex items-center space-x-1.5 bg-zinc-950 p-1 rounded-xl border border-zinc-800 text-xs font-mono">
          <button
            onClick={() => setActiveTab('OVERVIEW')}
            className={`px-3 py-1.5 rounded-lg font-bold transition-all flex items-center space-x-1.5 ${
              activeTab === 'OVERVIEW'
                ? 'bg-indigo-600 text-white shadow-sm'
                : 'text-zinc-400 hover:text-zinc-200'
            }`}
          >
            <Layers className="w-3.5 h-3.5" />
            <span>ALL MODULES</span>
          </button>

          <button
            onClick={() => setActiveTab('ATTRIBUTION')}
            className={`px-3 py-1.5 rounded-lg font-bold transition-all flex items-center space-x-1.5 ${
              activeTab === 'ATTRIBUTION'
                ? 'bg-indigo-600 text-white shadow-sm'
                : 'text-zinc-400 hover:text-zinc-200'
            }`}
          >
            <PieChart className="w-3.5 h-3.5" />
            <span>ALPHA ATTRIBUTION</span>
          </button>

          <button
            onClick={() => setActiveTab('CONSERVATISM')}
            className={`px-3 py-1.5 rounded-lg font-bold transition-all flex items-center space-x-1.5 ${
              activeTab === 'CONSERVATISM'
                ? 'bg-indigo-600 text-white shadow-sm'
                : 'text-zinc-400 hover:text-zinc-200'
            }`}
          >
            <Sliders className="w-3.5 h-3.5" />
            <span>CONSERVATISM</span>
          </button>

          <button
            onClick={() => setActiveTab('LAKEHOUSE')}
            className={`px-3 py-1.5 rounded-lg font-bold transition-all flex items-center space-x-1.5 ${
              activeTab === 'LAKEHOUSE'
                ? 'bg-indigo-600 text-white shadow-sm'
                : 'text-zinc-400 hover:text-zinc-200'
            }`}
          >
            <Database className="w-3.5 h-3.5" />
            <span>BIGQUERY LAKEHOUSE</span>
          </button>
        </div>
      </div>

      {/* Conditional or Stacked View */}
      {(activeTab === 'OVERVIEW' || activeTab === 'ATTRIBUTION') && (
        <StrategyAttributionCard />
      )}

      {(activeTab === 'OVERVIEW' || activeTab === 'CONSERVATISM') && (
        <ConservatismControlCard />
      )}

      {(activeTab === 'OVERVIEW' || activeTab === 'LAKEHOUSE') && (
        <BigQueryLakehouse />
      )}

      {/* Data Contract Lineage Footer */}
      <div className="p-4 rounded-xl bg-zinc-900/60 border border-zinc-800/80 text-xs text-zinc-400 space-y-2">
        <div className="flex items-center space-x-2 text-zinc-300 font-semibold">
          <Info className="w-4 h-4 text-cyan-400" />
          <span>Analytics & Attribution Contract Lineage (UI-09)</span>
        </div>
        <div className="grid grid-cols-1 md:grid-cols-3 gap-2 font-mono text-[11px]">
          <div className="p-2 bg-zinc-950 rounded border border-zinc-800">
            <span className="text-emerald-400 font-bold block">EXISTING (/api/bigquery/query):</span>
            Live BigQuery dataset, dry-run scan estimation ($5/TB), partitioned telemetry queries, ring-buffer flush.
          </div>
          <div className="p-2 bg-zinc-950 rounded border border-zinc-800">
            <span className="text-cyan-400 font-bold block">DERIVED_FRONTEND:</span>
            Virtual strategy PnL attribution, fee/funding drag decomposition, conservatism missed-move analytics.
          </div>
          <div className="p-2 bg-zinc-950 rounded border border-zinc-800">
            <span className="text-amber-400 font-bold block">PROPOSED_BACKEND (Epic UI-09):</span>
            Daily automated BigQuery attribution pipeline job (`analytics.daily_attribution`).
          </div>
        </div>
      </div>
    </div>
  );
};
