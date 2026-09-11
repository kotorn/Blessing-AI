import React, { useState } from 'react';
import { Play, RotateCcw, AlertTriangle, CheckCircle, BarChart2, ShieldAlert } from 'lucide-react';
import { BacktestMetrics } from '../types';

export const BacktestReplayStudio: React.FC = () => {
  const [selectedScenario, setSelectedScenario] = useState<'covid_2020' | 'luna_2022' | 'ftx_2022' | 'bull_2024'>('covid_2020');
  const [isRunning, setIsRunning] = useState(false);
  const [results, setResults] = useState<BacktestMetrics | null>(null);

  const runScenario = async (scenario: string) => {
    setIsRunning(true);
    try {
      const resp = await fetch('/api/quant/backtest/run', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
        body: JSON.stringify({ scenario }),
      });
      const contentType = resp.headers.get('content-type');
      if (resp.ok && contentType && contentType.includes('application/json')) {
        const data = await resp.json();
        setResults(data.metrics || data);
      }
    } catch (e) {
      console.warn('Backtest run deferred:', e);
    } finally {
      setIsRunning(false);
    }
  };

  return (
    <div className="space-y-6">
      <div className="bg-zinc-900/80 border border-zinc-800 rounded-xl p-5 space-y-4">
        <div>
          <h2 className="text-lg font-bold text-zinc-100 flex items-center space-x-2">
            <BarChart2 className="w-5 h-5 text-indigo-400" />
            <span>Event-Driven Backtester & Historical Replay Engine (Section 22 & 23)</span>
          </h2>
          <p className="text-xs text-zinc-400 mt-1">
            Replays tick-by-tick orderbook, funding settlement, basis shocks, and latency slippage through exact live execution models.
          </p>
        </div>

        {/* Scenario Selector */}
        <div className="grid grid-cols-1 sm:grid-cols-4 gap-3">
          {[
            { id: 'covid_2020', title: 'COVID-19 Flash Crash', desc: 'March 2020: -50% 24h drop, extreme liquidations' },
            { id: 'luna_2022', title: 'Terra / Luna Depeg', desc: 'May 2022: Death spiral & basis collapse' },
            { id: 'ftx_2022', title: 'FTX Insolvency Shock', desc: 'Nov 2022: Contagion & bidless market depth' },
            { id: 'bull_2024', title: '2024 ETF Expansion', desc: 'Strong directional trend & positive funding drag' },
          ].map((sc) => (
            <button
              key={sc.id}
              onClick={() => setSelectedScenario(sc.id as any)}
              className={`p-3 rounded-lg border text-left transition-all ${
                selectedScenario === sc.id
                  ? 'bg-indigo-950/40 border-indigo-500 text-white'
                  : 'bg-zinc-950/40 border-zinc-800 hover:border-zinc-700 text-zinc-400'
              }`}
            >
              <div className="font-semibold text-xs text-zinc-200">{sc.title}</div>
              <div className="text-[11px] text-zinc-400 mt-1 leading-snug">{sc.desc}</div>
            </button>
          ))}
        </div>

        <div className="flex items-center space-x-3 pt-2">
          <button
            onClick={() => runScenario(selectedScenario)}
            disabled={isRunning}
            className="flex items-center space-x-2 px-4 py-2 rounded-lg bg-indigo-600 hover:bg-indigo-500 text-white text-xs font-semibold shadow-lg shadow-indigo-600/20 disabled:opacity-50"
          >
            {isRunning ? <RotateCcw className="w-4 h-4 animate-spin" /> : <Play className="w-4 h-4" />}
            <span>{isRunning ? 'Simulating High-Frequency Replay...' : 'Execute Historical Replay'}</span>
          </button>
          <span className="text-xs text-zinc-400">Deterministic event-driven queue with 0ms lookahead bias</span>
        </div>
      </div>

      {/* Results View */}
      {results && (
        <div className="bg-zinc-900/80 border border-zinc-800 rounded-xl p-5 space-y-5 animate-fade-in">
          <div className="flex items-center justify-between border-b border-zinc-800 pb-3">
            <div>
              <span className="text-xs text-zinc-400 uppercase font-mono">Backtest Report</span>
              <h3 className="text-lg font-bold text-zinc-100">{results.name}</h3>
            </div>
            <div className="text-right">
              <span className="text-xs text-zinc-400">Total Baskets Replayed</span>
              <div className="text-base font-bold font-mono text-zinc-200">{results.total_baskets} Baskets</div>
            </div>
          </div>

          {/* Core Quant Metrics Table (Section 23 Mandate) */}
          <div className="grid grid-cols-2 md:grid-cols-4 gap-3 text-xs">
            <div className="bg-zinc-950/60 p-3 rounded-lg border border-zinc-800">
              <div className="text-zinc-400 text-[11px]">Net Return / ROI</div>
              <div className="text-lg font-bold font-mono text-emerald-400 mt-0.5">
                +{(results.roi_pct ?? 0).toFixed(2)}%
              </div>
              <div className="text-[10px] text-zinc-400 font-mono">+${(results.net_profit ?? 0).toLocaleString()}</div>
            </div>

            <div className="bg-zinc-950/60 p-3 rounded-lg border border-zinc-800">
              <div className="text-zinc-400 text-[11px]">Max Equity Drawdown</div>
              <div className="text-lg font-bold font-mono text-amber-400 mt-0.5">
                -{(results.max_equity_drawdown_pct ?? 0).toFixed(2)}%
              </div>
              <div className="text-[10px] text-zinc-400">Balance DD: -{(results.max_balance_drawdown_pct ?? 0).toFixed(2)}%</div>
            </div>

            <div className="bg-zinc-950/60 p-3 rounded-lg border border-zinc-800">
              <div className="text-zinc-400 text-[11px]">Equity vs Balance Div.</div>
              <div className="text-lg font-bold font-mono text-zinc-200 mt-0.5">
                {(results.equity_balance_divergence_pct ?? 0).toFixed(2)}%
              </div>
              <div className="text-[10px] text-zinc-400">Floating unrealized gap</div>
            </div>

            <div className="bg-zinc-950/60 p-3 rounded-lg border border-zinc-800">
              <div className="text-zinc-400 text-[11px]">Ulcer Index & Shortfall</div>
              <div className="text-lg font-bold font-mono text-zinc-200 mt-0.5">
                {(results.ulcer_index ?? 0).toFixed(2)}
              </div>
              <div className="text-[10px] text-zinc-400">ES (99%): {(results.expected_shortfall_99_pct ?? 0).toFixed(2)}%</div>
            </div>

            <div className="bg-zinc-950/60 p-3 rounded-lg border border-zinc-800">
              <div className="text-zinc-400 text-[11px]">Time Under Water</div>
              <div className="text-base font-bold font-mono text-zinc-200 mt-0.5">
                {(results.time_under_water_hrs ?? 0).toFixed(0)} hrs
              </div>
              <div className="text-[10px] text-zinc-400">Longest: {(results.longest_recovery_hrs ?? 0).toFixed(0)} hrs</div>
            </div>

            <div className="bg-zinc-950/60 p-3 rounded-lg border border-zinc-800">
              <div className="text-zinc-400 text-[11px]">Max Grid Depth (P95)</div>
              <div className="text-base font-bold font-mono text-zinc-200 mt-0.5">
                Level {results.max_grid_depth_reached ?? 0} (P95: L{results.grid_depth_p95 ?? 0})
              </div>
              <div className="text-[10px] text-zinc-400">Progression capped at 5</div>
            </div>

            <div className="bg-zinc-950/60 p-3 rounded-lg border border-zinc-800">
              <div className="text-zinc-400 text-[11px]">Friction & Drag Breakdown</div>
              <div className="text-base font-bold font-mono text-rose-400 mt-0.5">
                -${(((results.total_funding_cost ?? 0) + (results.total_trading_fees ?? 0) + (results.slippage_cost ?? 0))).toFixed(0)}
              </div>
              <div className="text-[10px] text-zinc-400">
                Funding: -${(results.total_funding_cost ?? 0).toFixed(0)} | Fees: -${(results.total_trading_fees ?? 0).toFixed(0)}
              </div>
            </div>

            <div className="bg-zinc-950/60 p-3 rounded-lg border border-zinc-800">
              <div className="text-zinc-400 text-[11px]">Return / Floating DD Ratio</div>
              <div className="text-base font-bold font-mono text-emerald-400 mt-0.5">
                {(results.profit_to_floating_dd_ratio ?? 0).toFixed(2)}x
              </div>
              <div className="text-[10px] text-zinc-400">Emergency Exits: {results.emergency_exits ?? 0}</div>
            </div>
          </div>
        </div>
      )}
    </div>
  );
};
