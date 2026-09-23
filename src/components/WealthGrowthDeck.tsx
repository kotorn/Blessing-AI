import React, { useState, useEffect } from 'react';
import { apiClient } from '../api/client';
import {
  TrendingUp,
  ShieldAlert,
  ShieldCheck,
  AlertTriangle,
  Award,
  Layers,
  HelpCircle,
  RefreshCw,
  GitCommit,
  Activity,
  Zap,
  Sparkles,
  Lock,
} from 'lucide-react';

interface WealthMetrics {
  evidence: {
    status: 'INSUFFICIENT_SAMPLE' | 'PROCESS_LOCAL_UNVERIFIED';
    source: 'PROCESS_MEMORY';
    sample_size: number;
    authoritative: boolean;
    reason: string;
  };
  portfolio: {
    total_trades: number;
    win_trades: number;
    loss_trades: number;
    break_even_trades: number;
    win_rate_pct: number;
    payoff_ratio: number;
    profit_factor: number;
    expectancy_usdt: number;
    gross_profit: number;
    gross_loss: number;
    net_pnl: number;
    total_commission: number;
    total_funding: number;
    fee_drag_pct: number;
    max_drawdown_pct: number;
    cagr_pct: number;
    sharpe_ratio: number;
    sortino_ratio: number;
    calmar_ratio: number;
    var_95_pct: number;
    cvar_95_pct: number;
    avg_slippage_bps: number;
    unknown_risk_violations: number;
    sustainable_growth_score: number;
    is_capital_safe: boolean;
    capital_safety_status: 'UNKNOWN' | 'SAFE' | 'UNSAFE';
  };
  promotion_gate: {
    current_stage: string;
    target_stage: string;
    eligible: boolean;
    passed_criteria: string[];
    blocking_reasons: string[];
  };
  strategies: Record<string, {
    total_trades: number;
    win_rate_pct: number;
    net_pnl: number;
    sharpe_ratio: number;
    max_drawdown_pct: number;
  }>;
}

interface EightDIncident {
  incident_id: string;
  title: string;
  severity: string;
  status: string;
  trigger_type: string;
  symbol?: string;
  strategy_id?: string;
  lineage_id?: string;
  d1_team: string[];
  d2_problem: {
    expected_behavior?: any;
    actual_outcome?: any;
    financial_impact_usdt?: number;
    context_summary?: string;
  };
  d3_containment: {
    action_name?: string;
    parameters?: any;
    applied_by?: string;
    status?: string;
  };
  d4_root_cause?: {
    problem_statement: string;
    levels: Array<{ level: string; question: string; answer: string }>;
    root_cause_summary: string;
    preventive_insight: string;
  };
  d5_pca: Array<{
    pca_id: string;
    title: string;
    description: string;
    target_component: string;
  }>;
  d6_verification?: {
    evidence?: string;
    passed?: boolean;
  };
  d7_prevention?: Array<{
    policy_update: string;
    scope: string;
  }>;
  d8_closure?: {
    lessons_learned_summary?: string;
    signoff_user_id?: string;
    closed_at?: string;
  };
  created_at: string;
}

interface PDCACheck {
  sample_size: number;
  evidence_status: 'INSUFFICIENT_SAMPLE' | 'PROCESS_LOCAL_UNVERIFIED';
  authoritative: boolean;
  plan_win_rate_pct: number;
  actual_win_rate_pct: number;
  win_rate_gap_pct: number;
  plan_edge_bps: number;
  actual_edge_bps: number;
  edge_decay_bps: number;
  plan_slippage_bps: number;
  actual_slippage_bps: number;
  drift_detected: boolean | null;
  drift_severity: string;
  recommended_actions: string[];
  triggers_8d: boolean | null;
}

interface TradeLineageItem {
  lineage_id: string;
  symbol: string;
  strategy_id: string;
  market_state: any;
  intent: any;
  opportunity_score: any;
  allocation_multiplier: string;
  risk_decision: any;
  entry_price: string;
  exit_price?: string;
  net_pnl: string;
  slippage_bps: string;
  expected_edge_bps: string;
  actual_edge_bps: string;
  edge_decay_bps: string;
  outcome_grade: string;
  requires_8d: boolean;
  why_why_analysis_id?: string;
  lessons_learned: string[];
  created_at: string;
  closed_at?: string;
}

const STAGES = [
  'OBSERVE_ONLY',
  'SHADOW_TRADING',
  'STAGED_FIRST_ORDER',
  'SMALL_LIVE',
  'CONSTRAINED_AUTONOMOUS',
  'PORTFOLIO_AUTONOMOUS',
];

