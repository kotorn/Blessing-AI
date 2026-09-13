import React from 'react';
import {
  Zap,
  Activity,
  AlertTriangle,
  Flame,
  ArrowUpRight,
  ArrowDownRight,
  ShieldAlert,
  CheckCircle2,
  Info,
} from 'lucide-react';
import { HorizonShockData, ShockHorizon, ShockState } from '../types/market';
import { InstrumentData } from '../types';

interface MultiHorizonShockMonitorProps {
  instrument: InstrumentData | undefined;
  symbol: string;
}

export const MultiHorizonShockMonitor: React.FC<MultiHorizonShockMonitorProps> = ({
  instrument,
  symbol,
}) => {
  const hasVerifiedData =
    instrument?.verified === true &&
    instrument.data_source === 'BINANCE_TESTNET';
  // InstrumentData currently carries no verified multi-horizon velocity,
  // acceleration, or percentile series. Do not synthesize those values in the
  // presentation layer; the worker/market-data service must supply them.
  const horizons: HorizonShockData[] = [];

  const highestPercentile = horizons.length > 0 ? Math.max(...horizons.map((h) => h.rollingPercentile)) : null;
  const shockState: ShockState | 'UNKNOWN' = !hasVerifiedData || highestPercentile == null
    ? 'UNKNOWN'
    : highestPercentile > 98
      ? 'SHOCK_EXPANSION'
      : highestPercentile > 95
        ? 'LIQUIDITY_SWEEP'
        : highestPercentile > 85
          ? 'ELEVATED'
          : 'NORMAL';

  return (
    <div className="bg-zinc-900/90 border border-zinc-800 rounded-2xl p-4 sm:p-5 space-y-4 shadow-xl shadow-black/20">
      {/* Header */}
      <div className="flex flex-wrap items-center justify-between gap-2 border-b border-zinc-800/80 pb-3">
        <div className="flex items-center space-x-2">
          <div className="p-1.5 rounded-lg bg-rose-950/80 border border-rose-800/60 text-rose-400">
            <Zap className="w-4 h-4" />
          </div>
          <div>
            <h3 className="text-xs font-bold text-zinc-100 uppercase tracking-wider">
              Multi-Horizon Shock Velocity & Acceleration ({symbol})
            </h3>
            <p className="text-[11px] text-zinc-400">
              Normalized rolling percentile distributions (No static dollar thresholds)
            </p>
          </div>
        </div>

        <div className="flex items-center space-x-2">
          <span className="text-[10px] font-mono text-zinc-500">Shock State:</span>
          <span
            className={`px-2 py-0.5 rounded text-[10px] font-bold font-mono border ${
              shockState === 'NORMAL'
                ? 'bg-emerald-950 text-emerald-300 border-emerald-800/80'
                : shockState === 'UNKNOWN'
                ? 'bg-amber-950 text-amber-300 border-amber-800/80'
                : shockState === 'ELEVATED'
                ? 'bg-amber-950 text-amber-300 border-amber-800/80'
                : 'bg-rose-950 text-rose-300 border-rose-800'
            }`}
          >
            {shockState}
          </span>
        </div>
      </div>

      {/* 4 Horizons Grid */}
      <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-3 text-xs">
        {horizons.length === 0 && (
          <div className="sm:col-span-2 lg:col-span-4 p-5 rounded-xl border border-amber-900/60 bg-amber-950/20 text-center text-amber-300">
            No verified multi-horizon shock snapshot is available for {symbol}.
          </div>
        )}
        {horizons.map((h) => {
          const isElevated = h.rollingPercentile >= 90;
          return (
            <div
              key={h.horizon}
              className={`p-3 rounded-xl border bg-zinc-950/70 space-y-2 transition-all ${
                isElevated ? 'border-amber-700/80' : 'border-zinc-800/80'
              }`}
            >
              {/* Top Horizon Tag */}
              <div className="flex items-center justify-between font-mono">
                <span className="text-[11px] font-bold text-cyan-400 bg-cyan-950/60 border border-cyan-800/60 px-1.5 py-0.5 rounded">
                  Δt = {h.horizon}
                </span>
                <span
                  className={`text-[10px] font-semibold ${
                    isElevated ? 'text-amber-400' : 'text-zinc-400'
                  }`}
                >
                  p{h.rollingPercentile.toFixed(1)} percentile
                </span>
              </div>

              {/* Velocity & Acceleration */}
              <div className="space-y-1">
                <div className="flex justify-between text-[11px]">
                  <span className="text-zinc-500">Velocity (v):</span>
                  <span className="font-mono text-zinc-200 font-medium">
                    {h.velocityPctPerSec >= 0 ? '+' : ''}
                    {(h.velocityPctPerSec * 100).toFixed(2)}%/s
                  </span>
                </div>
                <div className="flex justify-between text-[11px]">
                  <span className="text-zinc-500">Acceleration (a):</span>
                  <span className="font-mono text-zinc-200 font-medium">
                    {h.accelerationPctPerSec2 >= 0 ? '+' : ''}
                    {(h.accelerationPctPerSec2 * 100).toFixed(3)}%/s²
                  </span>
                </div>
                <div className="flex justify-between text-[11px]">
                  <span className="text-zinc-500">Displacement:</span>
                  <span className="font-mono text-zinc-200 font-medium">
                    {h.displacementBps} bps
                  </span>
                </div>
              </div>

              {/* Normalized Z-Score Bar */}
              <div className="space-y-1 pt-1 border-t border-zinc-800/60">
                <div className="flex justify-between text-[10px] font-mono">
                  <span className="text-zinc-500">Rolling Z-Score:</span>
                  <span className={isElevated ? 'text-amber-300 font-bold' : 'text-emerald-400'}>
                    +{h.zScore.toFixed(2)}σ
                  </span>
                </div>
                <div className="w-full h-1 bg-zinc-800 rounded-full overflow-hidden">
                  <div
                    className={`h-full ${
                      isElevated ? 'bg-amber-500' : 'bg-emerald-500'
                    }`}
                    style={{ width: `${Math.min(100, (h.zScore / 3.0) * 100)}%` }}
                  />
                </div>
              </div>
            </div>
          );
        })}
      </div>

      {/* Scientific Principle Disclosure */}
      <div className="p-3 bg-zinc-950 rounded-xl border border-zinc-800/80 flex items-start space-x-2.5 text-xs text-zinc-400">
        <Info className="w-4 h-4 text-cyan-400 shrink-0 mt-0.5" />
        <div>
          <span className="font-semibold text-zinc-200 block">
            Adaptive Volatility Normalization Invariant:
          </span>
          <span>
            Shock thresholds are strictly normalized by instantaneous rolling volatility percentiles (sigma_roll). Static point thresholds (e.g. $1,000 moves) are forbidden to ensure consistent risk budgeting across volatility regimes.
          </span>
        </div>
      </div>
    </div>
  );
};
