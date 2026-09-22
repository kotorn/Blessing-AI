import React, { useState, useEffect, useMemo } from 'react';
import {
  Rocket,
  CheckCircle2,
  XCircle,
  AlertTriangle,
  Activity,
  ShieldAlert,
  Play,
  Search,
  CheckSquare,
  Square,
  Sparkles,
  Coins,
} from 'lucide-react';
import { PreflightCheck, PreflightResult, TradingSystemState } from '../types';
import { quantApi } from '../api/quant';
import { useAuth } from '../context/AuthContext';
import { EvidenceStatus } from '../lib/evidence';
import {
  ControlPlaneRole,
  requiredRoleForExecutionMode,
  resolveControlPlaneRole,
  roleSatisfies,
} from '../lib/control-plane-role';
import { SUPPORTED_SYMBOLS_BY_MODE } from '../backend/system';

export interface InstrumentMeta {
  symbol: string;
  name: string;
  category: 'Major' | 'Layer 1' | 'DeFi' | 'Meme' | 'USDC';
  quoteAsset: 'USDT' | 'USDC';
  tag?: string;
  description: string;
}

export const INSTRUMENT_CATALOG: InstrumentMeta[] = [
  { symbol: 'BTCUSDT', name: 'Bitcoin', category: 'Major', quoteAsset: 'USDT', tag: 'Core', description: 'Bitcoin USDⓈ-M Perpetual' },
  { symbol: 'ETHUSDT', name: 'Ethereum', category: 'Major', quoteAsset: 'USDT', tag: 'Core', description: 'Ethereum USDⓈ-M Perpetual' },
  { symbol: 'SOLUSDT', name: 'Solana', category: 'Layer 1', quoteAsset: 'USDT', tag: 'High Beta', description: 'Solana USDⓈ-M Perpetual' },
  { symbol: 'BNBUSDT', name: 'BNB', category: 'Layer 1', quoteAsset: 'USDT', tag: 'Ecosystem', description: 'BNB USDⓈ-M Perpetual' },
  { symbol: 'XRPUSDT', name: 'Ripple', category: 'Major', quoteAsset: 'USDT', tag: 'High Volume', description: 'Ripple USDⓈ-M Perpetual' },
  { symbol: 'DOGEUSDT', name: 'Dogecoin', category: 'Meme', quoteAsset: 'USDT', tag: 'Meme Core', description: 'Dogecoin USDⓈ-M Perpetual' },
  { symbol: 'ADAUSDT', name: 'Cardano', category: 'Layer 1', quoteAsset: 'USDT', tag: 'PoS Layer 1', description: 'Cardano USDⓈ-M Perpetual' },
  { symbol: 'AVAXUSDT', name: 'Avalanche', category: 'Layer 1', quoteAsset: 'USDT', tag: 'Subnets', description: 'Avalanche USDⓈ-M Perpetual' },
  { symbol: 'SUIUSDT', name: 'Sui', category: 'Layer 1', quoteAsset: 'USDT', tag: 'Move L1', description: 'Sui USDⓈ-M Perpetual' },
  { symbol: 'NEARUSDT', name: 'Near Protocol', category: 'Layer 1', quoteAsset: 'USDT', tag: 'Sharded L1', description: 'Near USDⓈ-M Perpetual' },
  { symbol: 'LINKUSDT', name: 'Chainlink', category: 'DeFi', quoteAsset: 'USDT', tag: 'Oracle / DeFi', description: 'Chainlink USDⓈ-M Perpetual' },
  { symbol: 'ETHUSDC', name: 'Ethereum (USDC)', category: 'USDC', quoteAsset: 'USDC', tag: 'Pilot Active', description: 'Ethereum USDC-Margined Perpetual' },
  { symbol: 'BTCUSDC', name: 'Bitcoin (USDC)', category: 'USDC', quoteAsset: 'USDC', tag: 'USDC Collateral', description: 'Bitcoin USDC-Margined Perpetual' },
];

