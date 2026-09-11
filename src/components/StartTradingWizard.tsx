import React, { useState, useEffect } from 'react';
import {
  Rocket,
  CheckCircle2,
  XCircle,
  AlertTriangle,
  Server,
  Activity,
  ArrowRight,
  ShieldAlert,
  ChevronRight,
  Play,
} from 'lucide-react';
import { PreflightResult, PreflightCheck } from '../types';
import { quantApi } from '../api/quant';

interface StartTradingWizardProps {
  onComplete: (params: any) => Promise<void>;
  onCancel: () => void;
}

export const StartTradingWizard: React.FC<StartTradingWizardProps> = ({
  onComplete,
  onCancel,
}) => {
  const [step, setStep] = useState<number>(1);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Form State
  const [executionMode, setExecutionMode] = useState<'PAPER' | 'TESTNET' | 'LIVE'>('PAPER');
  const [instruments, setInstruments] = useState({ BTCUSDT: true, ETHUSDT: true });
  const [strategies, setStrategies] = useState({
    grid: true,
    trend: true,
    shock: false,
    carry: false,
  });
  const [riskProfile, setRiskProfile] = useState<'CONSERVATIVE' | 'BALANCED' | 'AGGRESSIVE'>('BALANCED');
  const [riskAcknowledged, setRiskAcknowledged] = useState(false);

  // Preflight State
  const [preflightResult, setPreflightResult] = useState<PreflightResult | null>(null);

  useEffect(() => {
    if (step === 3) {
      runPreflight();
    }
  }, [step, executionMode]);

  const runPreflight = async () => {
    setLoading(true);
    setError(null);
    try {
      const result = await quantApi.preflight(executionMode);
      setPreflightResult(result);
    } catch (err) {
      setError('Failed to run system preflight checks.');
    } finally {
      setLoading(false);
    }
  };

  const handleArm = async () => {
    if (!preflightResult?.canArm) return;
    setLoading(true);
    try {
      await onComplete({
        executionMode,
        instruments: Object.keys(instruments).filter((k) => (instruments as any)[k]),
        strategies,
        riskProfile,
      });
    } catch (err: any) {
      setError(err.message || 'Failed to arm engine.');
      setLoading(false);
    }
  };

  const renderStep1 = () => (
    <div className="space-y-5 animate-fadeIn">
      <div>
        <h3 className="text-sm font-bold text-zinc-100">1. Execution Mode & Assets</h3>
        <p className="text-[11px] text-zinc-400">Select the execution environment and target instruments.</p>
      </div>

      <div className="space-y-3">
        <label className="block text-xs font-semibold text-zinc-300">Execution Mode</label>
        <div className="grid grid-cols-3 gap-3">
          {(['PAPER', 'TESTNET', 'LIVE'] as const).map((mode) => (
            <button
              key={mode}
              onClick={() => setExecutionMode(mode)}
              className={`p-3 border rounded-xl text-left transition-all ${
                executionMode === mode
                  ? 'bg-indigo-900/30 border-indigo-500 text-indigo-300'
                  : 'bg-zinc-900 border-zinc-800 text-zinc-500 hover:border-zinc-700'
              }`}
            >
              <div className="font-bold text-sm">{mode}</div>
              <div className="text-[10px] mt-1 opacity-80">
                {mode === 'PAPER' ? 'Simulated execution' : mode === 'TESTNET' ? 'Binance Testnet' : 'Real capital risk'}
              </div>
            </button>
          ))}
        </div>
      </div>

      <div className="space-y-3">
        <label className="block text-xs font-semibold text-zinc-300">Target Instruments</label>
        <div className="grid grid-cols-2 gap-3">
          {Object.entries(instruments).map(([symbol, active]) => (
            <button
              key={symbol}
              onClick={() => setInstruments({ ...instruments, [symbol]: !active })}
              className={`p-3 border rounded-xl flex items-center justify-between transition-all ${
                active
                  ? 'bg-emerald-950/20 border-emerald-800 text-emerald-400'
                  : 'bg-zinc-900 border-zinc-800 text-zinc-500'
              }`}
            >
              <span className="font-bold font-mono text-xs">{symbol}</span>
              {active && <CheckCircle2 className="w-4 h-4" />}
            </button>
          ))}
        </div>
      </div>

      <div className="flex justify-end pt-4">
        <button
          onClick={() => setStep(2)}
          className="px-4 py-2 bg-indigo-600 hover:bg-indigo-500 text-white rounded-lg text-xs font-bold transition-colors"
        >
          Next: Strategies
        </button>
      </div>
    </div>
  );

  const renderStep2 = () => (
    <div className="space-y-5 animate-fadeIn">
      <div>
        <h3 className="text-sm font-bold text-zinc-100">2. Alpha Engines & Risk Profile</h3>
        <p className="text-[11px] text-zinc-400">Configure which strategies are authorized to deploy capital.</p>
      </div>

      <div className="space-y-3">
        <label className="block text-xs font-semibold text-zinc-300">Active Alpha Engines</label>
        <div className="grid grid-cols-2 gap-3">
          {Object.entries(strategies).map(([strat, active]) => (
            <button
              key={strat}
              onClick={() => setStrategies({ ...strategies, [strat]: !active })}
              className={`p-3 border rounded-xl flex items-center justify-between transition-all ${
                active
                  ? 'bg-cyan-950/30 border-cyan-800 text-cyan-400'
                  : 'bg-zinc-900 border-zinc-800 text-zinc-500'
              }`}
            >
              <span className="font-bold text-xs capitalize">{strat}</span>
              {active && <CheckCircle2 className="w-4 h-4" />}
            </button>
          ))}
        </div>
      </div>

      <div className="space-y-3">
        <label className="block text-xs font-semibold text-zinc-300">Portfolio Risk Governor Profile</label>
        <div className="grid grid-cols-3 gap-3">
          {(['CONSERVATIVE', 'BALANCED', 'AGGRESSIVE'] as const).map((profile) => (
            <button
              key={profile}
              onClick={() => setRiskProfile(profile)}
              className={`p-3 border rounded-xl text-left transition-all ${
                riskProfile === profile
                  ? 'bg-amber-950/30 border-amber-500 text-amber-300'
                  : 'bg-zinc-900 border-zinc-800 text-zinc-500 hover:border-zinc-700'
              }`}
            >
              <div className="font-bold text-sm">{profile}</div>
            </button>
          ))}
        </div>
      </div>

      <div className="flex justify-between pt-4">
        <button
          onClick={() => setStep(1)}
          className="px-4 py-2 bg-zinc-800 hover:bg-zinc-700 text-zinc-300 rounded-lg text-xs font-bold transition-colors"
        >
          Back
        </button>
        <button
          onClick={() => setStep(3)}
          className="px-4 py-2 bg-indigo-600 hover:bg-indigo-500 text-white rounded-lg text-xs font-bold transition-colors"
        >
          Next: Preflight
        </button>
      </div>
    </div>
  );

  const renderStep3 = () => (
    <div className="space-y-5 animate-fadeIn">
      <div>
        <h3 className="text-sm font-bold text-zinc-100 flex items-center justify-between">
          <span>3. Preflight Truth & Safety Boundary</span>
          {loading && <Activity className="w-4 h-4 animate-spin text-cyan-400" />}
        </h3>
        <p className="text-[11px] text-zinc-400">Verifying system capabilities for {executionMode} mode.</p>
      </div>

      <div className="bg-zinc-950 border border-zinc-800 rounded-xl p-3 space-y-2">
        {preflightResult?.checks.map((check) => (
          <div key={check.id} className="flex items-start gap-3 p-2 border-b border-zinc-800/50 last:border-0">
            <div className="mt-0.5">
              {check.status === 'PASS' ? (
                <CheckCircle2 className="w-4 h-4 text-emerald-400" />
              ) : check.status === 'WARN' ? (
                <AlertTriangle className="w-4 h-4 text-amber-400" />
              ) : check.status === 'UNKNOWN' ? (
                <CheckCircle2 className="w-4 h-4 text-zinc-600" />
              ) : (
                <XCircle className="w-4 h-4 text-rose-400" />
              )}
            </div>
            <div>
              <div className="text-xs font-bold text-zinc-200">{check.name}</div>
              <div className="text-[10px] text-zinc-500">{check.message}</div>
            </div>
          </div>
        ))}
      </div>

      {error && (
        <div className="p-3 bg-rose-950/30 border border-rose-900 rounded-lg text-rose-400 text-xs">
          {error}
        </div>
      )}

      {preflightResult?.canArm === false && (
        <div className="p-3 bg-amber-950/30 border border-amber-900 rounded-lg flex gap-3">
          <ShieldAlert className="w-5 h-5 text-amber-400 shrink-0" />
          <div className="text-xs text-amber-300">
            <strong>Preflight Failed.</strong> The system cannot be armed in this configuration. Please resolve the failing checks above or select a different execution mode.
          </div>
        </div>
      )}

      {preflightResult?.canArm && executionMode === 'LIVE' && (
        <label className="flex items-start space-x-3 p-3 bg-rose-950/20 border border-rose-900/50 rounded-xl cursor-pointer">
          <input
            type="checkbox"
            checked={riskAcknowledged}
            onChange={(e) => setRiskAcknowledged(e.target.checked)}
            className="mt-1 bg-zinc-900 border-zinc-700 rounded text-rose-600 focus:ring-rose-500"
          />
          <div className="text-xs text-zinc-300">
            <span className="font-bold text-rose-400 block mb-0.5">I acknowledge the risk of LIVE execution.</span>
            This will authorize the engine to place real orders with real capital. I accept full responsibility for any financial losses.
          </div>
        </label>
      )}

      <div className="flex justify-between pt-4">
        <button
          onClick={() => setStep(2)}
          className="px-4 py-2 bg-zinc-800 hover:bg-zinc-700 text-zinc-300 rounded-lg text-xs font-bold transition-colors"
        >
          Back
        </button>
        <button
          onClick={handleArm}
          disabled={loading || !preflightResult?.canArm || (executionMode === 'LIVE' && !riskAcknowledged)}
          className="flex items-center space-x-2 px-5 py-2 bg-emerald-600 hover:bg-emerald-500 disabled:opacity-50 text-white rounded-lg text-xs font-bold transition-colors"
        >
          <Play className="w-4 h-4" />
          <span>ARM ENGINE ({executionMode})</span>
        </button>
      </div>
    </div>
  );

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center p-4 bg-black/60 backdrop-blur-sm animate-fadeIn">
      <div className="bg-zinc-900 border border-zinc-800 rounded-2xl w-full max-w-xl overflow-hidden shadow-2xl">
        <div className="p-4 border-b border-zinc-800 bg-zinc-950 flex items-center justify-between">
          <div className="flex items-center space-x-2">
            <div className="p-1.5 rounded-lg bg-indigo-900 text-indigo-300">
              <Rocket className="w-4 h-4" />
            </div>
            <h2 className="text-sm font-bold text-zinc-100 uppercase tracking-wider">Start Trading Wizard</h2>
          </div>
          <button onClick={onCancel} className="text-zinc-500 hover:text-zinc-300">
            <XCircle className="w-5 h-5" />
          </button>
        </div>

        <div className="flex border-b border-zinc-800">
          {[1, 2, 3].map((s) => (
            <div
              key={s}
              className={`flex-1 text-center py-2 text-[10px] font-bold uppercase tracking-wider ${
                step === s ? 'bg-indigo-950/30 text-indigo-400 border-b-2 border-indigo-500' : 'text-zinc-600'
              }`}
            >
              Step {s}
            </div>
          ))}
        </div>

        <div className="p-6">
          {step === 1 && renderStep1()}
          {step === 2 && renderStep2()}
          {step === 3 && renderStep3()}
        </div>
      </div>
    </div>
  );
};
