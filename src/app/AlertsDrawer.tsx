import React, { useState } from 'react';
import { X, Bell, ShieldAlert, Activity, DollarSign,  Trash2, Filter, CheckCircle2, AlertTriangle } from 'lucide-react';
import { SystemAlert } from '../types';

interface AlertsDrawerProps {
  isOpen: boolean;
  onClose: () => void;
  alerts: SystemAlert[];
  onDismiss: (alertId: string) => void;
  onClearAll: () => void;
}

export const AlertsDrawer: React.FC<AlertsDrawerProps> = ({
  isOpen,
  onClose,
  alerts,
  onDismiss,
  onClearAll,
}) => {
  const [filterSeverity, setFilterSeverity] = useState<string>('ALL');

  if (!isOpen) return null;

  const filteredAlerts = alerts.filter((a) => {
    if (filterSeverity !== 'ALL' && a.severity !== filterSeverity) return false;
    return true;
  });

  return (
    <div className="fixed inset-y-0 right-0 z-40 flex shadow-2xl animate-slideLeft">
      {/* Backdrop */}
      <div
        className="fixed inset-0 bg-black/40 backdrop-blur-[2px]"
        onClick={onClose}
      />

      <div className="relative z-10 bg-zinc-950 border-l border-zinc-800 flex flex-col h-full w-full sm:w-[480px]">
        {/* Header */}
        <div className="px-4 py-3 border-b border-zinc-800 bg-zinc-900/90 flex items-center justify-between shrink-0">
          <div className="flex items-center space-x-2">
            <div className="p-1.5 rounded-lg bg-indigo-950/80 border border-indigo-800/60 text-indigo-400">
              <Bell className="w-4 h-4" />
            </div>
            <div>
              <h2 className="text-sm font-bold text-zinc-100">Alerts Center</h2>
              <p className="text-[10px] text-zinc-400 font-mono">
                System notifications & anomalies
              </p>
            </div>
          </div>
          <button
            onClick={onClose}
            className="p-1.5 text-zinc-400 hover:text-zinc-100 hover:bg-zinc-800 rounded-md transition-colors cursor-pointer"
          >
            <X className="w-4 h-4" />
          </button>
        </div>

        {/* Filters & Actions */}
        <div className="px-4 py-2 border-b border-zinc-800 bg-zinc-950 flex flex-wrap items-center justify-between gap-2 shrink-0">
          <div className="flex items-center space-x-2 text-xs">
            <Filter className="w-3.5 h-3.5 text-zinc-500" />
            <select
              value={filterSeverity}
              onChange={(e) => setFilterSeverity(e.target.value)}
              className="px-2 py-1 bg-zinc-900 border border-zinc-700 rounded text-zinc-300 focus:outline-none cursor-pointer"
            >
              <option value="ALL">All Severities</option>
              <option value="CRITICAL">Critical</option>
              <option value="WARNING">Warning</option>
              <option value="INFO">Info</option>
            </select>
          </div>
          <button
            onClick={onClearAll}
            disabled={alerts.length === 0}
            className="flex items-center space-x-1 px-2.5 py-1 text-[11px] font-semibold text-zinc-400 hover:text-zinc-200 disabled:opacity-50 transition-colors cursor-pointer"
          >
            <Trash2 className="w-3 h-3" />
            <span>Clear All</span>
          </button>
        </div>

        {/* Content */}
        <div className="flex-1 overflow-y-auto p-3 space-y-2">
          {filteredAlerts.length === 0 ? (
            <div className="h-full flex flex-col items-center justify-center text-zinc-500 space-y-2 p-8">
              <CheckCircle2 className="w-8 h-8 opacity-50" />
              <p className="text-sm font-semibold">No active alerts</p>
              <p className="text-xs text-center max-w-sm">
                The system is operating within normal parameters.
              </p>
            </div>
          ) : (
            filteredAlerts.map((alert) => (
              <div
                key={alert.id}
                className={`relative p-3 rounded-xl border flex gap-3 ${
                  alert.severity === 'CRITICAL'
                    ? 'bg-rose-950/20 border-rose-800/40'
                    : alert.severity === 'WARNING'
                    ? 'bg-amber-950/20 border-amber-800/40'
                    : 'bg-zinc-900/40 border-zinc-800'
                }`}
              >
                <div className="shrink-0 mt-0.5">
                  {alert.severity === 'CRITICAL' ? (
                    <ShieldAlert className="w-4 h-4 text-rose-400" />
                  ) : alert.severity === 'WARNING' ? (
                    <AlertTriangle className="w-4 h-4 text-amber-400" />
                  ) : alert.type === 'FUNDING' ? (
                    <DollarSign className="w-4 h-4 text-emerald-400" />
                  ) : (
                    <Activity className="w-4 h-4 text-cyan-400" />
                  )}
                </div>
                <div className="flex-1 min-w-0 space-y-1">
                  <div className="flex items-start justify-between gap-2">
                    <h4 className="text-[13px] font-bold text-zinc-200">
                      {alert.title}
                    </h4>
                    <span className="text-[10px] text-zinc-500 font-mono whitespace-nowrap">
                      {new Date(alert.timestamp).toLocaleTimeString()}
                    </span>
                  </div>
                  <p className="text-[11px] text-zinc-400 break-words whitespace-pre-wrap leading-relaxed">
                    {alert.message}
                  </p>
                  {alert.symbol && (
                    <span className="inline-block mt-1 px-1.5 py-0.5 rounded text-[10px] font-bold font-mono bg-zinc-800 text-zinc-300 border border-zinc-700">
                      {alert.symbol}
                    </span>
                  )}
                </div>
                <button
                  onClick={() => onDismiss(alert.id)}
                  className="absolute top-2 right-2 p-1 text-zinc-500 hover:text-zinc-300 opacity-0 group-hover:opacity-100 md:opacity-100 transition-opacity cursor-pointer"
                  title="Dismiss alert"
                >
                  <X className="w-3 h-3" />
                </button>
              </div>
            ))
          )}
        </div>
      </div>
    </div>
  );
};