const CATEGORIES = ['ALL', 'Major', 'Layer 1', 'DeFi', 'Meme', 'USDC'] as const;

interface StartTradingWizardProps {
  onComplete: (params: any) => Promise<void>;
  onCancel: () => void;
  systemState: TradingSystemState | null;
  evidenceStatus: EvidenceStatus;
}

export const StartTradingWizard: React.FC<StartTradingWizardProps> = ({
  onComplete,
  onCancel,
  systemState,
  evidenceStatus,
}) => {
  const { user } = useAuth();
  const [step, setStep] = useState<number>(1);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Form State
  const [executionMode, setExecutionMode] = useState<'PAPER' | 'TESTNET' | 'LIVE'>('PAPER');
  const [instruments, setInstruments] = useState<Record<string, boolean>>({ BTCUSDT: true, ETHUSDT: true });
  const [selectedCategory, setSelectedCategory] = useState<string>('ALL');
  const [searchQuery, setSearchQuery] = useState<string>('');

  const [strategies, setStrategies] = useState({
    grid: true,
    trend: true,
    shock: false,
    carry: false,
  });
  const [riskProfile, setRiskProfile] = useState<'CONSERVATIVE' | 'BALANCED' | 'AGGRESSIVE'>('BALANCED');

  // Preflight State
  const [preflightResult, setPreflightResult] = useState<PreflightResult | null>(null);
  const [controlPlaneRole, setControlPlaneRole] = useState<ControlPlaneRole>('unknown');
  const [roleLoading, setRoleLoading] = useState(false);

  useEffect(() => {
    let active = true;
    setRoleLoading(Boolean(user));
    void resolveControlPlaneRole(user).then((role) => {
      if (active) {
        setControlPlaneRole(role);
        setRoleLoading(false);
      }
    });
    return () => {
      active = false;
    };
  }, [user]);

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
    } catch {
      setError('Failed to run system preflight checks.');
    } finally {
      setLoading(false);
    }
  };

  const handleArm = async () => {
    if (!canArm) return;
    setLoading(true);
    try {
      await onComplete({
        executionMode,
        instruments: Object.keys(instruments).filter((k) => (instruments as any)[k]),
        strategies,
        riskProfile,
        enforcePreflight: true,
      });
    } catch (err: any) {
      setError(err.message || 'Failed to arm engine.');
      setLoading(false);
    }
  };

  // Supported symbols in current mode
  const currentModeSupportedSymbols = useMemo(() => {
    return new Set<string>(SUPPORTED_SYMBOLS_BY_MODE[executionMode] || SUPPORTED_SYMBOLS_BY_MODE.PAPER);
  }, [executionMode]);

  const handleModeChange = (mode: 'PAPER' | 'TESTNET' | 'LIVE') => {
    setExecutionMode(mode);
    const supported = new Set<string>(SUPPORTED_SYMBOLS_BY_MODE[mode] || SUPPORTED_SYMBOLS_BY_MODE.PAPER);
    // Retain selected symbols that are valid in the target mode
    const currentActive = Object.keys(instruments).filter((sym) => instruments[sym] && supported.has(sym));
    if (currentActive.length > 0) {
      const next: Record<string, boolean> = {};
      for (const sym of currentActive) {
        next[sym] = true;
      }
      setInstruments(next);
    } else {
      if (mode === 'LIVE') {
        setInstruments({ ETHUSDC: true });
      } else {
        setInstruments({ BTCUSDT: true, ETHUSDT: true });
      }
    }
  };

  const toggleInstrument = (symbol: string) => {
    setInstruments((prev) => ({
      ...prev,
      [symbol]: !prev[symbol],
    }));
  };

  const selectAllFiltered = () => {
    const next = { ...instruments };
    for (const inst of filteredInstruments) {
      if (currentModeSupportedSymbols.has(inst.symbol)) {
        next[inst.symbol] = true;
      }
    }
    setInstruments(next);
  };

  const clearAllInstruments = () => {
    setInstruments({});
  };

  const selectCoreInstruments = () => {
    if (executionMode === 'LIVE') {
      setInstruments({ ETHUSDC: true, BTCUSDC: true });
    } else {
      setInstruments({ BTCUSDT: true, ETHUSDT: true, SOLUSDT: true });
    }
  };

  const filteredInstruments = useMemo(() => {
    return INSTRUMENT_CATALOG.filter((inst) => {
      // Category filter
      if (selectedCategory !== 'ALL' && inst.category !== selectedCategory) {
        return false;
      }
      // Search query
      if (searchQuery.trim()) {
        const query = searchQuery.trim().toLowerCase();
        const matchesSymbol = inst.symbol.toLowerCase().includes(query);
        const matchesName = inst.name.toLowerCase().includes(query);
        const matchesTag = inst.tag?.toLowerCase().includes(query);
        if (!matchesSymbol && !matchesName && !matchesTag) {
          return false;
        }
      }
      return true;
    });
  }, [selectedCategory, searchQuery]);

  const activeInstrumentsList = useMemo(() => {
    return Object.keys(instruments).filter((k) => instruments[k] && currentModeSupportedSymbols.has(k));
  }, [instruments, currentModeSupportedSymbols]);

  const persistenceCheck = preflightResult?.checks.find((check) =>
    check.id.toUpperCase().includes('PERSISTENCE') || check.name.toUpperCase().includes('PERSISTENCE'),
  );
  const requiredRole = requiredRoleForExecutionMode(executionMode);
  const localReadinessChecks: PreflightCheck[] = [
    {
      id: 'CHK-AUTHENTICATION',
      name: 'Authentication',
      required: true,
      status: user ? 'PASS' : 'FAIL',
      message: user ? `Signed in as ${user.email || user.uid}` : 'Sign in is required before changing system state',
    },
    {
      id: 'CHK-ROLE',
      name: 'Role',
      required: true,
      status: roleLoading ? 'UNKNOWN' : roleSatisfies(controlPlaneRole, requiredRole) ? 'PASS' : 'FAIL',
      message: roleLoading
        ? 'Reading verified Firebase custom claims'
        : controlPlaneRole === 'unknown'
          ? `A verified ${requiredRole} role is required`
          : `${controlPlaneRole} role; ${requiredRole} is required for ${executionMode}`,
    },
    {
      id: 'CHK-WORKER-READINESS',
      name: 'Worker heartbeat',
      required: true,
      status: systemState?.workerResponsive === true && evidenceStatus !== 'STALE' && evidenceStatus !== 'UNAVAILABLE'
        ? 'PASS'
        : 'FAIL',
      message: systemState?.workerResponsive === true && evidenceStatus !== 'STALE' && evidenceStatus !== 'UNAVAILABLE'
        ? `Fresh authoritative worker state (${evidenceStatus})`
        : 'Worker state is unavailable or stale; execution is blocked',
    },
    {
      id: 'CHK-PERSISTENCE-UI',
      name: 'Persistence',
      required: persistenceCheck ? persistenceCheck.required : true,
      status: persistenceCheck?.status || 'UNKNOWN',
      message: persistenceCheck?.message || 'Worker did not expose a persistence check; execution is blocked',
    },
  ];

  const combinedChecks = [...localReadinessChecks, ...(preflightResult?.checks || [])];
  const canArm = Boolean(preflightResult?.canArm) && localReadinessChecks.every((check) => !check.required || check.status === 'PASS');

  const renderCheckIcon = (status: PreflightCheck['status']) => {
    if (status === 'PASS') return <CheckCircle2 className="w-4 h-4 text-emerald-400" />;
    if (status === 'WARN' || status === 'UNKNOWN') return <AlertTriangle className="w-4 h-4 text-amber-400" />;
    return <XCircle className="w-4 h-4 text-rose-400" />;
  };

  const renderStep1 = () => (
    <div className="space-y-4 animate-fadeIn">
      <div>
        <h3 className="text-sm font-bold text-zinc-100 flex items-center justify-between">
          <span>1. Execution Mode & Tradable Universe</span>
          <span className="text-[11px] font-mono text-cyan-400 bg-cyan-950/40 px-2 py-0.5 rounded border border-cyan-800/60">
            {activeInstrumentsList.length} Selected
          </span>
        </h3>
        <p className="text-[11px] text-zinc-400">Select execution environment and authorize target coin pairs for systematic execution.</p>
      </div>

      {/* Execution Mode Selector */}
      <div className="space-y-2">
        <label className="block text-xs font-semibold text-zinc-300">Execution Mode</label>
        <div className="grid grid-cols-3 gap-2.5">
          {(['PAPER', 'TESTNET', 'LIVE'] as const).map((mode) => (
            <button
              key={mode}
              type="button"
              onClick={() => handleModeChange(mode)}
              className={`p-3 border rounded-xl text-left transition-all relative cursor-pointer ${
                executionMode === mode
                  ? mode === 'LIVE'
                    ? 'bg-rose-950/40 border-rose-600 text-rose-300 shadow-lg shadow-rose-950/20'
                    : 'bg-indigo-900/30 border-indigo-500 text-indigo-300 shadow-lg shadow-indigo-950/20'
                  : mode === 'LIVE'
                    ? 'bg-zinc-950 border-zinc-800 text-zinc-500 hover:border-rose-800'
                    : 'bg-zinc-900 border-zinc-800 text-zinc-500 hover:border-zinc-700'
              }`}
            >
              <div className="flex items-center justify-between">
                <span className="font-bold text-sm">{mode}</span>
                {mode === 'LIVE' && (
                  <span className="text-[9px] px-1.5 py-0.5 rounded bg-rose-900/60 border border-rose-700 text-rose-300 font-mono font-bold">
                    RELEASE GATED
                  </span>
                )}
              </div>
              <div className="text-[10px] mt-1 opacity-80 leading-relaxed">
                {mode === 'PAPER'
                  ? 'Simulated execution (all pairs)'
                  : mode === 'TESTNET'
                    ? 'Binance Futures Testnet'
                    : 'Binance USDⓈ-M Mainnet'}
              </div>
            </button>
          ))}
        </div>
      </div>

      {/* Instrument Universe Controls */}
      <div className="space-y-2 pt-1">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <label className="text-xs font-semibold text-zinc-300 flex items-center space-x-1.5">
            <Coins className="w-3.5 h-3.5 text-cyan-400" />
            <span>Target Tradable Instruments</span>
          </label>
          <div className="flex items-center space-x-1.5 text-[11px]">
            <button
              type="button"
              onClick={selectAllFiltered}
              className="px-2 py-0.5 rounded bg-zinc-800 hover:bg-zinc-700 text-zinc-300 hover:text-white transition-colors"
            >
              Select All
            </button>
            <button
              type="button"
              onClick={selectCoreInstruments}
              className="px-2 py-0.5 rounded bg-indigo-950 border border-indigo-800/80 hover:bg-indigo-900 text-indigo-300 transition-colors flex items-center space-x-1"
            >
              <Sparkles className="w-3 h-3" />
              <span>Core Pairs</span>
            </button>
            <button
              type="button"
              onClick={clearAllInstruments}
              className="px-2 py-0.5 rounded bg-zinc-800 hover:bg-zinc-700 text-zinc-400 hover:text-zinc-200 transition-colors"
            >
              Clear
            </button>
          </div>
        </div>

        {/* Filter Bar: Categories and Search */}
        <div className="flex flex-col sm:flex-row items-stretch sm:items-center gap-2 pt-1">
          {/* Category Chips */}
          <div className="flex items-center space-x-1 overflow-x-auto pb-1 sm:pb-0 scrollbar-thin">
            {CATEGORIES.map((cat) => (
              <button
                key={cat}
                type="button"
                onClick={() => setSelectedCategory(cat)}
                className={`px-2.5 py-1 rounded-lg text-[10px] font-bold tracking-wider transition-all whitespace-nowrap ${
                  selectedCategory === cat
                    ? 'bg-cyan-600 text-white shadow-sm'
                    : 'bg-zinc-950 border border-zinc-800 text-zinc-400 hover:text-zinc-200'
                }`}
              >
                {cat}
              </button>
            ))}
          </div>

          {/* Search Box */}
          <div className="relative flex-1">
            <Search className="w-3.5 h-3.5 text-zinc-500 absolute left-2.5 top-1/2 -translate-y-1/2" />
            <input
              type="text"
              value={searchQuery}
              onChange={(e) => setSearchQuery(e.target.value)}
              placeholder="Search BTC, ETH, SOL, Meme..."
              className="w-full pl-8 pr-3 py-1 bg-zinc-950 border border-zinc-800 rounded-lg text-xs text-zinc-200 placeholder-zinc-500 focus:outline-none focus:border-cyan-500"
            />
          </div>
        </div>

        {/* Instrument Cards Grid */}
        <div className="grid grid-cols-2 sm:grid-cols-3 gap-2 max-h-56 overflow-y-auto p-1 border border-zinc-800/80 rounded-xl bg-zinc-950/50">
          {filteredInstruments.map((inst) => {
            const isSupported = currentModeSupportedSymbols.has(inst.symbol);
            const active = Boolean(instruments[inst.symbol] && isSupported);

            return (
              <button
                key={inst.symbol}
                type="button"
                disabled={!isSupported}
                onClick={() => toggleInstrument(inst.symbol)}
                className={`p-2.5 border rounded-xl text-left transition-all flex flex-col justify-between relative group ${
                  !isSupported
                    ? 'opacity-35 bg-zinc-950 border-zinc-900 cursor-not-allowed text-zinc-600'
                    : active
                      ? 'bg-emerald-950/30 border-emerald-600/80 text-emerald-300 shadow-sm shadow-emerald-950/30'
                      : 'bg-zinc-900 border-zinc-800 text-zinc-400 hover:border-zinc-700 hover:text-zinc-200 cursor-pointer'
                }`}
              >
                <div className="flex items-center justify-between w-full">
                  <div className="flex items-center space-x-1.5">
                    <span className="font-bold font-mono text-xs">{inst.symbol}</span>
                  </div>
                  {active ? (
                    <CheckSquare className="w-4 h-4 text-emerald-400 shrink-0" />
                  ) : (
                    <Square className={`w-4 h-4 shrink-0 ${isSupported ? 'text-zinc-600 group-hover:text-zinc-400' : 'text-zinc-800'}`} />
                  )}
                </div>

                <div className="flex items-center justify-between text-[10px] mt-1.5">
                  <span className="truncate text-zinc-400 max-w-[90px]">{inst.name}</span>
                  <span
                    className={`px-1 py-0.2 rounded font-mono text-[9px] ${
                      inst.quoteAsset === 'USDC'
                        ? 'bg-blue-950 text-blue-300 border border-blue-800/60'
                        : 'bg-zinc-800 text-zinc-400'
                    }`}
                  >
                    {inst.quoteAsset}
                  </span>
                </div>

                {!isSupported && (
                  <span className="text-[9px] text-zinc-600 italic mt-0.5">
                    Not in {executionMode}
                  </span>
                )}
              </button>
            );
          })}
        </div>

        {/* Selected Pairs Summary / Warning */}
        {activeInstrumentsList.length === 0 ? (
          <div className="p-2.5 bg-rose-950/20 border border-rose-900/60 rounded-lg flex items-center space-x-2 text-rose-400 text-xs">
            <AlertTriangle className="w-4 h-4 shrink-0" />
            <span>Please select at least one instrument to enable systematic execution.</span>
          </div>
        ) : (
          <div className="p-2 bg-zinc-900/70 border border-zinc-800 rounded-lg flex items-center justify-between text-xs text-zinc-300">
            <div className="flex items-center space-x-2">
              <span className="w-2 h-2 rounded-full bg-emerald-400 animate-pulse" />
              <span className="text-[11px]">
                Active Targets: <span className="font-mono text-emerald-400 font-bold">{activeInstrumentsList.join(', ')}</span>
              </span>
            </div>
            <span className="text-[10px] font-mono text-zinc-500">
              {activeInstrumentsList.length} pair{activeInstrumentsList.length > 1 ? 's' : ''}
            </span>
          </div>
        )}
      </div>

      <div className="flex justify-end pt-2">
        <button
          type="button"
          disabled={activeInstrumentsList.length === 0}
          onClick={() => setStep(2)}
          className="px-4 py-2 bg-indigo-600 hover:bg-indigo-500 disabled:opacity-40 disabled:cursor-not-allowed text-white rounded-lg text-xs font-bold transition-colors cursor-pointer"
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
              type="button"
              onClick={() => setStrategies({ ...strategies, [strat]: !active })}
              className={`p-3 border rounded-xl flex items-center justify-between transition-all cursor-pointer ${
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
              type="button"
              onClick={() => setRiskProfile(profile)}
              className={`p-3 border rounded-xl text-left transition-all cursor-pointer ${
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
          type="button"
          onClick={() => setStep(1)}
          className="px-4 py-2 bg-zinc-800 hover:bg-zinc-700 text-zinc-300 rounded-lg text-xs font-bold transition-colors cursor-pointer"
        >
          Back
        </button>
        <button
          type="button"
          onClick={() => setStep(3)}
          className="px-4 py-2 bg-indigo-600 hover:bg-indigo-500 text-white rounded-lg text-xs font-bold transition-colors cursor-pointer"
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

      <div className="bg-zinc-950 border border-zinc-800 rounded-xl p-3 space-y-2" aria-label="Operator readiness checks">
        {combinedChecks.map((check) => (
          <div key={check.id} className="flex items-start gap-3 p-2 border-b border-zinc-800/50 last:border-0">
            <div className="mt-0.5">
              {renderCheckIcon(check.status)}
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

      {!canArm && (
        <div className="p-3 bg-amber-950/30 border border-amber-900 rounded-lg flex gap-3">
          <ShieldAlert className="w-5 h-5 text-amber-400 shrink-0" />
          <div className="text-xs text-amber-300">
            <strong>Preflight Failed.</strong> The system cannot be armed in this configuration. Please resolve the failing checks above or select a different execution mode.
          </div>
        </div>
      )}

      <div className="flex justify-between pt-4">
        <button
          type="button"
          onClick={() => setStep(2)}
          className="px-4 py-2 bg-zinc-800 hover:bg-zinc-700 text-zinc-300 rounded-lg text-xs font-bold transition-colors cursor-pointer"
        >
          Back
        </button>
        <button
          type="button"
          onClick={handleArm}
          disabled={loading || !canArm}
          className="flex items-center space-x-2 px-5 py-2 bg-emerald-600 hover:bg-emerald-500 disabled:opacity-50 text-white rounded-lg text-xs font-bold transition-colors cursor-pointer"
        >
          <Play className="w-4 h-4" />
          <span>ARM ENGINE ({executionMode})</span>
        </button>
      </div>
    </div>
  );

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center p-4 bg-black/60 backdrop-blur-sm animate-fadeIn">
      <div className="bg-zinc-900 border border-zinc-800 rounded-2xl w-full max-w-2xl overflow-hidden shadow-2xl">
        <div className="p-4 border-b border-zinc-800 bg-zinc-950 flex items-center justify-between">
          <div className="flex items-center space-x-2">
            <div className="p-1.5 rounded-lg bg-indigo-900 text-indigo-300">
              <Rocket className="w-4 h-4" />
            </div>
            <h2 className="text-sm font-bold text-zinc-100 uppercase tracking-wider">Start Trading Wizard</h2>
          </div>
          <button onClick={onCancel} className="text-zinc-500 hover:text-zinc-300 cursor-pointer">
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
