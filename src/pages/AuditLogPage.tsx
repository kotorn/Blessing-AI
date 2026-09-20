import React, { useState, useEffect } from 'react';
import { Terminal, ShieldAlert, CheckCircle2,  RefreshCw, Loader2, AlertCircle } from 'lucide-react';
import { useAuth } from '../context/AuthContext';

export const AuditLogPage: React.FC = () => {
  const { user, firestoreConnected, fetchAuditLogs } = useAuth();
  
  const [logs, setLogs] = useState<any[]>([]);
  const [isLoading, setIsLoading] = useState<boolean>(true);
  const [error, setError] = useState<string | null>(null);

  const loadLogs = async () => {
    setIsLoading(true);
    setError(null);
    try {
      const fetchedLogs = await fetchAuditLogs();
      setLogs(fetchedLogs);
    } catch  {
      setError('Failed to fetch audit logs.');
    } finally {
      setIsLoading(false);
    }
  };

  useEffect(() => {
    if (firestoreConnected && user) {
      loadLogs();
    } else {
      setIsLoading(false);
    }
  }, [firestoreConnected, user]);

  const filteredLogs = logs.filter((_log) => {
    // If we wanted to parse severity from action string or details, we could.
    // By default, let's say ALL severity means show all.
    // Same for source. We'll implement basic text matching if needed.
    return true;
  });

  return (
    <div className="space-y-6 h-full flex flex-col">
      <div className="flex flex-wrap items-center justify-between gap-3 shrink-0">
        <div>
          <h2 className="text-xl font-bold text-zinc-100 flex items-center space-x-2">
            <Terminal className="w-5 h-5 text-cyan-400" />
            <span>Audit & Operational Decision Log</span>
          </h2>
          <p className="text-xs text-zinc-400 mt-1">
            Immutable timeline of risk decisions, operator overrides, basket adjustments, and exchange syncs.
          </p>
        </div>

        <div className="flex items-center space-x-3">
          <button
            onClick={loadLogs}
            disabled={isLoading || !firestoreConnected}
            className="flex items-center space-x-1.5 px-3 py-1.5 rounded-lg text-xs font-semibold bg-zinc-800 hover:bg-zinc-700 text-zinc-300 transition-colors disabled:opacity-50"
          >
            <RefreshCw className={`w-3.5 h-3.5 ${isLoading ? 'animate-spin' : ''}`} />
            <span>Refresh</span>
          </button>
          
          <div className="flex items-center space-x-2 text-xs">
            <span className="text-zinc-500">Cloud Sync:</span>
            <span
              className={`px-2 py-0.5 rounded font-mono font-bold text-[10px] ${
                firestoreConnected
                  ? 'bg-cyan-950 text-cyan-400 border border-cyan-800'
                  : 'bg-zinc-800 text-zinc-400 border border-zinc-700'
              }`}
            >
              {firestoreConnected ? 'FIRESTORE ACTIVE' : 'OFFLINE'}
            </span>
          </div>
        </div>
      </div>

      {/* Log Feed */}
      <div className="bg-zinc-900/90 border border-zinc-800 rounded-xl flex flex-col flex-1 min-h-[400px]">
        <div className="p-3.5 bg-zinc-950 border-b border-zinc-800 flex items-center justify-between text-xs font-mono text-zinc-400 shrink-0">
          <div className="flex items-center space-x-4">
            <span>TIMESTAMP</span>
          </div>
          <span>ACTION / EVENT DETAILS</span>
        </div>

        <div className="flex-1 overflow-y-auto min-h-0">
          {!user ? (
            <div className="h-full flex flex-col items-center justify-center text-zinc-500 space-y-2 p-8">
              <ShieldAlert className="w-8 h-8 opacity-50" />
              <p className="text-sm font-semibold">Authentication Required</p>
              <p className="text-xs text-center max-w-sm">
                You must sign in to view your secure audit logs.
              </p>
            </div>
          ) : isLoading ? (
            <div className="h-full flex flex-col items-center justify-center text-zinc-500 space-y-2 p-8">
              <Loader2 className="w-8 h-8 animate-spin opacity-50" />
              <p className="text-sm font-semibold">Loading Telemetry...</p>
            </div>
          ) : error ? (
            <div className="h-full flex flex-col items-center justify-center text-rose-500 space-y-2 p-8">
              <AlertCircle className="w-8 h-8 opacity-50" />
              <p className="text-sm font-semibold">Query Failed</p>
              <p className="text-xs text-center max-w-sm text-rose-400/80">{error}</p>
            </div>
          ) : filteredLogs.length === 0 ? (
            <div className="h-full flex flex-col items-center justify-center text-zinc-500 space-y-2 p-8">
              <CheckCircle2 className="w-8 h-8 opacity-50" />
              <p className="text-sm font-semibold">No Audit Logs Found</p>
              <p className="text-xs text-center max-w-sm">
                Events will appear here as the system executes trades and configuration changes.
              </p>
            </div>
          ) : (
            <div className="divide-y divide-zinc-800/80 font-mono text-xs">
              {filteredLogs.map((log) => {
                const date = log.timestamp ? new Date(log.timestamp) : new Date();
                
                return (
                  <div key={log.logId} className="p-4 flex items-start gap-4 hover:bg-zinc-800/30 transition-colors">
                    <div className="space-y-1 shrink-0 w-32">
                      <div className="text-zinc-400 text-[11px]">
                        {date.toLocaleDateString()}
                      </div>
                      <div className="text-zinc-300 font-bold">
                        {date.toLocaleTimeString()}
                      </div>
                    </div>

                    <div className="flex-1 space-y-1.5 min-w-0">
                      <div className="flex items-center flex-wrap gap-2">
                        <span className="px-2 py-0.5 rounded text-[10px] font-bold bg-zinc-800 text-cyan-300 border border-zinc-700">
                          {log.action}
                        </span>
                        {log.symbol && (
                          <span className="px-2 py-0.5 rounded text-[10px] font-bold bg-indigo-950 text-indigo-400 border border-indigo-800/60">
                            {log.symbol}
                          </span>
                        )}
                        <span className="px-2 py-0.5 rounded text-[10px] font-bold bg-zinc-900 text-zinc-500 border border-zinc-800">
                          ID: {log.logId}
                        </span>
                      </div>
                      <p className="text-zinc-400 text-xs font-sans break-words whitespace-pre-wrap">
                        {log.details || 'No additional details provided.'}
                      </p>
                    </div>
                  </div>
                );
              })}
            </div>
          )}
        </div>
      </div>
    </div>
  );
};
