import React, { useState } from 'react';
import { Terminal, ShieldAlert, CheckCircle2, Filter, RefreshCw } from 'lucide-react';
import { useAuth } from '../context/AuthContext';

export const AuditLogPage: React.FC = () => {
  const { user, firestoreConnected } = useAuth();
  const [filterSeverity, setFilterSeverity] = useState<string>('ALL');

  const sampleLogs = [
    {
      id: 'evt-001',
      timestamp: new Date().toLocaleTimeString(),
      severity: 'ACTION',
      source: 'Operator',
      action: 'POLL_QUANT_STATE',
      details: 'Authoritative state poll received from /api/quant/state (status: OK)',
    },
    {
      id: 'evt-002',
      timestamp: new Date(Date.now() - 30000).toLocaleTimeString(),
      severity: 'INFO',
      source: 'RiskGovernor',
      action: 'MARGIN_CHECK',
      details: 'Margin utilization verified at 14.2% (Threshold: 25.0%). Hard limits PASS',
    },
    {
      id: 'evt-003',
      timestamp: new Date(Date.now() - 120000).toLocaleTimeString(),
      severity: 'INFO',
      source: 'BinanceAdapter',
      action: 'DISCOVERY_CAPABILITIES',
      details: 'Spot and USDⓈ-M exchange capabilities verified for BTCUSDT and ETHUSDT',
    },
  ];

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h2 className="text-xl font-bold text-zinc-100 flex items-center space-x-2">
            <Terminal className="w-5 h-5 text-cyan-400" />
            <span>Audit & Operational Decision Log</span>
          </h2>
          <p className="text-xs text-zinc-400 mt-1">
            Immutable timeline of risk decisions, operator overrides, basket adjustments, and exchange syncs.
          </p>
        </div>

        <div className="flex items-center space-x-2 text-xs">
          <span className="text-zinc-500">Cloud Sync:</span>
          <span
            className={`px-2 py-0.5 rounded font-mono font-bold text-[10px] ${
              firestoreConnected
                ? 'bg-cyan-950 text-cyan-400 border border-cyan-800'
                : 'bg-zinc-800 text-zinc-400'
            }`}
          >
            {firestoreConnected ? 'FIRESTORE ACTIVE' : 'LOCAL CACHE'}
          </span>
        </div>
      </div>

      {/* Log Feed */}
      <div className="bg-zinc-900/90 border border-zinc-800 rounded-xl overflow-hidden">
        <div className="p-3.5 bg-zinc-950 border-b border-zinc-800 flex items-center justify-between text-xs font-mono text-zinc-400">
          <span>TIMESTAMP / SOURCE</span>
          <span>ACTION / EVENT DETAILS</span>
        </div>

        <div className="divide-y divide-zinc-800/80 font-mono text-xs">
          {sampleLogs.map((log) => (
            <div key={log.id} className="p-3 flex items-start justify-between gap-4 hover:bg-zinc-800/30 transition-colors">
              <div className="space-y-0.5 shrink-0">
                <div className="text-zinc-400 text-[11px]">{log.timestamp}</div>
                <div className="text-zinc-300 font-bold">{log.source}</div>
              </div>

              <div className="flex-1 space-y-1">
                <div className="flex items-center space-x-2">
                  <span className="px-1.5 py-0.2 rounded text-[10px] font-bold bg-zinc-800 text-cyan-300 border border-zinc-700">
                    {log.action}
                  </span>
                  <span className="px-1.5 py-0.2 rounded text-[10px] font-bold bg-emerald-950 text-emerald-400 border border-emerald-800/60">
                    {log.severity}
                  </span>
                </div>
                <p className="text-zinc-400 text-xs font-sans">{log.details}</p>
              </div>
            </div>
          ))}
        </div>
      </div>
    </div>
  );
};
