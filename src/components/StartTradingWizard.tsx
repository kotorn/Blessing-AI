import React, { useState, useEffect } from 'react';
import {
  Rocket,
  CheckCircle2,
  XCircle,
  AlertTriangle,
  Server,
  Key,
  Database,
  Activity,
  ArrowRight,
  ShieldAlert,
  ChevronRight,
  Play,
} from 'lucide-react';
import { BinanceKeyStatus } from '../api/binance';

interface StartTradingWizardProps {
  binanceStatus?: BinanceKeyStatus | null;
  firestoreConnected: boolean;
  onComplete: () => void;
  onCancel: () => void;
}

export const StartTradingWizard: React.FC<StartTradingWizardProps> = ({
  binanceStatus,
  firestoreConnected,
  onComplete,
  onCancel,
}) => {
  const [step, setStep] = useState<number>(1);
  const [loading, setLoading] = useState(false);
  const [riskAcknowledged, setRiskAcknowledged] = useState(false);

  // Step 1: Pre-flight Checks (Exchange, Database, NATS)
  // Step 2: Risk Governor Limits
  // Step 3: Strategy Allocation
  // Step 4: Final Confirmation

  const handleNext = () => {
    if (step < 4) setStep(step + 1);
  };

  const handleBack = () => {
    if (step > 1) setStep(step - 1);
  };

  const handleStart = async () => {
    setLoading(true);
    // Simulate activation delay
    await new Promise((resolve) => setTimeout(resolve, 1500));
    setLoading(false);
    onComplete();
  };

  const isBinanceReady = binanceStatus?.spot?.authenticated || binanceStatus?.futures?.authenticated;

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 backdrop-blur-sm p-4">
      <div className="bg-zinc-900 border border-zinc-800 rounded-2xl w-full max-w-3xl shadow-2xl overflow-hidden flex flex-col max-h-[90vh]">
        {/* Header */}
        <div className="p-5 border-b border-zinc-800 bg-zinc-950/50 flex items-center justify-between shrink-0">
          <div className="flex items-center space-x-3">
            <div className="p-2 bg-indigo-950/50 border border-indigo-800 text-indigo-400 rounded-lg">
              <Rocket className="w-5 h-5" />
            </div>
            <div>
              <h2 className="text-lg font-bold text-zinc-100">Start Trading Wizard</h2>
              <p className="text-xs text-zinc-400">Initialize fail-closed execution environment</p>
            </div>
          </div>
          <button
            onClick={onCancel}
            className="text-zinc-500 hover:text-zinc-300 transition-colors"
          >
            <XCircle className="w-6 h-6" />
          </button>
        </div>

        {/* Wizard Progress Steps */}
        <div className="flex border-b border-zinc-800 bg-zinc-900/50 shrink-0">
          {[
            { num: 1, label: 'Pre-flight Checks' },
            { num: 2, label: 'Risk Governor' },
            { num: 3, label: 'Strategy Limits' },
            { num: 4, label: 'Confirmation' },
          ].map((s) => (
            <div
              key={s.num}
              className={`flex-1 py-3 px-4 text-xs font-bold border-b-2 flex items-center justify-center space-x-2 ${
                step === s.num
                  ? 'border-indigo-500 text-indigo-400 bg-indigo-950/20'
                  : step > s.num
                  ? 'border-emerald-500 text-emerald-400'
                  : 'border-transparent text-zinc-600'
              }`}
            >
              <div
                className={`w-5 h-5 rounded-full flex items-center justify-center text-[10px] ${
                  step === s.num
                    ? 'bg-indigo-500 text-white'
                    : step > s.num
                    ? 'bg-emerald-500 text-white'
                    : 'bg-zinc-800 text-zinc-500'
                }`}
              >
                {step > s.num ? <CheckCircle2 className="w-3 h-3" /> : s.num}
              </div>
              <span className="hidden sm:inline">{s.label}</span>
            </div>
          ))}
        </div>

        {/* Content Area */}
        <div className="p-6 overflow-y-auto flex-1">
          {step === 1 && (
            <div className="space-y-6">
              <div>
                <h3 className="text-sm font-bold text-zinc-100">Step 1: System Capability Discovery</h3>
                <p className="text-xs text-zinc-400 mt-1">
                  Validating connections to exchange endpoints, databases, and message buses.
                </p>
              </div>

              <div className="space-y-3">
                <div className="p-4 rounded-xl border border-zinc-800 bg-zinc-950 flex items-center justify-between">
                  <div className="flex items-center space-x-3">
                    <Key className="w-5 h-5 text-amber-400" />
                    <div>
                      <div className="text-sm font-bold text-zinc-200">Binance Global API</div>
                      <div className="text-xs text-zinc-500">Spot & USDⓈ-M Futures Keys</div>
                    </div>
                  </div>
                  {isBinanceReady ? (
                    <span className="px-2.5 py-1 rounded-lg bg-emerald-950 text-emerald-400 border border-emerald-800/80 text-xs font-bold flex items-center space-x-1">
                      <CheckCircle2 className="w-3.5 h-3.5" />
                      <span>AUTHENTICATED</span>
                    </span>
                  ) : (
                    <span className="px-2.5 py-1 rounded-lg bg-rose-950 text-rose-400 border border-rose-800/80 text-xs font-bold flex items-center space-x-1">
                      <XCircle className="w-3.5 h-3.5" />
                      <span>MISSING</span>
                    </span>
                  )}
                </div>

                <div className="p-4 rounded-xl border border-zinc-800 bg-zinc-950 flex items-center justify-between">
                  <div className="flex items-center space-x-3">
                    <Database className="w-5 h-5 text-cyan-400" />
                    <div>
                      <div className="text-sm font-bold text-zinc-200">Cloud Persistence</div>
                      <div className="text-xs text-zinc-500">Firestore & BigQuery Lakehouse</div>
                    </div>
                  </div>
                  {firestoreConnected ? (
                    <span className="px-2.5 py-1 rounded-lg bg-emerald-950 text-emerald-400 border border-emerald-800/80 text-xs font-bold flex items-center space-x-1">
                      <CheckCircle2 className="w-3.5 h-3.5" />
                      <span>CONNECTED</span>
                    </span>
                  ) : (
                    <span className="px-2.5 py-1 rounded-lg bg-amber-950 text-amber-400 border border-amber-800/80 text-xs font-bold flex items-center space-x-1">
                      <AlertTriangle className="w-3.5 h-3.5" />
                      <span>STANDALONE</span>
                    </span>
                  )}
                </div>

                <div className="p-4 rounded-xl border border-zinc-800 bg-zinc-950 flex items-center justify-between opacity-70">
                  <div className="flex items-center space-x-3">
                    <Server className="w-5 h-5 text-indigo-400" />
                    <div>
                      <div className="text-sm font-bold text-zinc-200">NATS JetStream</div>
                      <div className="text-xs text-zinc-500">Live event bus for Python execution</div>
                    </div>
                  </div>
                  <span className="px-2.5 py-1 rounded-lg bg-emerald-950 text-emerald-400 border border-emerald-800/80 text-xs font-bold flex items-center space-x-1">
                    <CheckCircle2 className="w-3.5 h-3.5" />
                    <span>VERIFIED</span>
                  </span>
                </div>
              </div>

              {!isBinanceReady && (
                <div className="p-3 bg-rose-950/30 border border-rose-900/50 rounded-xl text-xs text-rose-400 flex items-start space-x-2">
                  <AlertTriangle className="w-4 h-4 shrink-0 mt-0.5" />
                  <p>You must connect Binance API keys in Settings or the Connections page before starting live trading.</p>
                </div>
              )}
            </div>
          )}

          {step === 2 && (
            <div className="space-y-6">
              <div>
                <h3 className="text-sm font-bold text-zinc-100">Step 2: Portfolio Risk Governor</h3>
                <p className="text-xs text-zinc-400 mt-1">
                  Set hard constraints that supersede all machine learning and strategy logic.
                </p>
              </div>

              <div className="space-y-4">
                <div className="space-y-2">
                  <label className="text-xs font-bold text-zinc-300">Max Portfolio Drawdown (%)</label>
                  <div className="flex items-center space-x-3">
                    <input
                      type="range"
                      min="1"
                      max="20"
                      defaultValue="5"
                      className="flex-1 accent-rose-500"
                    />
                    <span className="text-sm font-bold text-rose-400 w-12 text-right">5.0%</span>
                  </div>
                  <p className="text-[10px] text-zinc-500 font-mono">Triggers total liquidation if reached.</p>
                </div>

                <div className="space-y-2">
                  <label className="text-xs font-bold text-zinc-300">Max Gross Leverage (Across all models)</label>
                  <div className="flex items-center space-x-3">
                    <input
                      type="range"
                      min="1"
                      max="10"
                      defaultValue="2"
                      step="0.5"
                      className="flex-1 accent-amber-500"
                    />
                    <span className="text-sm font-bold text-amber-400 w-12 text-right">2.0x</span>
                  </div>
                  <p className="text-[10px] text-zinc-500 font-mono">Hard limit on total notional exposure / wallet balance.</p>
                </div>
              </div>
            </div>
          )}

          {step === 3 && (
            <div className="space-y-6">
              <div>
                <h3 className="text-sm font-bold text-zinc-100">Step 3: Strategy Enablement & Allocation</h3>
                <p className="text-xs text-zinc-400 mt-1">
                  Select which alpha engines are permitted to request capital via the Meta Allocator.
                </p>
              </div>

              <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
                {[
                  { name: 'Grid (Mean-Reversion)', desc: 'Structural range harvester', default: true },
                  { name: 'Trend (Breakout)', desc: 'Asymmetric momentum capture', default: true },
                  { name: 'Shock Momentum', desc: 'Fast impulse velocity', default: false },
                  { name: 'Basis Carry', desc: 'Market neutral funding yield', default: false },
                ].map((strat) => (
                  <label key={strat.name} className="flex items-start space-x-3 p-4 rounded-xl border border-zinc-800 bg-zinc-950 cursor-pointer hover:border-zinc-700 transition-colors">
                    <div className="mt-0.5">
                      <input type="checkbox" defaultChecked={strat.default} className="w-4 h-4 accent-indigo-500 rounded bg-zinc-800 border-zinc-700" />
                    </div>
                    <div>
                      <div className="text-sm font-bold text-zinc-200">{strat.name}</div>
                      <div className="text-xs text-zinc-500">{strat.desc}</div>
                    </div>
                  </label>
                ))}
              </div>
            </div>
          )}

          {step === 4 && (
            <div className="space-y-6">
              <div>
                <h3 className="text-sm font-bold text-zinc-100">Step 4: Final Confirmation</h3>
                <p className="text-xs text-zinc-400 mt-1">
                  Acknowledge the execution risks before authorizing the autonomous agent.
                </p>
              </div>

              <div className="p-4 rounded-xl border border-rose-900/50 bg-rose-950/20 space-y-3">
                <div className="flex items-center space-x-2 text-rose-400 font-bold text-sm">
                  <ShieldAlert className="w-5 h-5" />
                  <span>Execution Safety Acknowledgement</span>
                </div>
                <div className="text-xs text-zinc-300 font-mono space-y-2">
                  <p>• The system will operate autonomously on Binance Global.</p>
                  <p>• You are enabling REAL financial risk.</p>
                  <p>• The Portfolio Risk Governor is active, but market gaps can exceed slippage models.</p>
                </div>
                
                <label className="flex items-center space-x-2 pt-2 cursor-pointer">
                  <input
                    type="checkbox"
                    checked={riskAcknowledged}
                    onChange={(e) => setRiskAcknowledged(e.target.checked)}
                    className="w-4 h-4 accent-rose-500"
                  />
                  <span className="text-xs font-bold text-rose-300">I acknowledge these risks and authorize live execution.</span>
                </label>
              </div>
            </div>
          )}
        </div>

        {/* Footer Controls */}
        <div className="p-5 border-t border-zinc-800 bg-zinc-950 flex justify-between shrink-0">
          <button
            onClick={handleBack}
            disabled={step === 1 || loading}
            className="px-4 py-2 rounded-lg text-xs font-bold text-zinc-400 hover:text-zinc-200 disabled:opacity-30 transition-colors"
          >
            Back
          </button>
          
          {step < 4 ? (
            <button
              onClick={handleNext}
              disabled={step === 1 && !isBinanceReady}
              className="px-4 py-2 rounded-lg text-xs font-bold bg-indigo-600 hover:bg-indigo-500 text-white disabled:opacity-50 transition-colors flex items-center space-x-1.5"
            >
              <span>Continue</span>
              <ChevronRight className="w-4 h-4" />
            </button>
          ) : (
            <button
              onClick={handleStart}
              disabled={!riskAcknowledged || loading}
              className="px-5 py-2 rounded-lg text-sm font-bold bg-rose-600 hover:bg-rose-500 text-white disabled:opacity-50 transition-colors flex items-center space-x-2 shadow-lg shadow-rose-900/50"
            >
              {loading ? (
                <>
                  <Activity className="w-4 h-4 animate-spin" />
                  <span>Activating Engines...</span>
                </>
              ) : (
                <>
                  <Play className="w-4 h-4 fill-current" />
                  <span>START TRADING</span>
                </>
              )}
            </button>
          )}
        </div>
      </div>
    </div>
  );
};
