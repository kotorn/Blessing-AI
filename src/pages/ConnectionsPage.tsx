import React from 'react';
import {
  Key,
  Cloud,
  FileSpreadsheet,
  ExternalLink,
  Server,
} from 'lucide-react';
import { BinanceKeyStatus } from '../api/binance';
import { useAuth } from '../context/AuthContext';

interface ConnectionsPageProps {
  binanceStatus?: BinanceKeyStatus | null;
  onOpenBinanceModal: () => void;
  onOpenGoogleCloudModal: () => void;
  onOpenWorkspaceModal: () => void;
}

export const ConnectionsPage: React.FC<ConnectionsPageProps> = ({
  binanceStatus,
  onOpenBinanceModal,
  onOpenGoogleCloudModal,
  onOpenWorkspaceModal,
}) => {
  const { user, firestoreConnected } = useAuth();
  const testnetFuturesAuthenticated = Boolean(
    binanceStatus?.isTestnet === true && binanceStatus?.futures?.authenticated,
  );

  return (
    <div className="space-y-6">
      {/* Header */}
      <div>
        <h2 className="text-xl font-bold text-zinc-100 flex items-center space-x-2">
          <Server className="w-5 h-5 text-cyan-400" />
          <span>System & Exchange Connections</span>
        </h2>
        <p className="text-xs text-zinc-400 mt-1">
          Manage exchange API credentials, Google Cloud SaaS telemetry, and Workspace analytical sync.
        </p>
      </div>

      <div className="grid grid-cols-1 md:grid-cols-3 gap-6">
        {/* Binance Global Connection Card */}
        <div className="bg-zinc-900/90 border border-zinc-800 rounded-xl p-5 flex flex-col justify-between space-y-4 hover:border-zinc-700 transition-colors">
          <div className="space-y-3">
            <div className="flex items-center justify-between">
              <div className="p-2 rounded-lg bg-amber-950/60 border border-amber-800/60 text-amber-400">
                <Key className="w-5 h-5" />
              </div>
              <span
                className={`px-2 py-0.5 rounded text-[10px] font-bold border ${
                  testnetFuturesAuthenticated
                    ? 'bg-emerald-950 text-emerald-300 border-emerald-800'
                    : 'bg-zinc-800 text-zinc-400 border-zinc-700'
                }`}
              >
                {testnetFuturesAuthenticated ? 'CONNECTED' : 'DISCONNECTED'}
              </span>
            </div>

            <div>
              <h3 className="text-sm font-bold text-zinc-100">Binance USDⓈ-M Testnet API</h3>
              <p className="text-xs text-zinc-400 mt-1">
                Spot & USDⓈ-M Futures trading keys, multi-profile credentials, and Hedge Mode detection.
              </p>
            </div>

            <div className="text-xs space-y-1 text-zinc-400 font-mono bg-zinc-950 p-2.5 rounded-lg border border-zinc-800/80">
              <div className="flex justify-between">
                <span>Spot Market:</span>
                <span className={binanceStatus?.spot?.authenticated && binanceStatus?.isTestnet ? 'text-emerald-400' : 'text-zinc-500'}>
                  {binanceStatus?.spot?.authenticated && binanceStatus?.isTestnet ? 'Authenticated (read-only probe)' : 'Pending'}
                </span>
              </div>
              <div className="flex justify-between">
                <span>USDⓈ-M Futures:</span>
                <span className={testnetFuturesAuthenticated ? 'text-emerald-400' : 'text-zinc-500'}>
                  {testnetFuturesAuthenticated ? 'Authenticated (read-only probe)' : 'Pending'}
                </span>
              </div>
              <div className="flex justify-between">
                <span>Key Source:</span>
                <span className="text-cyan-400">{binanceStatus?.environment === 'MAINNET' ? 'BINANCE_MAINNET' : 'BINANCE_TESTNET'}</span>
              </div>
            </div>
          </div>

          <button
            type="button"
            onClick={onOpenBinanceModal}
            className="w-full py-2 px-3 rounded-lg text-xs font-semibold bg-amber-600 hover:bg-amber-500 text-white transition-colors cursor-pointer flex items-center justify-center space-x-1.5"
          >
            <span>Manage Binance Profiles & Keys</span>
            <ExternalLink className="w-3.5 h-3.5" />
          </button>
        </div>

        {/* Google Cloud Center Card */}
        <div className="bg-zinc-900/90 border border-zinc-800 rounded-xl p-5 flex flex-col justify-between space-y-4 hover:border-zinc-700 transition-colors">
          <div className="space-y-3">
            <div className="flex items-center justify-between">
              <div className="p-2 rounded-lg bg-cyan-950/60 border border-cyan-800/60 text-cyan-400">
                <Cloud className="w-5 h-5" />
              </div>
              <span
                className={`px-2 py-0.5 rounded text-[10px] font-bold border ${
                  firestoreConnected
                    ? 'bg-cyan-950 text-cyan-300 border-cyan-800'
                    : 'bg-zinc-800 text-zinc-400 border-zinc-700'
                }`}
              >
                {firestoreConnected ? 'FIRESTORE CONNECTED' : 'READ-BACK REQUIRED'}
              </span>
            </div>

            <div>
              <h3 className="text-sm font-bold text-zinc-100">Google Cloud Platform</h3>
              <p className="text-xs text-zinc-400 mt-1">
                Cloud SQL PostgreSQL, Firestore audit persistence, BigQuery lakehouse, and Cloud Run daemon.
              </p>
            </div>

            <div className="text-xs space-y-1 text-zinc-400 font-mono bg-zinc-950 p-2.5 rounded-lg border border-zinc-800/80">
              <div className="flex justify-between">
                <span>Firestore DB:</span>
                <span className={firestoreConnected ? 'text-emerald-400' : 'text-zinc-500'}>
                  {firestoreConnected ? 'Synchronized' : 'Offline Mode'}
                </span>
              </div>
              <div className="flex justify-between">
                <span>BigQuery Dataset:</span>
                <span className="text-cyan-400">market_data · signals · risk · backtests</span>
              </div>
              <div className="flex justify-between">
                <span>Cloud Run Service:</span>
                <span className="text-amber-400">Target declared · not verified</span>
              </div>
            </div>
          </div>

          <button
            type="button"
            onClick={onOpenGoogleCloudModal}
            className="w-full py-2 px-3 rounded-lg text-xs font-semibold bg-cyan-600 hover:bg-cyan-500 text-white transition-colors cursor-pointer flex items-center justify-center space-x-1.5"
          >
            <span>Open Google Cloud Center</span>
            <ExternalLink className="w-3.5 h-3.5" />
          </button>
        </div>

        {/* Google Workspace Integration Card */}
        <div className="bg-zinc-900/90 border border-zinc-800 rounded-xl p-5 flex flex-col justify-between space-y-4 hover:border-zinc-700 transition-colors">
          <div className="space-y-3">
            <div className="flex items-center justify-between">
              <div className="p-2 rounded-lg bg-emerald-950/60 border border-emerald-800/60 text-emerald-400">
                <FileSpreadsheet className="w-5 h-5" />
              </div>
              <span className="px-2 py-0.5 rounded text-[10px] font-bold border bg-zinc-800 text-zinc-300 border-zinc-700">
                {user ? 'AUTHENTICATED' : 'SIGN-IN REQUIRED'}
              </span>
            </div>

            <div>
              <h3 className="text-sm font-bold text-zinc-100">Google Workspace (Sheets / Drive)</h3>
              <p className="text-xs text-zinc-400 mt-1">
                Real-time trade telemetry streaming directly to Google Sheets and portfolio snapshots in Drive.
              </p>
            </div>

            <div className="text-xs space-y-1 text-zinc-400 font-mono bg-zinc-950 p-2.5 rounded-lg border border-zinc-800/80">
              <div className="flex justify-between">
                <span>Operator Account:</span>
                <span className="text-zinc-200 truncate max-w-[150px]">{user?.email || 'Not connected'}</span>
              </div>
              <div className="flex justify-between">
                <span>Sheets Telemetry:</span>
                <span className="text-cyan-400">Supported</span>
              </div>
              <div className="flex justify-between">
                <span>Drive Snapshots:</span>
                <span className="text-cyan-400">Supported</span>
              </div>
            </div>
          </div>

          <button
            type="button"
            onClick={onOpenWorkspaceModal}
            className="w-full py-2 px-3 rounded-lg text-xs font-semibold bg-emerald-600 hover:bg-emerald-500 text-white transition-colors cursor-pointer flex items-center justify-center space-x-1.5"
          >
            <span>Open Google Workspace Tools</span>
            <ExternalLink className="w-3.5 h-3.5" />
          </button>
        </div>
      </div>
    </div>
  );
};
