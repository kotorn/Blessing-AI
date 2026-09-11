import React, { useState } from 'react';
import { Bot, Sparkles, Send, ShieldAlert, CheckCircle, Cpu } from 'lucide-react';

export const AIQuantCopilot: React.FC = () => {
  const [prompt, setPrompt] = useState('');
  const [selectedTask, setSelectedTask] = useState('ANALYZE_DAILY_SUMMARY');
  const [isLoading, setIsLoading] = useState(false);
  const [response, setResponse] = useState<string | null>(null);

  const presets = [
    { id: 'ANALYZE_DAILY_SUMMARY', label: 'Daily Trading Audit', prompt: 'Audit today\'s basket performance, total funding drag, and grid expansion frequency across BTC and ETH.' },
    { id: 'DIAGNOSE_RECOVERY', label: 'Diagnose Basket Recovery', prompt: 'Explain why Basket #BASKET-BTC-001 entered RECOVERY state and whether dynamic ATR spacing mitigated adverse excursion.' },
    { id: 'STRESS_TEST_SCENARIOS', label: 'Design Stress Scenarios', prompt: 'Propose 3 synthetic black-swan stress test scenarios with simultaneous funding spike and basis dislocation.' },
    { id: 'AUDIT_RISK_INCIDENTS', label: 'Audit Risk Governance', prompt: 'Review current portfolio margin utilization (14.2%) and effective leverage (0.85x) against hard drawdown escalation tiers.' },
  ];

  const handleRunTask = async (taskPrompt: string) => {
    setIsLoading(true);
    setResponse(null);
    try {
      const resp = await fetch('/api/quant/ai/research', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
        body: JSON.stringify({
          task: selectedTask,
          prompt: taskPrompt,
          query: taskPrompt,
          context: {
            portfolio_equity: 50720.50,
            active_baskets: 2,
            risk_state: 'NORMAL',
            effective_leverage: 0.85,
            drawdown_pct: 1.15,
          },
        }),
      });
      const contentType = resp.headers.get('content-type');
      if (contentType && contentType.includes('application/json')) {
        const data = await resp.json();
        setResponse(data.analysis || data.error || 'No response returned');
      } else {
        const text = await resp.text();
        setResponse(text.slice(0, 300) || 'Quant engine processing completed.');
      }
    } catch (e) {
      console.warn('AI Copilot request failed:', e);
      setResponse('Failed to communicate with Quant Research Engine.');
    } finally {
      setIsLoading(false);
    }
  };

  return (
    <div className="space-y-5">
      <div className="bg-zinc-900/80 border border-zinc-800 rounded-xl p-5 space-y-4">
        <div className="flex flex-wrap items-center justify-between gap-3 border-b border-zinc-800 pb-3">
          <div className="flex items-center space-x-3">
            <div className="p-2 rounded-lg bg-indigo-500/10 text-indigo-400">
              <Bot className="w-5 h-5" />
            </div>
            <div>
              <h2 className="text-base font-bold text-zinc-100 flex items-center space-x-2">
                <span>AI Quant Research Copilot (Section 33)</span>
                <span className="text-[11px] px-2 py-0.5 rounded bg-indigo-500/20 text-indigo-300 font-mono">Gemini Flash</span>
              </h2>
              <p className="text-xs text-zinc-400">Post-trade analytics, regime explainability & scenario generation</p>
            </div>
          </div>

          <div className="flex items-center space-x-2 text-xs bg-zinc-950/80 px-3 py-1.5 rounded-lg border border-zinc-800">
            <ShieldAlert className="w-3.5 h-3.5 text-amber-400" />
            <span className="text-zinc-300">Mandate: Zero live order execution authority. Research only.</span>
          </div>
        </div>

        {/* Task Presets */}
        <div className="space-y-2">
          <label className="text-xs font-semibold text-zinc-400 uppercase tracking-wider">
            Quant Research Workflows
          </label>
          <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-2">
            {presets.map((p) => (
              <button
                key={p.id}
                onClick={() => {
                  setSelectedTask(p.id);
                  setPrompt(p.prompt);
                  handleRunTask(p.prompt);
                }}
                className={`p-3 rounded-lg border text-left text-xs transition-all ${
                  selectedTask === p.id
                    ? 'bg-indigo-950/40 border-indigo-500 text-white'
                    : 'bg-zinc-950/40 border-zinc-800 hover:border-zinc-700 text-zinc-400'
                }`}
              >
                <div className="font-semibold text-zinc-200">{p.label}</div>
                <div className="text-[11px] text-zinc-400 mt-1 line-clamp-2">{p.prompt}</div>
              </button>
            ))}
          </div>
        </div>

        {/* Interactive Prompt Input */}
        <div className="space-y-2 pt-2">
          <div className="flex items-center space-x-2">
            <input
              type="text"
              value={prompt}
              onChange={(e) => setPrompt(e.target.value)}
              placeholder="Ask Quant Copilot (e.g. 'Analyze sensitivity to ATR multipliers in R2 Weak Trend')..."
              className="flex-1 bg-zinc-950/80 border border-zinc-800 rounded-lg px-3.5 py-2.5 text-xs text-zinc-200 placeholder-zinc-500 focus:outline-none focus:border-indigo-500"
              onKeyDown={(e) => e.key === 'Enter' && prompt && handleRunTask(prompt)}
            />
            <button
              onClick={() => prompt && handleRunTask(prompt)}
              disabled={isLoading || !prompt}
              className="flex items-center space-x-1.5 px-4 py-2.5 rounded-lg bg-indigo-600 hover:bg-indigo-500 text-white text-xs font-semibold shadow-lg shadow-indigo-600/20 disabled:opacity-50"
            >
              {isLoading ? <Cpu className="w-4 h-4 animate-spin" /> : <Send className="w-4 h-4" />}
              <span>{isLoading ? 'Synthesizing...' : 'Analyze'}</span>
            </button>
          </div>
        </div>
      </div>

      {/* Response Panel */}
      {response && (
        <div className="bg-zinc-900/80 border border-zinc-800 rounded-xl p-5 space-y-3 animate-fade-in">
          <div className="flex items-center justify-between border-b border-zinc-800 pb-2">
            <div className="flex items-center space-x-2 text-xs font-semibold text-zinc-300">
              <Sparkles className="w-4 h-4 text-indigo-400" />
              <span>Quant Research Synthesis</span>
            </div>
            <span className="text-[11px] font-mono text-zinc-400">Model: Gemini 2.5 Flash</span>
          </div>
          <div className="text-xs text-zinc-300 whitespace-pre-wrap font-mono leading-relaxed bg-zinc-950/60 p-4 rounded-lg border border-zinc-800/80">
            {response}
          </div>
        </div>
      )}
    </div>
  );
};
