import React from 'react';
import { ShieldCheck, BarChart3, TrendingUp, Zap, Gauge, AlertCircle } from 'lucide-react';
import { InstrumentData, MarketRegimeType } from '../types';

interface InstrumentsPanelProps {
  instruments: Record<string, InstrumentData>;
}

export const InstrumentsPanel: React.FC<InstrumentsPanelProps> = ({ instruments }) => {
  const getRegimeColor = (regime: MarketRegimeType) => {
    switch (regime) {
      case 'R0_STRONG_MEAN_REVERSION':
        return 'text-cyan-400 bg-cyan-500/10 border-cyan-500/20';
      case 'R1_RANGE':
        return 'text-emerald-400 bg-emerald-500/10 border-emerald-500/20';
      case 'R2_WEAK_TREND':
        return 'text-amber-400 bg-amber-500/10 border-amber-500/20';
      case 'R3_STRONG_TREND':
        return 'text-orange-400 bg-orange-500/10 border-orange-500/20';
      case 'R4_BREAKOUT':
      case 'R5_VOLATILITY_SHOCK':
      case 'R6_CRISIS':
        return 'text-rose-400 bg-rose-500/10 border-rose-500/20';
    }
  };

  const getSafetyBadge = (score: number) => {
    if (score >= 80) return { label: 'Grid Allowed', color: 'text-emerald-400 bg-emerald-500/10 border-emerald-500/30' };
    if (score >= 65) return { label: 'Allowed (Reduced)', color: 'text-amber-400 bg-amber-500/10 border-amber-500/30' };
    if (score >= 50) return { label: 'Conservative Only', color: 'text-orange-400 bg-orange-500/10 border-orange-500/30' };
    if (score >= 35) return { label: 'Recovery Only', color: 'text-rose-400 bg-rose-500/10 border-rose-500/30' };
    return { label: 'No New Grid', color: 'text-rose-500 bg-rose-500/20 border-rose-500/40' };
  };

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <h2 className="text-sm font-semibold uppercase tracking-wider text-zinc-400 flex items-center space-x-2">
          <BarChart3 className="w-4 h-4 text-indigo-400" />
          <span>Active Research Instruments (USDⓈ-M Futures)</span>
        </h2>
        <span className="text-xs text-zinc-400">Adaptive Volatility & Regime Driven</span>
      </div>

      <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
        {(Object.values(instruments) as InstrumentData[]).map((inst) => {
          const hasVerifiedData =
            inst.verified === true &&
            inst.data_source === 'BINANCE_TESTNET';
          const safety = hasVerifiedData
            ? getSafetyBadge(inst.grid_safety_score)
            : { label: 'UNKNOWN', color: 'text-amber-400 bg-amber-500/10 border-amber-500/30' };
          return (
            <div key={inst.symbol} className="bg-zinc-900/80 border border-zinc-800 rounded-xl p-4 space-y-4">
              {/* Instrument Header */}
              <div className="flex items-center justify-between border-b border-zinc-800/80 pb-3">
                <div className="flex items-center space-x-3">
                  <div className="w-8 h-8 rounded-lg bg-zinc-800 border border-zinc-700 flex items-center justify-center font-bold text-xs text-zinc-200">
                    {inst.symbol.slice(0, 3)}
                  </div>
                  <div>
                    <div className="flex items-center space-x-2">
                      <span className="font-bold text-zinc-100 text-base">{inst.symbol}</span>
                      <span className={`text-[11px] px-2 py-0.5 rounded border font-medium ${hasVerifiedData ? getRegimeColor(inst.regime) : 'text-amber-400 bg-amber-500/10 border-amber-500/20'}`}>
                        {hasVerifiedData ? inst.regime.replace('_', ' ') : 'UNKNOWN'}
                      </span>
                    </div>
                    <div className="text-xs text-zinc-400 flex items-center space-x-2 mt-0.5">
                      <span>Perp: {hasVerifiedData ? `$${inst.perp_price.toLocaleString()}` : 'UNKNOWN'}</span>
                      <span>•</span>
                      <span>Spot: {hasVerifiedData ? `$${inst.spot_price.toLocaleString()}` : 'UNKNOWN'}</span>
                    </div>
                  </div>
                </div>

                <div className="text-right">
                  <div className="text-xs text-zinc-400">Grid Safety {hasVerifiedData ? 'Worker Data' : 'Unverified'}</div>
                  <div className="flex items-center justify-end space-x-1.5 mt-0.5">
                    <span className="text-lg font-bold font-mono text-zinc-100">{hasVerifiedData ? inst.grid_safety_score : 'UNKNOWN'}</span>
                    {hasVerifiedData && <span className="text-xs text-zinc-400">/100</span>}
                  </div>
                </div>
              </div>

              {/* Grid Safety Status & Expected Metrics */}
              <div className="bg-zinc-950/60 p-3 rounded-lg border border-zinc-800/60 grid grid-cols-3 gap-2 text-center text-xs">
                <div>
                  <div className="text-zinc-400 text-[11px]">Grid Action</div>
                  <div className={`mt-1 font-semibold px-1.5 py-0.5 rounded border text-[11px] inline-block ${safety.color}`}>
                    {safety.label}
                  </div>
                </div>
                <div>
                  <div className="text-zinc-400 text-[11px]">Exp. MAE</div>
                  <div className="mt-1 font-mono text-zinc-200 font-medium">{hasVerifiedData ? `${inst.expected_mae_pct.toFixed(2)}%` : 'UNKNOWN'}</div>
                </div>
                <div>
                  <div className="text-zinc-400 text-[11px]">Est. Recovery</div>
                  <div className="mt-1 font-mono text-zinc-200 font-medium">{hasVerifiedData ? `${inst.expected_recovery_time_hrs.toFixed(1)} hrs` : 'UNKNOWN'}</div>
                </div>
              </div>

              {/* Microstructure Metrics: Funding, Basis, OI, ATR */}
              <div className="grid grid-cols-4 gap-2 text-xs">
                <div className="bg-zinc-800/40 p-2 rounded border border-zinc-800">
                  <div className="text-zinc-400 text-[10px]">Funding Rate (8h)</div>
                  <div className={`font-mono font-medium mt-0.5 ${!hasVerifiedData ? 'text-amber-400' : inst.funding_rate >= 0 ? 'text-amber-400' : 'text-cyan-400'}`}>
                    {hasVerifiedData ? `${(inst.funding_rate * 100).toFixed(4)}%` : 'UNKNOWN'}
                  </div>
                  <div className="text-[10px] text-zinc-400">{hasVerifiedData ? `~${inst.funding_annualized_pct.toFixed(1)}% APR` : 'Unverified'}</div>
                </div>

                <div className="bg-zinc-800/40 p-2 rounded border border-zinc-800">
                  <div className="text-zinc-400 text-[10px]">Basis (Spot vs Perp)</div>
                  <div className="font-mono font-medium mt-0.5 text-zinc-200">
                    {hasVerifiedData ? `${inst.basis >= 0 ? '+' : ''}$${inst.basis.toFixed(1)}` : 'UNKNOWN'}
                  </div>
                  <div className="text-[10px] text-zinc-400">Z: {hasVerifiedData ? inst.basis_zscore.toFixed(2) : 'UNKNOWN'}</div>
                </div>

                <div className="bg-zinc-800/40 p-2 rounded border border-zinc-800">
                  <div className="text-zinc-400 text-[10px]">Open Interest</div>
                  <div className="font-mono font-medium mt-0.5 text-zinc-200">
                    {hasVerifiedData ? `$${(inst.open_interest_usd / 1e9).toFixed(2)}B` : 'UNKNOWN'}
                  </div>
                  <div className="text-[10px] text-emerald-400 font-mono">{hasVerifiedData ? `${inst.open_interest_delta_24h_pct >= 0 ? '+' : ''}${inst.open_interest_delta_24h_pct}% 24h` : 'Unverified'}</div>
                </div>

                <div className="bg-zinc-800/40 p-2 rounded border border-zinc-800">
                  <div className="text-zinc-400 text-[10px]">ATR (1h) / Vol</div>
                  <div className="font-mono font-medium mt-0.5 text-zinc-200">
                    {hasVerifiedData ? `$${inst.atr_1h.toFixed(1)}` : 'UNKNOWN'}
                  </div>
                  <div className="text-[10px] text-zinc-400">{hasVerifiedData ? `${inst.realized_vol_24h_pct}% Ann.` : 'Unverified'}</div>
                </div>
              </div>

              {/* Regime Probability Distribution Bar */}
              <div className="space-y-1.5 pt-1">
                <div className="flex items-center justify-between text-[11px] text-zinc-400">
                  <span>Regime Distribution</span>
                  <span>P(Range/MeanRev): {hasVerifiedData ? `${((inst.regime_probabilities.R0_STRONG_MEAN_REVERSION + inst.regime_probabilities.R1_RANGE) * 100).toFixed(0)}%` : 'UNKNOWN'}</span>
                </div>
                <div className="w-full h-2 rounded-full bg-zinc-800 flex overflow-hidden">
                <div style={{ width: hasVerifiedData ? `${inst.regime_probabilities.R0_STRONG_MEAN_REVERSION * 100}%` : '0%' }} className="bg-cyan-500" title="R0 Mean Reversion" />
                  <div style={{ width: hasVerifiedData ? `${inst.regime_probabilities.R1_RANGE * 100}%` : '0%' }} className="bg-emerald-500" title="R1 Range" />
                  <div style={{ width: hasVerifiedData ? `${inst.regime_probabilities.R2_WEAK_TREND * 100}%` : '0%' }} className="bg-amber-500" title="R2 Weak Trend" />
                  <div style={{ width: hasVerifiedData ? `${inst.regime_probabilities.R3_STRONG_TREND * 100}%` : '0%' }} className="bg-orange-500" title="R3 Strong Trend" />
                  <div style={{ width: hasVerifiedData ? `${inst.regime_probabilities.R4_BREAKOUT * 100}%` : '0%' }} className="bg-purple-500" title="R4 Breakout" />
                  <div style={{ width: hasVerifiedData ? `${(inst.regime_probabilities.R5_VOLATILITY_SHOCK + inst.regime_probabilities.R6_CRISIS) * 100}%` : '0%' }} className="bg-rose-500" title="Shock/Crisis" />
                </div>
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
};