export const WealthGrowthDeck: React.FC = () => {
  const [metrics, setMetrics] = useState<WealthMetrics | null>(null);
  const [incidents, setIncidents] = useState<EightDIncident[]>([]);
  const [pdca, setPdca] = useState<Record<string, PDCACheck>>({});
  const [lineages, setLineages] = useState<TradeLineageItem[]>([]);
  const [selectedIncident, setSelectedIncident] = useState<EightDIncident | null>(null);
  const [activeDeckTab, setActiveDeckTab] = useState<'METRICS' | 'INCIDENTS_8D' | 'PDCA' | 'LINEAGE'>('METRICS');
  const [loading, setLoading] = useState(false);
  const [telemetryState, setTelemetryState] = useState<'loading' | 'available' | 'unavailable'>('loading');
  const [closingIncident, setClosingIncident] = useState(false);
  const [verificationEvidence, setVerificationEvidence] = useState('');
  const [preventionEvidence, setPreventionEvidence] = useState('');
  const [closureLessons, setClosureLessons] = useState('');
  const [closureConfirmed, setClosureConfirmed] = useState(false);
  const [closureError, setClosureError] = useState<string | null>(null);

  const fetchData = async () => {
    try {
      setLoading(true);
      const [mRes, iRes, pRes, lRes] = await Promise.all([
        apiClient.get<WealthMetrics>('/api/wealth/metrics'),
        apiClient.get<EightDIncident[]>('/api/incidents/8d'),
        apiClient.get<Record<string, PDCACheck>>('/api/learning/pdca'),
        apiClient.get<TradeLineageItem[]>('/api/learning/lineages'),
      ]);

      setMetrics(mRes);
      setIncidents(iRes);
      setPdca(pRes);
      setLineages(lRes);
      setTelemetryState('available');
    } catch (e) {
      console.error('Failed to load wealth engine telemetry:', e);
      setMetrics(null);
      setIncidents([]);
      setPdca({});
      setLineages([]);
      setTelemetryState('unavailable');
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    void fetchData();
    const interval = setInterval(fetchData, 10000);
    return () => clearInterval(interval);
  }, []);

  const handleCloseIncident = async (incidentId: string) => {
    if (!verificationEvidence.trim() || !preventionEvidence.trim() || !closureLessons.trim() || !closureConfirmed) {
      setClosureError('Enter actual D6 verification, D7 prevention, D8 lessons, and confirm the evidence before signing off.');
      return;
    }
    try {
      setClosingIncident(true);
      setClosureError(null);
      await apiClient.post(`/api/incidents/8d/${encodeURIComponent(incidentId)}/close`, {
        verification: verificationEvidence.trim(),
        prevention: preventionEvidence.trim(),
        lessons: closureLessons.trim(),
      });
      setVerificationEvidence('');
      setPreventionEvidence('');
      setClosureLessons('');
      setClosureConfirmed(false);
      await fetchData();
      setSelectedIncident(null);
    } catch (e) {
      setClosureError(e instanceof Error ? e.message : 'Incident closure failed.');
    } finally {
      setClosingIncident(false);
    }
  };

  const selectIncident = (incident: EightDIncident) => {
    setSelectedIncident(incident);
    setVerificationEvidence('');
    setPreventionEvidence('');
    setClosureLessons('');
    setClosureConfirmed(false);
    setClosureError(null);
  };

  if (telemetryState !== 'available') {
    return (
      <div
        role={telemetryState === 'unavailable' ? 'alert' : 'status'}
        className="rounded-2xl border border-amber-800 bg-zinc-950 p-8 text-center space-y-3"
      >
        <ShieldAlert className="mx-auto h-8 w-8 text-amber-400" />
        <h2 className="text-sm font-bold text-amber-200">
          {telemetryState === 'loading' ? 'Loading Wealth Evidence' : 'WEALTH_TELEMETRY_UNAVAILABLE'}
        </h2>
        <p className="text-xs text-zinc-400">
          Worker metrics and incident evidence could not be verified. Safety, promotion, and incident status are not inferred.
        </p>
        <button
          onClick={() => void fetchData()}
          disabled={loading}
          className="rounded-lg border border-zinc-700 px-3 py-2 text-xs text-zinc-200 disabled:opacity-50"
        >
          {loading ? 'Checking…' : 'Retry'}
        </button>
      </div>
    );
  }

  const p = metrics?.portfolio;
  const evidence = metrics?.evidence;
  const hasVerifiedEvidence = evidence?.authoritative === true && evidence.sample_size > 0;
  const gate = metrics?.promotion_gate;
  const promotionEligible = gate?.eligible === true && hasVerifiedEvidence;
  const currentStage = gate?.current_stage || 'OBSERVE_ONLY';
  const stageIdx = STAGES.indexOf(currentStage);

  return (
    <div className="space-y-6">
      {/* 1. Header Banner & Rule #0 Invariant */}
      <div className="bg-gradient-to-r from-zinc-950 via-zinc-900 to-indigo-950/40 border border-zinc-800 rounded-2xl p-5 shadow-2xl relative overflow-hidden">
        <div className="flex flex-wrap items-center justify-between gap-4">
          <div className="space-y-1">
            <div className="flex items-center space-x-2">
              <span className="p-1.5 rounded-lg bg-indigo-950/80 border border-indigo-700/60 text-indigo-400">
                <TrendingUp className="w-5 h-5" />
              </span>
              <h2 className="text-lg font-bold text-zinc-100 flex items-center space-x-2">
                <span>Wealth Growth & Risk-Adjusted Return Engine</span>
                <span className="text-xs px-2 py-0.5 rounded-full bg-amber-950 text-amber-200 border border-amber-800 font-mono">
                  OBSERVE ONLY
                </span>
              </h2>
            </div>
            <p className="text-xs text-zinc-400 font-mono">
              Observe → Analyze → Decide → Risk Check → Execute → Verify → Learn → Improve → Repeat
            </p>
          </div>

          <div className="flex items-center space-x-3">
            {/* Rule #0 Badge */}
            <div
              className={`flex items-center space-x-2 px-3 py-1.5 rounded-xl border text-xs font-mono font-bold ${
                p?.unknown_risk_violations && p.unknown_risk_violations > 0
                  ? 'bg-rose-950/90 text-rose-300 border-rose-800 animate-pulse'
                  : hasVerifiedEvidence
                    ? 'bg-emerald-950/70 text-emerald-300 border-emerald-800'
                    : 'bg-amber-950/70 text-amber-200 border-amber-800'
              }`}
            >
              {p?.unknown_risk_violations && p.unknown_risk_violations > 0 ? (
                <ShieldAlert className="w-4 h-4 text-rose-400" />
              ) : hasVerifiedEvidence ? (
                <ShieldCheck className="w-4 h-4 text-emerald-400" />
              ) : (
                <HelpCircle className="w-4 h-4 text-amber-400" />
              )}
              <span>
                RULE #0: {p?.unknown_risk_violations && p.unknown_risk_violations > 0
                  ? 'BREACHED (NO NEW RISK)'
                  : hasVerifiedEvidence
                    ? 'NO BREACH OBSERVED'
                    : 'STATUS UNKNOWN — NO VERIFIED SAMPLE'}
              </span>
            </div>

            <button
              onClick={fetchData}
              disabled={loading}
              className="p-2 rounded-xl bg-zinc-900 border border-zinc-700 text-zinc-300 hover:text-white transition-all"
              title="Refresh Engine"
            >
              <RefreshCw className={`w-4 h-4 ${loading ? 'animate-spin' : ''}`} />
            </button>
          </div>
        </div>

        <div role="status" className="mt-4 rounded-xl border border-amber-800/70 bg-amber-950/40 p-3 text-xs text-amber-100">
          <div className="font-bold font-mono">
            {evidence?.status === 'INSUFFICIENT_SAMPLE' ? 'INSUFFICIENT SAMPLE' : 'UNVERIFIED PROCESS-LOCAL SAMPLE'}
            {' · '}{evidence?.sample_size ?? 0} closed trades
          </div>
          <p className="mt-1 text-amber-200/90">
            {evidence?.reason || 'No authoritative closed-trade evidence is available. Performance and safety metrics are not proof of live results.'}
          </p>
        </div>

        {/* Progressive Deployment Pipeline */}
        <div className="mt-5 pt-4 border-t border-zinc-800/80">
          <div className="flex items-center justify-between text-xs text-zinc-400 font-mono mb-2">
            <span className="font-bold text-zinc-300 flex items-center space-x-1.5">
              <Layers className="w-3.5 h-3.5 text-indigo-400" />
              <span>Progressive Mainnet Deployment Pipeline</span>
            </span>
            <span>
              Stage: <strong className="text-indigo-300">{currentStage}</strong> → Target: <strong className="text-emerald-300">{gate?.target_stage || 'PORTFOLIO_AUTONOMOUS'}</strong>
            </span>
          </div>

          <div className="grid grid-cols-2 md:grid-cols-6 gap-2 font-mono text-[11px]">
            {STAGES.map((st, idx) => {
              const isActive = idx === stageIdx;
              const isPast = idx < stageIdx;
              const isTarget = idx === stageIdx + 1;
              return (
                <div
                  key={st}
                  className={`p-2 rounded-lg border text-center transition-all ${
                    isActive
                      ? 'bg-indigo-950/80 border-indigo-500 text-indigo-200 font-bold shadow-md shadow-indigo-950'
                      : isPast
                      ? 'bg-emerald-950/40 border-emerald-800/60 text-emerald-400'
                      : isTarget
                      ? promotionEligible
                        ? 'bg-emerald-950/60 border-emerald-500 text-emerald-300 border-dashed animate-pulse'
                        : 'bg-zinc-950/60 border-zinc-800 text-zinc-500 border-dashed'
                      : 'bg-zinc-950/40 border-zinc-900 text-zinc-600'
                  }`}
                >
                  <div className="text-[10px] text-zinc-500">Stage {idx + 1}</div>
                  <div className="truncate">{st.replace('_', ' ')}</div>
                  <div className="text-[9px] mt-0.5">
                    {isActive ? '● CURRENT' : isPast ? '✓ PASSED' : isTarget ? (promotionEligible ? '★ ELIGIBLE' : '🔒 LOCKED') : 'PENDING'}
                  </div>
                </div>
              );
            })}
          </div>

          {/* Promotion Criteria Feedback */}
          {gate && gate.blocking_reasons.length > 0 && (
            <div className="mt-2 text-[11px] font-mono text-amber-400 bg-amber-950/30 p-2 rounded-lg border border-amber-800/50 flex items-center space-x-2">
              <Lock className="w-3.5 h-3.5 text-amber-400 shrink-0" />
              <span>Next Stage Promotion Gate: {gate.blocking_reasons.join('; ')}</span>
            </div>
          )}
        </div>
      </div>

      {/* 2. Navigation Tabs */}
      <div className="flex items-center space-x-2 bg-zinc-950 p-1.5 rounded-xl border border-zinc-800 text-xs font-mono">
        <button
          onClick={() => setActiveDeckTab('METRICS')}
          className={`px-4 py-2 rounded-lg font-bold transition-all flex items-center space-x-2 ${
            activeDeckTab === 'METRICS'
              ? 'bg-indigo-600 text-white shadow-sm'
              : 'text-zinc-400 hover:text-zinc-200'
          }`}
        >
          <Activity className="w-4 h-4" />
          <span>RISK-ADJUSTED WEALTH</span>
        </button>

        <button
          onClick={() => setActiveDeckTab('INCIDENTS_8D')}
          className={`px-4 py-2 rounded-lg font-bold transition-all flex items-center space-x-2 relative ${
            activeDeckTab === 'INCIDENTS_8D'
              ? 'bg-indigo-600 text-white shadow-sm'
              : 'text-zinc-400 hover:text-zinc-200'
          }`}
        >
          <AlertTriangle className="w-4 h-4 text-amber-400" />
          <span>8D PROBLEM SOLVING</span>
          {incidents.filter((i) => i.status !== 'D8_CLOSED').length > 0 && (
            <span className="px-1.5 py-0.2 rounded-full bg-rose-600 text-white text-[10px]">
              {incidents.filter((i) => i.status !== 'D8_CLOSED').length}
            </span>
          )}
        </button>

        <button
          onClick={() => setActiveDeckTab('PDCA')}
          className={`px-4 py-2 rounded-lg font-bold transition-all flex items-center space-x-2 ${
            activeDeckTab === 'PDCA'
              ? 'bg-indigo-600 text-white shadow-sm'
              : 'text-zinc-400 hover:text-zinc-200'
          }`}
        >
          <RefreshCw className="w-4 h-4 text-cyan-400" />
          <span>PDCA CLOSED-LOOP</span>
        </button>

        <button
          onClick={() => setActiveDeckTab('LINEAGE')}
          className={`px-4 py-2 rounded-lg font-bold transition-all flex items-center space-x-2 ${
            activeDeckTab === 'LINEAGE'
              ? 'bg-indigo-600 text-white shadow-sm'
              : 'text-zinc-400 hover:text-zinc-200'
          }`}
        >
          <GitCommit className="w-4 h-4 text-emerald-400" />
          <span>TRADE LINEAGE & PROVENANCE</span>
        </button>
      </div>

      {/* Tab 1: Risk-Adjusted Wealth Performance */}
      {activeDeckTab === 'METRICS' && (
        <div className="space-y-4">
          <div className="grid grid-cols-2 md:grid-cols-4 gap-3 font-mono">
            {/* Sharpe Ratio */}
            <div className="p-4 rounded-xl bg-zinc-900/90 border border-zinc-800 space-y-1">
              <span className="text-zinc-400 text-xs flex items-center justify-between">
                <span>SHARPE RATIO</span>
                <Sparkles className="w-3.5 h-3.5 text-indigo-400" />
              </span>
              <div className="text-xl font-bold text-zinc-100">
                {hasVerifiedEvidence && p ? p.sharpe_ratio.toFixed(2) : '—'}
              </div>
              <p className="text-[10px] text-zinc-500">Annualized excess return / volatility</p>
            </div>

            {/* Sortino Ratio */}
            <div className="p-4 rounded-xl bg-zinc-900/90 border border-zinc-800 space-y-1">
              <span className="text-zinc-400 text-xs flex items-center justify-between">
                <span>SORTINO RATIO</span>
                <TrendingUp className="w-3.5 h-3.5 text-zinc-500" />
              </span>
              <div className="text-xl font-bold text-zinc-400">
                {hasVerifiedEvidence && p ? p.sortino_ratio.toFixed(2) : '—'}
              </div>
              <p className="text-[10px] text-zinc-500">Downside deviation adjusted</p>
            </div>

            {/* Calmar Ratio */}
            <div className="p-4 rounded-xl bg-zinc-900/90 border border-zinc-800 space-y-1">
              <span className="text-zinc-400 text-xs flex items-center justify-between">
                <span>CALMAR RATIO</span>
                <Award className="w-3.5 h-3.5 text-cyan-400" />
              </span>
              <div className="text-xl font-bold text-zinc-100">
                {hasVerifiedEvidence && p ? p.calmar_ratio.toFixed(2) : '—'}
              </div>
              <p className="text-[10px] text-zinc-500">CAGR % / Max Drawdown %</p>
            </div>

            {/* Sustainable Growth Score */}
            <div className="p-4 rounded-xl bg-zinc-900/90 border border-indigo-900/50 space-y-1 bg-gradient-to-br from-zinc-900 to-indigo-950/30">
              <span className="text-zinc-400 text-xs flex items-center justify-between">
                <span>GROWTH SCORE</span>
                <Zap className="w-3.5 h-3.5 text-amber-400" />
              </span>
              <div className="text-xl font-bold text-zinc-400">
                {hasVerifiedEvidence && p ? p.sustainable_growth_score.toFixed(1) : '—'} / 100
              </div>
              <p className="text-[10px] text-zinc-500">Composite wealth stability index</p>
            </div>
          </div>

          <div className="grid grid-cols-2 md:grid-cols-4 gap-3 font-mono">
            {/* Max Drawdown */}
            <div className="p-4 rounded-xl bg-zinc-900/90 border border-zinc-800 space-y-1">
              <span className="text-zinc-400 text-xs">MAX DRAWDOWN</span>
              <div className={`text-xl font-bold ${!hasVerifiedEvidence ? 'text-zinc-400' : p && p.max_drawdown_pct > 3.0 ? 'text-amber-400' : 'text-zinc-100'}`}>
                {hasVerifiedEvidence && p ? p.max_drawdown_pct.toFixed(2) : '—'}%
              </div>
              <p className="text-[10px] text-zinc-500">Peak-to-trough decline</p>
            </div>

            {/* Value at Risk (95%) */}
            <div className="p-4 rounded-xl bg-zinc-900/90 border border-zinc-800 space-y-1">
              <span className="text-zinc-400 text-xs">VaR 95% / CVaR 95%</span>
              <div className="text-xl font-bold text-zinc-100">
                {hasVerifiedEvidence && p ? `${p.var_95_pct.toFixed(1)}% / ${p.cvar_95_pct.toFixed(1)}%` : '— / —'}
              </div>
              <p className="text-[10px] text-zinc-500">Expected Shortfall tail risk</p>
            </div>

            {/* Win Rate & Payoff */}
            <div className="p-4 rounded-xl bg-zinc-900/90 border border-zinc-800 space-y-1">
              <span className="text-zinc-400 text-xs">WIN RATE & PAYOFF</span>
              <div className="text-xl font-bold text-zinc-400">
                {hasVerifiedEvidence && p ? `${p.win_rate_pct.toFixed(1)}% (${p.payoff_ratio.toFixed(2)}x)` : '—'}
              </div>
              <p className="text-[10px] text-zinc-500">{hasVerifiedEvidence && p ? `${p.win_trades}W / ${p.loss_trades}L (${p.total_trades} Total)` : 'No verified sample'}</p>
            </div>

            {/* Profit Factor & Expectancy */}
            <div className="p-4 rounded-xl bg-zinc-900/90 border border-zinc-800 space-y-1">
              <span className="text-zinc-400 text-xs">PROFIT FACTOR</span>
              <div className="text-xl font-bold text-zinc-100">
                {hasVerifiedEvidence && p ? `${p.profit_factor.toFixed(2)} ($${p.expectancy_usdt.toFixed(2)})` : '—'}
              </div>
              <p className="text-[10px] text-zinc-500">Expectancy per trade</p>
            </div>
          </div>

          <div className="grid grid-cols-1 md:grid-cols-3 gap-3 font-mono text-xs">
            <div className="p-3 bg-zinc-950 rounded-xl border border-zinc-800 space-y-1">
              <span className="text-zinc-500">EXECUTION SLIPPAGE DRAG:</span>
              <div className="text-zinc-200 font-bold">{hasVerifiedEvidence && p ? `${p.avg_slippage_bps.toFixed(2)} bps average` : '—'}</div>
            </div>
            <div className="p-3 bg-zinc-950 rounded-xl border border-zinc-800 space-y-1">
              <span className="text-zinc-500">COMMISSIONS & FUNDING:</span>
              <div className="text-zinc-200 font-bold">{hasVerifiedEvidence && p ? `$${p.total_commission.toFixed(2)} fees | $${p.total_funding.toFixed(2)} carry` : '—'}</div>
            </div>
            <div className="p-3 bg-zinc-950 rounded-xl border border-zinc-800 space-y-1">
              <span className="text-zinc-500">NET REALIZED WEALTH GROWTH:</span>
              <div className={`font-bold ${!hasVerifiedEvidence ? 'text-zinc-400' : p && p.net_pnl >= 0 ? 'text-emerald-400' : 'text-rose-400'}`}>
                {hasVerifiedEvidence && p ? `$${p.net_pnl.toFixed(2)} USDT` : '—'}
              </div>
            </div>
          </div>
        </div>
      )}

      {/* Tab 2: 8D Problem Solving Deck */}
      {activeDeckTab === 'INCIDENTS_8D' && (
        <div className="space-y-4">
          <div className="flex items-center justify-between">
            <h3 className="text-sm font-bold text-zinc-200 flex items-center space-x-2">
              <AlertTriangle className="w-4 h-4 text-amber-400" />
              <span>8D Problem Solving Incidents ({incidents.length})</span>
            </h3>
            <span className="text-xs text-zinc-500 font-mono">
              Snapshot records only; absence of incidents does not prove execution was incident-free
            </span>
          </div>

          {incidents.length === 0 ? (
            <div className="p-8 text-center rounded-2xl bg-zinc-900/50 border border-zinc-800/80 space-y-2">
              <HelpCircle className="w-8 h-8 text-amber-400 mx-auto" />
              <div className="text-sm font-bold text-zinc-200">No 8D records in this worker snapshot</div>
              <p className="text-xs text-zinc-400">This does not establish incident-free execution. Verified closed-trade lineage is not connected to durable execution history.</p>
            </div>
          ) : (
            <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
              {incidents.map((inc) => (
                <div
                  key={inc.incident_id}
                  onClick={() => selectIncident(inc)}
                  className={`p-4 rounded-xl border cursor-pointer transition-all ${
                    selectedIncident?.incident_id === inc.incident_id
                      ? 'bg-zinc-900 border-indigo-500 shadow-lg shadow-indigo-950'
                      : 'bg-zinc-950/80 border-zinc-800 hover:border-zinc-700'
                  }`}
                >
                  <div className="flex items-center justify-between text-xs font-mono mb-2">
                    <span className="font-bold text-zinc-200">{inc.incident_id}</span>
                    <span
                      className={`px-2 py-0.5 rounded text-[10px] font-bold ${
                        inc.severity === 'CRITICAL'
                          ? 'bg-rose-950 text-rose-300 border border-rose-800'
                          : inc.severity === 'HIGH'
                          ? 'bg-amber-950 text-amber-300 border border-amber-800'
                          : 'bg-zinc-800 text-zinc-300'
                      }`}
                    >
                      {inc.severity}
                    </span>
                  </div>
                  <h4 className="text-xs font-bold text-zinc-100 mb-1">{inc.title}</h4>
                  <div className="text-[11px] text-zinc-400 font-mono space-y-1">
                    <div>Status: <strong className="text-indigo-400">{inc.status}</strong></div>
                    <div>Trigger: {inc.trigger_type} ({inc.symbol || 'GLOBAL'})</div>
                  </div>
                </div>
              ))}
            </div>
          )}

          {/* Selected Incident Deep Investigation Modal / Panel */}
          {selectedIncident && (
            <div className="p-5 rounded-2xl bg-zinc-900 border border-zinc-700 space-y-4 shadow-2xl">
              <div className="flex items-center justify-between border-b border-zinc-800 pb-3">
                <div>
                  <span className="text-xs font-mono text-zinc-500">{selectedIncident.incident_id}</span>
                  <h3 className="text-sm font-bold text-zinc-100">{selectedIncident.title}</h3>
                </div>
                <div className="flex items-center space-x-2">
                  <span className="px-2 py-1 rounded bg-indigo-950 text-indigo-300 border border-indigo-800 text-xs font-mono">
                    {selectedIncident.status}
                  </span>
                </div>
              </div>

              {selectedIncident.status === 'D5_PCA_CHOSEN' && (
                <form
                  onSubmit={(event) => {
                    event.preventDefault();
                    void handleCloseIncident(selectedIncident.incident_id);
                  }}
                  className="space-y-3 rounded-xl border border-zinc-700 bg-zinc-950 p-4"
                >
                  <div className="text-xs text-zinc-400">
                    Record actual verification and prevention evidence. The authenticated operator identity is attached by the server.
                  </div>
                  <label className="block space-y-1 text-xs text-zinc-300">
                    <span>D6 verification evidence</span>
                    <textarea
                      required
                      maxLength={4000}
                      value={verificationEvidence}
                      onChange={(event) => setVerificationEvidence(event.target.value)}
                      className="min-h-20 w-full rounded-lg border border-zinc-700 bg-zinc-900 p-2"
                      placeholder="Test run, result, or evidence reference"
                    />
                  </label>
                  <label className="block space-y-1 text-xs text-zinc-300">
                    <span>D7 systemic prevention applied</span>
                    <textarea
                      required
                      maxLength={4000}
                      value={preventionEvidence}
                      onChange={(event) => setPreventionEvidence(event.target.value)}
                      className="min-h-20 w-full rounded-lg border border-zinc-700 bg-zinc-900 p-2"
                      placeholder="Describe the change actually applied and its scope"
                    />
                  </label>
                  <label className="block space-y-1 text-xs text-zinc-300">
                    <span>D8 closure lessons</span>
                    <textarea
                      required
                      maxLength={4000}
                      value={closureLessons}
                      onChange={(event) => setClosureLessons(event.target.value)}
                      className="min-h-20 w-full rounded-lg border border-zinc-700 bg-zinc-900 p-2"
                      placeholder="Record the verified outcome and lessons"
                    />
                  </label>
                  <label className="flex items-start gap-2 text-xs text-zinc-300">
                    <input
                      type="checkbox"
                      required
                      checked={closureConfirmed}
                      onChange={(event) => setClosureConfirmed(event.target.checked)}
                    />
                    <span>I confirm these notes describe completed work and actual evidence for this incident.</span>
                  </label>
                  {closureError && <p role="alert" className="text-xs text-rose-300">{closureError}</p>}
                  <button
                    type="submit"
                    disabled={closingIncident}
                    className="rounded bg-emerald-600 px-3 py-2 text-xs font-bold text-white transition-all hover:bg-emerald-500 disabled:opacity-50"
                  >
                    {closingIncident ? 'Recording closure…' : 'Record D8 closure'}
                  </button>
                </form>
              )}
              {selectedIncident.status !== 'D5_PCA_CHOSEN' && selectedIncident.status !== 'D8_CLOSED' && (
                <p className="rounded-lg border border-amber-900 bg-amber-950/30 p-3 text-xs text-amber-200">
                  D8 closure is unavailable until D4 root cause and D5 corrective action are recorded.
                </p>
              )}

              {/* 8D Disciplines Grid */}
              <div className="grid grid-cols-1 md:grid-cols-2 gap-3 text-xs font-mono">
                {/* D1 Team */}
                <div className="p-3 bg-zinc-950 rounded-xl border border-zinc-800 space-y-1">
                  <span className="text-indigo-400 font-bold block">D1: Cross-Functional Team / Agents</span>
                  <div className="text-zinc-300">{selectedIncident.d1_team.join(', ')}</div>
                </div>

                {/* D2 Problem */}
                <div className="p-3 bg-zinc-950 rounded-xl border border-zinc-800 space-y-1">
                  <span className="text-amber-400 font-bold block">D2: Problem Description</span>
                  <div className="text-zinc-300">
                    Impact: ${selectedIncident.d2_problem.financial_impact_usdt || 0} USDT | {selectedIncident.d2_problem.context_summary}
                  </div>
                </div>

                {/* D3 Containment */}
                <div className="p-3 bg-zinc-950 rounded-xl border border-zinc-800 space-y-1">
                  <span className="text-rose-400 font-bold block">D3: Interim Containment Action</span>
                  <div className="text-zinc-300">
                    Action: {selectedIncident.d3_containment.action_name} ({selectedIncident.d3_containment.status})
                  </div>
                </div>

                {/* D5 PCA */}
                <div className="p-3 bg-zinc-950 rounded-xl border border-zinc-800 space-y-1">
                  <span className="text-emerald-400 font-bold block">D5: Permanent Corrective Action (PCA)</span>
                  <div className="text-zinc-300">
                    {selectedIncident.d5_pca.map((p) => p.title).join('; ') || 'Formulating...'}
                  </div>
                </div>
              </div>

              {/* D4: 5-Why Recursive Analysis Tree */}
              {selectedIncident.d4_root_cause && (
                <div className="p-4 bg-zinc-950 rounded-xl border border-indigo-900/60 space-y-2">
                  <span className="text-xs font-bold text-indigo-400 font-mono flex items-center space-x-1.5">
                    <HelpCircle className="w-4 h-4" />
                    <span>D4: 5-Why Root Cause Analysis Tree</span>
                  </span>
                  <div className="space-y-2 font-mono text-xs">
                    {selectedIncident.d4_root_cause.levels.map((lvl) => (
                      <div key={lvl.level} className="p-2 rounded bg-zinc-900/80 border border-zinc-800">
                        <span className="text-indigo-400 font-bold">{lvl.level}: {lvl.question}</span>
                        <p className="text-zinc-300 mt-0.5 pl-2 border-l border-indigo-500/50">{lvl.answer}</p>
                      </div>
                    ))}
                  </div>
                  <div className="p-2.5 rounded bg-indigo-950/40 border border-indigo-800/60 text-xs font-mono">
                    <strong className="text-indigo-300">Root Cause Summary: </strong>
                    <span className="text-zinc-300">{selectedIncident.d4_root_cause.root_cause_summary}</span>
                    <div className="mt-1">
                      <strong className="text-emerald-300">Preventive Insight: </strong>
                      <span className="text-zinc-300">{selectedIncident.d4_root_cause.preventive_insight}</span>
                    </div>
                  </div>
                </div>
              )}
            </div>
          )}
        </div>
      )}

      {/* Tab 3: PDCA Closed-Loop */}
      {activeDeckTab === 'PDCA' && (
        <div className="space-y-4">
          <div className="flex items-center justify-between">
            <h3 className="text-sm font-bold text-zinc-200 flex items-center space-x-2">
              <RefreshCw className="w-4 h-4 text-cyan-400" />
              <span>PDCA (Plan-Do-Check-Act) Strategy Performance & Drift</span>
            </h3>
            <span className="text-xs text-amber-300 font-mono">Process-local evidence · not an execution authority</span>
          </div>

          {Object.keys(pdca).length === 0 ? (
            <div role="status" className="rounded-xl border border-amber-800 bg-amber-950/30 p-5 text-center text-xs text-amber-100">
              INSUFFICIENT SAMPLE · PDCA evidence is unavailable; health and drift status are unknown.
            </div>
          ) : (
          <div className="grid grid-cols-1 md:grid-cols-2 gap-4 font-mono text-xs">
            {(Object.entries(pdca) as [string, PDCACheck][]).map(([strat, c]) => (
              <div key={strat} className="p-4 rounded-xl bg-zinc-950 border border-zinc-800 space-y-3">
                <div className="flex items-center justify-between border-b border-zinc-800 pb-2">
                  <span className="font-bold text-zinc-200 uppercase">{strat.replace('_', ' ')}</span>
                  <span
                    className={`px-2 py-0.5 rounded text-[10px] font-bold ${
                      c.drift_detected === true && c.authoritative ? 'bg-rose-950 text-rose-300 border border-rose-800' : c.authoritative && c.sample_size > 0 && c.drift_detected === false ? 'bg-emerald-950 text-emerald-300 border border-emerald-800' : 'bg-amber-950 text-amber-200 border border-amber-800'
                    }`}
                  >
                    {c.evidence_status === 'INSUFFICIENT_SAMPLE' || c.sample_size === 0
                      ? 'INSUFFICIENT SAMPLE'
                      : c.drift_detected === true
                        ? c.authoritative ? `DRIFT: ${c.drift_severity}` : `UNVERIFIED DRIFT: ${c.drift_severity}`
                        : c.authoritative && c.drift_detected === false ? 'HEALTHY' : 'UNVERIFIED SAMPLE'}
                  </span>
                </div>

                <div className="grid grid-cols-2 gap-2 text-[11px]">
                  <div className="p-2 bg-zinc-900 rounded border border-zinc-800/80">
                    <span className="text-zinc-500 block">WIN RATE (PLAN vs ACTUAL)</span>
                    <span className="text-zinc-200 font-bold">{c.plan_win_rate_pct}% vs {c.authoritative && c.sample_size > 0 ? `${c.actual_win_rate_pct}%` : '—'}</span>
                  </div>
                  <div className="p-2 bg-zinc-900 rounded border border-zinc-800/80">
                    <span className="text-zinc-500 block">EDGE DECAY</span>
                    <span className={`font-bold ${!c.authoritative || c.sample_size === 0 ? 'text-zinc-400' : c.edge_decay_bps > 10 ? 'text-amber-400' : 'text-emerald-400'}`}>
                      {c.authoritative && c.sample_size > 0 ? `${c.edge_decay_bps.toFixed(1)} bps` : '—'}
                    </span>
                  </div>
                  <div className="p-2 bg-zinc-900 rounded border border-zinc-800/80">
                    <span className="text-zinc-500 block">SLIPPAGE (PLAN vs ACTUAL)</span>
                    <span className="text-zinc-200 font-bold">{c.plan_slippage_bps} vs {c.authoritative && c.sample_size > 0 ? `${c.actual_slippage_bps} bps` : '—'}</span>
                  </div>
                  <div className="p-2 bg-zinc-900 rounded border border-zinc-800/80">
                    <span className="text-zinc-500 block">SAMPLE SIZE</span>
                    <span className="text-zinc-200 font-bold">{c.sample_size} process-local trades</span>
                  </div>
                </div>

                <div className="p-2.5 bg-zinc-900/60 rounded border border-zinc-800 space-y-1">
                  <span className="text-cyan-400 font-bold text-[10px] block">ACT RECOMMENDATIONS:</span>
                  <ul className="list-disc pl-4 text-zinc-400 text-[11px] space-y-0.5">
                    {c.recommended_actions.map((act, i) => (
                      <li key={i}>{act}</li>
                    ))}
                  </ul>
                </div>
              </div>
            ))}
          </div>
          )}
        </div>
      )}

      {/* Tab 4: Trade Lineage & Provenance */}
      {activeDeckTab === 'LINEAGE' && (
        <div className="space-y-4">
          <div className="flex items-center justify-between">
            <h3 className="text-sm font-bold text-zinc-200 flex items-center space-x-2">
              <GitCommit className="w-4 h-4 text-emerald-400" />
              <span>Closed-Loop Trade Lineage ({lineages.length})</span>
            </h3>
            <span className="text-xs text-zinc-500 font-mono">
              Market State → Intent → Score → Allocation → Gate → Fill → Learning
            </span>
          </div>

          {lineages.length === 0 ? (
            <div className="p-6 text-center rounded-2xl bg-zinc-900/50 border border-zinc-800 text-xs font-mono text-zinc-500">
              No trade lineages recorded yet. Real-time executions will populate here automatically.
            </div>
          ) : (
            <div className="space-y-2 font-mono text-xs">
              {lineages.map((lin) => (
                <div key={lin.lineage_id} className="p-3 bg-zinc-950 rounded-xl border border-zinc-800 flex flex-wrap items-center justify-between gap-3">
                  <div className="space-y-0.5">
                    <div className="flex items-center space-x-2">
                      <span className="font-bold text-zinc-200">{lin.lineage_id}</span>
                      <span className="text-indigo-400 font-semibold">{lin.symbol}</span>
                      <span className="text-zinc-500">({lin.strategy_id})</span>
                    </div>
                    <div className="text-[11px] text-zinc-400">
                      Entry: ${lin.entry_price} → Exit: ${lin.exit_price || 'N/A'} | Slippage: {lin.slippage_bps} bps
                    </div>
                  </div>

                  <div className="flex items-center space-x-3 text-right">
                    <div>
                      <div className={`font-bold ${parseFloat(lin.net_pnl) >= 0 ? 'text-emerald-400' : 'text-rose-400'}`}>
                        ${parseFloat(lin.net_pnl).toFixed(2)} USDT
                      </div>
                      <div className="text-[10px] text-zinc-500">
                        Edge: {lin.actual_edge_bps} bps (Decay: {lin.edge_decay_bps} bps)
                      </div>
                    </div>

                    <span
                      className={`px-2 py-1 rounded text-[10px] font-bold ${
                        lin.outcome_grade === 'ALPHA'
                          ? 'bg-emerald-950 text-emerald-300 border border-emerald-800'
                          : lin.outcome_grade === 'EXCESS_SLIPPAGE' || lin.outcome_grade === 'UNEXPECTED_LOSS'
                          ? 'bg-rose-950 text-rose-300 border border-rose-800'
                          : 'bg-zinc-800 text-zinc-300'
                      }`}
                    >
                      {lin.outcome_grade}
                    </span>
                  </div>
                </div>
              ))}
            </div>
          )}
        </div>
      )}
    </div>
  );
};
