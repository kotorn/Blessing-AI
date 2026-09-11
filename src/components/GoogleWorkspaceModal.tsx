import React, { useState, useEffect } from 'react';
import {
  FileSpreadsheet,
  FolderSync,
  ExternalLink,
  Trash2,
  RefreshCw,
  X,
  CheckCircle2,
  AlertCircle,
  FileText,
  Clock,
  Download,
  Share2,
} from 'lucide-react';
import { useAuth } from '../context/AuthContext';
import { GoogleDriveFile, SheetExportResult } from '../lib/workspace';
import { ConfirmationModal } from './ConfirmationModal';

interface GoogleWorkspaceModalProps {
  isOpen: boolean;
  onClose: () => void;
  baskets: any[];
  portfolioState: any;
}

export const GoogleWorkspaceModal: React.FC<GoogleWorkspaceModalProps> = ({
  isOpen,
  onClose,
  baskets,
  portfolioState,
}) => {
  const {
    user,
    accessToken,
    signIn,
    exportToGoogleSheet,
    fetchDriveFiles,
    deleteDriveFile,
  } = useAuth();

  const [activeTab, setActiveTab] = useState<'export' | 'drive'>('export');
  const [isExporting, setIsExporting] = useState<boolean>(false);
  const [exportResult, setExportResult] = useState<SheetExportResult | null>(null);
  const [exportError, setExportError] = useState<string | null>(null);

  const [driveFiles, setDriveFiles] = useState<GoogleDriveFile[]>([]);
  const [isLoadingFiles, setIsLoadingFiles] = useState<boolean>(false);
  const [fileError, setFileError] = useState<string | null>(null);

  const [fileToDelete, setFileToDelete] = useState<GoogleDriveFile | null>(null);
  const [isDeleting, setIsDeleting] = useState(false);

  // Load drive files whenever modal opens or tab switches to 'drive'
  useEffect(() => {
    if (isOpen && user && activeTab === 'drive') {
      loadFiles();
    }
  }, [isOpen, user, activeTab]);

  // Handle escape key
  useEffect(() => {
    const handleKeyDown = (e: KeyboardEvent) => {
      if (e.key === 'Escape') {
        onClose();
      }
    };
    if (isOpen && !fileToDelete) {
      window.addEventListener('keydown', handleKeyDown);
    }
    return () => window.removeEventListener('keydown', handleKeyDown);
  }, [isOpen, onClose]);

  const loadFiles = async () => {
    setIsLoadingFiles(true);
    setFileError(null);
    try {
      const files = await fetchDriveFiles();
      setDriveFiles(files);
    } catch (err: any) {
      setFileError(err.message || 'Failed to load files from Google Drive');
    } finally {
      setIsLoadingFiles(false);
    }
  };

  const handleExport = async () => {
    if (!user) {
      await signIn();
      return;
    }
    setIsExporting(true);
    setExportError(null);
    setExportResult(null);
    try {
      const res = await exportToGoogleSheet(baskets, portfolioState);
      setExportResult(res);
      // Also refresh files in Drive
      fetchDriveFiles().then(setDriveFiles).catch(() => {});
    } catch (err: any) {
      setExportError(err.message || 'Failed to export to Google Sheets');
    } finally {
      setIsExporting(false);
    }
  };

  const confirmDeleteFile = async () => {
    if (!fileToDelete) return;
    setIsDeleting(true);
    try {
      const success = await deleteDriveFile(fileToDelete.id, fileToDelete.name);
      if (success) {
        setDriveFiles((prev) => prev.filter((f) => f.id !== fileToDelete.id));
      }
    } catch (err: any) {
      alert(`Error deleting file: ${err.message}`);
    } finally {
      setIsDeleting(false);
      setFileToDelete(null);
    }
  };

  if (!isOpen) return null;

  return (
    <div
      id="workspace-modal-backdrop"
      onClick={onClose}
      className="fixed inset-0 bg-black/85 z-[9999] flex items-center justify-center p-4 cursor-pointer overflow-y-auto"
    >
      <div
        id="workspace-modal-content"
        onClick={(e) => e.stopPropagation()}
        className="bg-zinc-900 border border-zinc-800 rounded-xl max-w-xl w-full p-5 shadow-2xl space-y-4 cursor-default relative z-[10000] my-auto max-h-[88vh] flex flex-col"
      >
        {/* Modal Header */}
        <div className="flex items-center justify-between pb-3 border-b border-zinc-800 shrink-0">
          <div className="flex items-center space-x-2.5">
            <div className="w-8 h-8 rounded-lg bg-emerald-950/80 border border-emerald-500/40 flex items-center justify-center text-emerald-400">
              <FileSpreadsheet className="w-4 h-4" />
            </div>
            <div>
              <h3 className="text-sm font-semibold text-zinc-100 flex items-center space-x-1.5">
                <span>Google Drive & Sheets Integration</span>
              </h3>
              <p className="text-[11px] text-zinc-400">
                Direct quantitative reporting and telemetry export to your Google Workspace
              </p>
            </div>
          </div>
          <button
            type="button"
            onClick={onClose}
            className="text-zinc-400 hover:text-zinc-100 p-1.5 rounded-lg hover:bg-zinc-800 transition-colors"
            title="Close (Esc)"
          >
            <X className="w-5 h-5" />
          </button>
        </div>

        {/* Auth Status Banner */}
        <div className="p-3 bg-zinc-950 rounded-lg border border-zinc-800 flex items-center justify-between shrink-0">
          <div className="flex items-center space-x-2.5">
            <div
              className={`w-2.5 h-2.5 rounded-full ${
                user ? 'bg-emerald-400 shadow-sm shadow-emerald-400/50' : 'bg-amber-400'
              }`}
            />
            <div>
              <span className="text-xs font-medium text-zinc-200 block">
                {user ? `Connected as ${user.email}` : 'Google Workspace Not Connected'}
              </span>
              <span className="text-[10px] text-zinc-500">
                {user
                  ? 'Authorized for Google Drive file management & Google Sheets creation'
                  : 'Sign in to export portfolio audits and explore stored sheets'}
              </span>
            </div>
          </div>

          {!user && (
            <button
              type="button"
              onClick={signIn}
              className="flex items-center space-x-1.5 px-3 py-1.5 bg-zinc-800 hover:bg-zinc-700 text-zinc-200 border border-zinc-700 rounded-lg text-xs font-medium transition-colors"
            >
              <span>Sign in with Google</span>
            </button>
          )}
        </div>

        {/* Navigation Tabs */}
        <div className="flex items-center space-x-2 border-b border-zinc-800/80 pb-2 shrink-0">
          <button
            type="button"
            onClick={() => setActiveTab('export')}
            className={`flex items-center space-x-1.5 px-3 py-1.5 rounded-md text-xs font-medium transition-colors ${
              activeTab === 'export'
                ? 'bg-zinc-800 text-white shadow-sm'
                : 'text-zinc-400 hover:text-zinc-200 hover:bg-zinc-800/40'
            }`}
          >
            <Download className="w-3.5 h-3.5" />
            <span>Export to Google Sheets</span>
          </button>
          <button
            type="button"
            onClick={() => setActiveTab('drive')}
            className={`flex items-center space-x-1.5 px-3 py-1.5 rounded-md text-xs font-medium transition-colors ${
              activeTab === 'drive'
                ? 'bg-zinc-800 text-white shadow-sm'
                : 'text-zinc-400 hover:text-zinc-200 hover:bg-zinc-800/40'
            }`}
          >
            <FolderSync className="w-3.5 h-3.5" />
            <span>Google Drive Files</span>
          </button>
        </div>

        {/* Tab Content */}
        <div className="overflow-y-auto pr-1 flex-1 space-y-3.5 text-xs">
          {activeTab === 'export' ? (
            <div className="space-y-4">
              <div className="p-3.5 bg-zinc-950 rounded-xl border border-zinc-800 space-y-2.5">
                <span className="font-semibold text-zinc-200 block text-xs">
                  Automated Quantitative Export
                </span>
                <p className="text-zinc-400 text-[11px] leading-relaxed">
                  Generate a structured Google Spreadsheet with two dedicated tabs:
                </p>
                <div className="grid grid-cols-2 gap-2 text-[11px]">
                  <div className="p-2.5 rounded-lg bg-zinc-900 border border-zinc-800">
                    <span className="font-medium text-emerald-400 block mb-0.5">
                      Tab 1: Portfolio Summary
                    </span>
                    <span className="text-zinc-400 text-[10px]">
                      Equity, margin utilization, effective leverage, aggregate long/short exposure, and risk state.
                    </span>
                  </div>
                  <div className="p-2.5 rounded-lg bg-zinc-900 border border-zinc-800">
                    <span className="font-medium text-cyan-400 block mb-0.5">
                      Tab 2: Active Baskets
                    </span>
                    <span className="text-zinc-400 text-[10px]">
                      {baskets.length} active baskets (Instrument, Direction, Grid Depth, Size, Entry, Mark, PnL).
                    </span>
                  </div>
                </div>
              </div>

              {/* Error Message */}
              {exportError && (
                <div className="p-3 rounded-lg bg-rose-950/50 border border-rose-800/50 text-rose-300 flex items-start space-x-2">
                  <AlertCircle className="w-4 h-4 shrink-0 mt-0.5" />
                  <span>{exportError}</span>
                </div>
              )}

              {/* Success Result */}
              {exportResult && (
                <div className="p-3.5 rounded-xl bg-emerald-950/40 border border-emerald-800/60 space-y-2 text-zinc-200">
                  <div className="flex items-center space-x-2 text-emerald-400 font-medium">
                    <CheckCircle2 className="w-4 h-4" />
                    <span>Google Spreadsheet Created Successfully!</span>
                  </div>
                  <p className="text-xs text-zinc-300 font-medium">{exportResult.title}</p>
                  <div className="pt-1">
                    <a
                      href={exportResult.spreadsheetUrl}
                      target="_blank"
                      rel="noopener noreferrer"
                      className="inline-flex items-center space-x-1.5 px-3.5 py-1.5 bg-emerald-600 hover:bg-emerald-500 text-white rounded-md text-xs font-medium shadow-sm transition-colors"
                    >
                      <span>Open in Google Sheets</span>
                      <ExternalLink className="w-3.5 h-3.5" />
                    </a>
                  </div>
                </div>
              )}

              {/* Action Button */}
              <div className="pt-2">
                <button
                  type="button"
                  onClick={handleExport}
                  disabled={isExporting}
                  className="w-full py-2.5 px-4 bg-emerald-600 hover:bg-emerald-500 disabled:opacity-50 text-white rounded-lg text-xs font-semibold flex items-center justify-center space-x-2 shadow-lg shadow-emerald-900/30 transition-all cursor-pointer"
                >
                  {isExporting ? (
                    <>
                      <RefreshCw className="w-4 h-4 animate-spin" />
                      <span>Writing to Google Sheets API...</span>
                    </>
                  ) : (
                    <>
                      <Share2 className="w-4 h-4" />
                      <span>
                        {user ? 'Export Live Audit to Google Sheets' : 'Sign in & Export to Google Sheets'}
                      </span>
                    </>
                  )}
                </button>
              </div>
            </div>
          ) : (
            /* DRIVE EXPLORER TAB */
            <div className="space-y-3">
              <div className="flex items-center justify-between">
                <span className="text-zinc-400 text-xs">
                  Recent Quant Sheets in Google Drive ({driveFiles.length})
                </span>
                <button
                  type="button"
                  onClick={loadFiles}
                  disabled={isLoadingFiles}
                  className="flex items-center space-x-1 text-emerald-400 hover:text-emerald-300 text-xs cursor-pointer"
                >
                  <RefreshCw className={`w-3.5 h-3.5 ${isLoadingFiles ? 'animate-spin' : ''}`} />
                  <span>Refresh</span>
                </button>
              </div>

              {fileError && (
                <div className="p-2.5 rounded-lg bg-rose-950/50 border border-rose-800/50 text-rose-300 text-xs flex items-center space-x-2">
                  <AlertCircle className="w-4 h-4 shrink-0" />
                  <span>{fileError}</span>
                </div>
              )}

              {isLoadingFiles ? (
                <div className="py-8 text-center text-zinc-500 space-y-2">
                  <RefreshCw className="w-5 h-5 animate-spin mx-auto text-zinc-400" />
                  <p>Searching Google Drive for Blessing reports...</p>
                </div>
              ) : driveFiles.length === 0 ? (
                <div className="py-8 text-center text-zinc-500 bg-zinc-950 rounded-xl border border-zinc-800 space-y-2">
                  <FileText className="w-6 h-6 mx-auto text-zinc-600" />
                  <p>No Blessing AI spreadsheets found in your Drive.</p>
                  <button
                    type="button"
                    onClick={() => setActiveTab('export')}
                    className="text-xs text-emerald-400 hover:underline"
                  >
                    Click here to create your first export
                  </button>
                </div>
              ) : (
                <div className="space-y-2">
                  {driveFiles.map((file) => (
                    <div
                      key={file.id}
                      className="p-3 bg-zinc-950 rounded-lg border border-zinc-800/80 flex items-center justify-between hover:border-zinc-700 transition-colors"
                    >
                      <div className="flex items-center space-x-2.5 min-w-0 pr-2">
                        <FileSpreadsheet className="w-4 h-4 text-emerald-400 shrink-0" />
                        <div className="min-w-0">
                          <span className="font-medium text-zinc-200 block truncate text-xs">
                            {file.name}
                          </span>
                          <span className="text-[10px] text-zinc-500 flex items-center space-x-1">
                            <Clock className="w-2.5 h-2.5 inline" />
                            <span>
                              {file.modifiedTime ? new Date(file.modifiedTime).toLocaleString() : 'Unknown date'}
                            </span>
                          </span>
                        </div>
                      </div>

                      <div className="flex items-center space-x-2 shrink-0">
                        {file.webViewLink && (
                          <a
                            href={file.webViewLink}
                            target="_blank"
                            rel="noopener noreferrer"
                            className="p-1.5 text-zinc-400 hover:text-emerald-400 rounded hover:bg-zinc-800 transition-colors"
                            title="Open in Google Drive"
                          >
                            <ExternalLink className="w-4 h-4" />
                          </a>
                        )}
                        <button
                          type="button"
                          onClick={() => setFileToDelete(file)}
                          className="p-1.5 text-zinc-400 hover:text-rose-400 rounded hover:bg-rose-950/40 transition-colors"
                          title="Delete file from Google Drive"
                        >
                          <Trash2 className="w-4 h-4" />
                        </button>
                      </div>
                    </div>
                  ))}
                </div>
              )}
            </div>
          )}
        </div>

        {/* Modal Footer */}
        <div className="flex items-center justify-between pt-3 border-t border-zinc-800 shrink-0">
          <span className="text-[11px] text-zinc-500">
            Powered by Google Drive & Sheets API
          </span>
          <button
            type="button"
            onClick={onClose}
            className="px-4 py-1.5 bg-zinc-800 hover:bg-zinc-700 text-zinc-200 rounded-md text-xs font-medium transition-colors"
          >
            Close
          </button>
        </div>
      </div>

      <ConfirmationModal
        isOpen={Boolean(fileToDelete)}
        title="Delete Google Drive File"
        message={
          <>
            Are you sure you want to permanently delete <strong>{fileToDelete?.name}</strong>? This will remove the file from your Google Drive and cannot be undone.
          </>
        }
        confirmText="Delete File"
        isDestructive={true}
        isLoading={isDeleting}
        onConfirm={confirmDeleteFile}
        onCancel={() => setFileToDelete(null)}
      />
    </div>
  );
};
