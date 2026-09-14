import React, { useState, useEffect } from 'react';
import {
  Key,
  Layers,
  Plus,
  Check,
  X,
  Edit3,
  AlertTriangle,
  Eye,
  EyeOff,
  Trash2,
  RefreshCw,
  Save,
  CheckCircle2,
} from 'lucide-react';
import { binanceApi, BinanceKeyStatus, ApiProfile } from '../api/binance';

interface BinanceProfileModalProps {
  isOpen: boolean;
  onClose: () => void;
  onStatusChanged?: (status: BinanceKeyStatus) => void;
}

export const BinanceProfileModal: React.FC<BinanceProfileModalProps> = ({
  isOpen,
  onClose,
  onStatusChanged,
}) => {
  const [binanceStatus, setBinanceStatus] = useState<any>(null);
  const [isVerifyingKey, setIsVerifyingKey] = useState<boolean>(false);
  const [profiles, setProfiles] = useState<ApiProfile[]>([]);
  const [activeProfileId, setActiveProfileId] = useState<string>('default');
  const [isEditing, setIsEditing] = useState<boolean>(false);
  const [isSwitchingProfile, setIsSwitchingProfile] = useState<boolean>(false);
  const [isSavingProfile, setIsSavingProfile] = useState<boolean>(false);
  const [showSecretInput, setShowSecretInput] = useState<boolean>(false);
  const [feedbackMsg, setFeedbackMsg] = useState<{ type: 'success' | 'error'; text: string } | null>(null);

  const [formData, setFormData] = useState({
    id: '',
    name: '',
    apiKey: '',
    apiSecret: '',
    isTestnet: true,
  });

  const fetchBinanceStatus = async () => {
    setIsVerifyingKey(true);
    try {
      const data = await binanceApi.verifyKey();
      setBinanceStatus(data);
      if (onStatusChanged) {
        onStatusChanged(data);
      }
    } catch (err) {
      console.warn('Failed to verify Binance API key:', err);
    } finally {
      setIsVerifyingKey(false);
    }
  };

  const fetchProfiles = async () => {
    try {
      const data = await binanceApi.getProfiles();
      setProfiles(data.profiles || []);
      if (data.activeProfileId) {
        setActiveProfileId(data.activeProfileId);
      }
    } catch (err) {
      console.warn('Failed to fetch Binance profiles:', err);
    }
  };

  useEffect(() => {
    if (isOpen) {
      fetchBinanceStatus();
      fetchProfiles();
    }
  }, [isOpen]);

  useEffect(() => {
    const handleKeyDown = (e: KeyboardEvent) => {
      if (e.key === 'Escape' && isOpen) {
        onClose();
      }
    };
    window.addEventListener('keydown', handleKeyDown);
    return () => window.removeEventListener('keydown', handleKeyDown);
  }, [isOpen, onClose]);

  if (!isOpen) return null;

  const handleSwitchProfile = async (profileId: string) => {
    setIsSwitchingProfile(true);
    setFeedbackMsg(null);
    try {
      const data = await binanceApi.switchProfile(profileId);
      setActiveProfileId(data.activeProfileId);
      setBinanceStatus(data.results);
      if (onStatusChanged && data.results) {
        onStatusChanged(data.results);
      }
      await fetchProfiles();
      setFeedbackMsg({ type: 'success', text: `Switched to ${data.activeProfileName}` });
    } catch (err: any) {
      setFeedbackMsg({ type: 'error', text: err.message || 'Failed to switch profile' });
    } finally {
      setIsSwitchingProfile(false);
    }
  };

  const handleOpenEdit = (profile?: ApiProfile) => {
    if (profile) {
      setFormData({
        id: profile.id,
        name: profile.name,
        apiKey: '',
        apiSecret: '',
        isTestnet: profile.environment === 'TESTNET',
      });
    } else {
      setFormData({
        id: '',
        name: `Binance Key ${profiles.length + 1}`,
        apiKey: '',
        apiSecret: '',
        isTestnet: true,
      });
    }
    setShowSecretInput(false);
    setIsEditing(true);
    setFeedbackMsg(null);
  };

  const handleSaveProfile = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!formData.name.trim()) {
      setFeedbackMsg({ type: 'error', text: 'Please enter a profile name' });
      return;
    }
    setIsSavingProfile(true);
    setFeedbackMsg(null);
    try {
      const data = await binanceApi.saveProfile({
        id: formData.id || undefined,
        name: formData.name,
        isTestnet: true,
        apiKey: formData.apiKey,
        apiSecret: formData.apiSecret,
      });
      setBinanceStatus(data.results);
      if (onStatusChanged && data.results) {
        onStatusChanged(data.results);
      }
      setActiveProfileId(data.activeProfileId);
      await fetchProfiles();
      setIsEditing(false);
      setFeedbackMsg({ type: 'success', text: 'Testnet profile saved. Worker authentication and reconciliation are still required for execution readiness.' });
    } catch (err: any) {
      setFeedbackMsg({ type: 'error', text: err.message || 'Failed to save profile' });
    } finally {
      setIsSavingProfile(false);
    }
  };

  const handleDeleteProfile = async (profileId: string) => {
    if (!window.confirm('Delete this API Key profile?')) return;
    try {
      await binanceApi.deleteProfile(profileId);
      await fetchProfiles();
      await fetchBinanceStatus();
      setFeedbackMsg({ type: 'success', text: 'Profile removed' });
    } catch (err: any) {
      setFeedbackMsg({ type: 'error', text: err.message || 'Failed to delete' });
    }
  };

  return (
    <div
      id="binance-diagnostics-backdrop"
      onClick={onClose}
      className="fixed inset-0 bg-black/85 z-[9999] flex items-center justify-center p-4 cursor-pointer overflow-y-auto"
    >
      <div
        id="binance-diagnostics-modal"
        onClick={(e) => e.stopPropagation()}
        className="bg-zinc-900 border border-zinc-800 rounded-xl max-w-lg w-full p-5 shadow-2xl space-y-4 cursor-default relative z-[10000] my-auto max-h-[88vh] flex flex-col"
      >
        {/* Modal Title & Close */}
        <div className="flex items-center justify-between pb-3 border-b border-zinc-800 shrink-0">
          <div className="flex items-center space-x-2">
            <Key className="w-4 h-4 text-emerald-400" />
            <h3 className="text-sm font-semibold text-zinc-100">Binance API Key Management & Diagnostics</h3>
          </div>
          <button
            id="btn-close-binance-modal-top"
            type="button"
            onClick={onClose}
            className="text-zinc-400 hover:text-zinc-100 p-1.5 rounded-lg hover:bg-zinc-800 transition-colors flex items-center justify-center cursor-pointer"
            aria-label="Close modal"
            title="Close (Esc)"
          >
            <X className="w-5 h-5" />
          </button>
        </div>

        {/* Profile Switcher Tabs */}
        <div className="shrink-0 space-y-1.5">
          <div className="flex items-center justify-between text-[11px] text-zinc-400">
            <span className="flex items-center space-x-1.5 font-medium">
              <Layers className="w-3.5 h-3.5 text-zinc-500" />
              <span>Select Active API Key Profile:</span>
            </span>
            {!isEditing && (
              <button
                type="button"
                onClick={() => handleOpenEdit()}
                className="flex items-center space-x-1 text-emerald-400 hover:text-emerald-300 font-medium cursor-pointer"
              >
                <Plus className="w-3.5 h-3.5" />
                <span>Add New Key</span>
              </button>
            )}
          </div>

          {/* Profiles Pills */}
          <div className="flex flex-wrap gap-1.5">
            {profiles.map((p) => {
              const isCurrent = p.id === activeProfileId;
              const isTestnet = p.environment === 'TESTNET';
              return (
                <button
                  key={p.id}
                  type="button"
                  onClick={() => {
                    if (!isCurrent) handleSwitchProfile(p.id);
                  }}
                  disabled={isSwitchingProfile || !isTestnet}
                  className={`flex items-center space-x-1.5 px-2.5 py-1.5 rounded-lg border text-xs transition-all cursor-pointer ${
                      isCurrent && isTestnet
                        ? 'bg-emerald-950/60 border-emerald-500/50 text-emerald-200 shadow-sm'
                        : !isTestnet
                          ? 'bg-rose-950/30 border-rose-900/60 text-rose-300 cursor-not-allowed'
                          : 'bg-zinc-950 border-zinc-800 text-zinc-400 hover:border-zinc-700 hover:text-zinc-200'
                  }`}
                >
                  {isCurrent && isTestnet ? (
                    <Check className="w-3 h-3 text-emerald-400 shrink-0" />
                  ) : (
                    <Key className="w-3 h-3 text-zinc-500 shrink-0" />
                  )}
                  <span className="font-medium truncate max-w-[140px]">{p.name}</span>
                  <span className="text-[10px] font-mono opacity-70">({p.maskedApiKey || 'Key'})</span>
                  <span
                    className={`text-[9px] px-1 py-0.2 rounded font-mono ${
                      isTestnet ? 'bg-amber-950/80 text-amber-300' : 'bg-rose-950/80 text-rose-300'
                    }`}
                  >
                    {isTestnet ? 'Testnet' : 'Mainnet Blocked'}
                  </span>
                </button>
              );
            })}
          </div>
        </div>

        {/* Feedback Message */}
        {feedbackMsg && (
          <div
            className={`p-2.5 rounded-lg text-xs border flex items-center justify-between ${
              feedbackMsg.type === 'success'
                ? 'bg-emerald-950/60 border-emerald-800/60 text-emerald-300'
                : 'bg-rose-950/60 border-rose-800/60 text-rose-300'
            }`}
          >
            <span>{feedbackMsg.text}</span>
            <button
              type="button"
              onClick={() => setFeedbackMsg(null)}
              className="text-zinc-400 hover:text-zinc-200 p-0.5 cursor-pointer"
            >
              <X className="w-3.5 h-3.5" />
            </button>
          </div>
        )}

        {/* Modal Body */}
        <div className="overflow-y-auto pr-1 space-y-3.5 flex-1 text-xs">
          {isEditing ? (
            /* EDIT / ADD PROFILE FORM */
            <form onSubmit={handleSaveProfile} className="p-4 bg-zinc-950 rounded-xl border border-zinc-800 space-y-3.5">
              <div className="flex items-center justify-between border-b border-zinc-800/80 pb-2.5">
                <span className="font-semibold text-zinc-200 text-sm flex items-center space-x-1.5">
                  <Edit3 className="w-4 h-4 text-emerald-400" />
                  <span>{formData.id ? 'Edit API Credentials' : 'Add Binance API Key'}</span>
                </span>
                <span className="text-[11px] text-zinc-500">Secure Server Proxy</span>
              </div>

              {/* Profile Name */}
              <div className="space-y-1">
                <label className="block text-[11px] font-medium text-zinc-300">Profile Name</label>
                <input
                  type="text"
                  value={formData.name}
                  onChange={(e) => setFormData({ ...formData, name: e.target.value })}
                  placeholder="e.g., Binance Testnet Primary"
                  required
                  className="w-full px-3 py-2 bg-zinc-900 border border-zinc-800 rounded-lg text-zinc-100 placeholder-zinc-500 focus:outline-none focus:border-emerald-500/70 text-xs"
                />
              </div>

              {/* Network Switcher */}
              <div className="space-y-1">
                <label className="block text-[11px] font-medium text-zinc-300">Trading Network</label>
                <div className="grid grid-cols-2 gap-2">
                  <button
                    type="button"
                    disabled
                    className="py-2 px-3 rounded-lg border text-xs font-medium flex items-center justify-center space-x-1.5 cursor-not-allowed bg-zinc-950 border-zinc-800 text-zinc-600"
                  >
                    <span>Binance Mainnet (Blocked)</span>
                  </button>
                  <button
                    type="button"
                    onClick={() => setFormData({ ...formData, isTestnet: true })}
                    className={`py-2 px-3 rounded-lg border text-xs font-medium flex items-center justify-center space-x-1.5 cursor-pointer ${
                      formData.isTestnet
                        ? 'bg-amber-950/60 border-amber-500/60 text-amber-300'
                        : 'bg-zinc-900 border-zinc-800 text-zinc-400 hover:text-zinc-200'
                    }`}
                  >
                    <AlertTriangle className="w-3.5 h-3.5" />
                    <span>Binance Testnet (Demo)</span>
                  </button>
                </div>
              </div>

              {/* API Key */}
              <div className="space-y-1">
                <div className="flex items-center justify-between">
                  <label className="block text-[11px] font-medium text-zinc-300">Binance API Key</label>
                  <span className="text-[10px] text-zinc-500">64 characters</span>
                </div>
                <input
                  type="text"
                  value={formData.apiKey}
                  onChange={(e) => setFormData({ ...formData, apiKey: e.target.value })}
                  placeholder={
                    formData.id ? 'Leave blank to keep existing key, or enter new key' : 'Paste 64-char API key'
                  }
                  className="w-full px-3 py-2 bg-zinc-900 border border-zinc-800 rounded-lg text-zinc-100 placeholder-zinc-500 font-mono focus:outline-none focus:border-emerald-500/70 text-xs"
                />
              </div>

              {/* API Secret */}
              <div className="space-y-1">
                <div className="flex items-center justify-between">
                  <label className="block text-[11px] font-medium text-zinc-300">Binance API Secret</label>
                  <button
                    type="button"
                    onClick={() => setShowSecretInput(!showSecretInput)}
                    className="text-[11px] text-zinc-400 hover:text-zinc-200 flex items-center space-x-1 cursor-pointer"
                  >
                    {showSecretInput ? <EyeOff className="w-3 h-3" /> : <Eye className="w-3 h-3" />}
                    <span>{showSecretInput ? 'Hide' : 'Show'}</span>
                  </button>
                </div>
                <input
                  type={showSecretInput ? 'text' : 'password'}
                  value={formData.apiSecret}
                  onChange={(e) => setFormData({ ...formData, apiSecret: e.target.value })}
                  placeholder={
                    formData.id ? 'Leave blank to keep existing secret, or enter new secret' : 'Paste 64-char secret key'
                  }
                  className="w-full px-3 py-2 bg-zinc-900 border border-zinc-800 rounded-lg text-zinc-100 placeholder-zinc-500 font-mono focus:outline-none focus:border-emerald-500/70 text-xs"
                />
              </div>

              {/* Security Invariants */}
              <div className="p-2.5 bg-zinc-900 border border-zinc-800 rounded-lg text-zinc-400 text-[11px] space-y-1">
                <span className="font-semibold text-zinc-300 block">Security Invariants:</span>
                <ul className="list-disc list-inside space-y-0.5">
                  <li>Credentials stay on server memory / env only and are never exposed in browser devtools.</li>
                  <li>Never check "Enable Withdrawals" in Binance API Management.</li>
                </ul>
              </div>

              {/* Form Action Buttons */}
              <div className="flex items-center justify-between pt-2 border-t border-zinc-800/80">
                <div>
                  {formData.id && profiles.length > 1 && (
                    <button
                      type="button"
                      onClick={() => handleDeleteProfile(formData.id)}
                      className="flex items-center space-x-1 text-rose-400 hover:text-rose-300 text-xs py-1 px-2 rounded hover:bg-rose-950/40 transition-colors cursor-pointer"
                    >
                      <Trash2 className="w-3.5 h-3.5" />
                      <span>Delete Profile</span>
                    </button>
                  )}
                </div>
                <div className="flex items-center space-x-2">
                  <button
                    type="button"
                    onClick={() => setIsEditing(false)}
                    className="px-3 py-1.5 bg-zinc-800 hover:bg-zinc-700 text-zinc-300 rounded-md text-xs font-medium cursor-pointer"
                  >
                    Cancel
                  </button>
                  <button
                    type="submit"
                    disabled={isSavingProfile}
                    className="flex items-center space-x-1.5 px-3.5 py-1.5 bg-emerald-600 hover:bg-emerald-500 text-white rounded-md text-xs font-medium transition-colors cursor-pointer shadow-sm"
                  >
                    {isSavingProfile ? (
                      <RefreshCw className="w-3.5 h-3.5 animate-spin" />
                    ) : (
                      <Save className="w-3.5 h-3.5" />
                    )}
                    <span>Save & Verify Key</span>
                  </button>
                </div>
              </div>
            </form>
          ) : binanceStatus ? (
            /* DIAGNOSTICS VIEW */
            <>
              {/* Configuration Status Card */}
              <div className="p-3 bg-zinc-950 rounded-lg border border-zinc-800/80 flex items-center justify-between">
                <div>
                  <div className="flex items-center space-x-2">
                    <span className="text-zinc-400 block text-[11px]">Active API Key</span>
                    <span
                      className={`px-1.5 py-0.2 rounded text-[10px] font-mono ${
                        binanceStatus.isTestnet === true
                          ? 'bg-amber-950 text-amber-400 border border-amber-800/50'
                          : 'bg-rose-950 text-rose-300 border border-rose-800/50'
                      }`}
                    >
                      {binanceStatus.isTestnet === true ? 'Testnet Sandbox' : 'MAINNET BLOCKED'}
                    </span>
                  </div>
                  <span className="font-mono text-zinc-200 font-medium">{binanceStatus.maskedKey || 'None'}</span>
                </div>

                <div className="flex items-center space-x-2">
                  <button
                    type="button"
                    onClick={() => {
                      const active = profiles.find((p) => p.id === activeProfileId);
                      handleOpenEdit(active);
                    }}
                    className="flex items-center space-x-1 px-2.5 py-1 bg-zinc-800 hover:bg-zinc-700 text-zinc-200 border border-zinc-700/80 rounded text-xs font-medium cursor-pointer transition-colors"
                    title="Edit key, secret, or network"
                  >
                    <Edit3 className="w-3.5 h-3.5 text-emerald-400" />
                    <span>Edit Key</span>
                  </button>
                </div>
              </div>

              {/* Spot Check */}
              <div className="p-3 bg-zinc-950 rounded-lg border border-zinc-800/80 space-y-1">
                <div className="flex items-center justify-between">
                  <span className="font-semibold text-zinc-200">Binance Spot API</span>
                  {binanceStatus.isTestnet === true && binanceStatus.spot?.authenticated ? (
                    <span className="text-emerald-400 flex items-center space-x-1 font-medium">
                      <CheckCircle2 className="w-3.5 h-3.5" />
                      <span>Authenticated (Trade: {String(binanceStatus.spot.canTrade)})</span>
                    </span>
                  ) : (
                    <span className="text-rose-400 flex items-center space-x-1 font-medium">
                      <AlertTriangle className="w-3.5 h-3.5" />
                      <span>Failed</span>
                    </span>
                  )}
                </div>
                <p className="text-zinc-400">{binanceStatus.spot?.message}</p>
              </div>

              {/* Futures Check */}
              <div className="p-3 bg-zinc-950 rounded-lg border border-zinc-800/80 space-y-2">
                <div className="flex items-center justify-between">
                  <span className="font-semibold text-zinc-200">Binance USDⓈ-M Futures API</span>
                  {binanceStatus.isTestnet === true && binanceStatus.futures?.authenticated ? (
                    <span className="text-emerald-400 flex items-center space-x-1 font-medium">
                      <CheckCircle2 className="w-3.5 h-3.5" />
                      <span>Authenticated ({binanceStatus.futures.message})</span>
                    </span>
                  ) : (
                    <span className="text-amber-400 flex items-center space-x-1 font-medium">
                      <AlertTriangle className="w-3.5 h-3.5" />
                      <span>Action Required</span>
                    </span>
                  )}
                </div>
                <p className="text-zinc-400">{binanceStatus.futures?.message}</p>

                {!binanceStatus.futures?.authenticated && (
                  <div className="p-2.5 bg-amber-950/40 border border-amber-800/50 rounded text-amber-200/90 space-y-1">
                    <span className="font-semibold block text-amber-300">How to Enable Futures on Binance:</span>
                    <ol className="list-decimal list-inside space-y-0.5 text-zinc-300">
                      <li>Go to Binance &gt; <strong>API Management</strong>.</li>
                      <li>Click <strong>Edit Restrictions</strong> on this API Key.</li>
                      <li>Check the box for <strong>"Enable Futures"</strong> and save.</li>
                    </ol>
                  </div>
                )}
              </div>
            </>
          ) : (
            <div className="py-6 text-center text-zinc-400">Querying Binance endpoints...</div>
          )}
        </div>

        {/* Bottom Footer Actions */}
        <div className="flex items-center justify-between pt-3 border-t border-zinc-800 shrink-0">
          <span className="text-[11px] text-zinc-500">Press Esc or click outside to dismiss.</span>
          <div className="flex items-center space-x-2">
            {!isEditing && (
              <button
                type="button"
                onClick={() => {
                  const active = profiles.find((p) => p.id === activeProfileId);
                  handleOpenEdit(active);
                }}
                className="flex items-center space-x-1.5 px-3 py-1.5 bg-zinc-800 hover:bg-zinc-700 text-zinc-200 rounded-md text-xs font-medium transition-colors cursor-pointer"
              >
                <Edit3 className="w-3.5 h-3.5 text-emerald-400" />
                <span>Edit Key</span>
              </button>
            )}
            <button
              id="btn-retest-binance-key"
              type="button"
              onClick={fetchBinanceStatus}
              disabled={isVerifyingKey || isSwitchingProfile}
              className="flex items-center space-x-1.5 px-3 py-1.5 bg-zinc-800 hover:bg-zinc-700 text-zinc-200 rounded-md text-xs font-medium transition-colors cursor-pointer"
            >
              <RefreshCw className={`w-3.5 h-3.5 ${isVerifyingKey || isSwitchingProfile ? 'animate-spin' : ''}`} />
              <span>Re-test Key</span>
            </button>
            <button
              id="btn-close-binance-modal-bottom"
              type="button"
              onClick={onClose}
              className="px-4 py-1.5 bg-zinc-800 hover:bg-zinc-700 text-zinc-200 rounded-md text-xs font-medium transition-colors cursor-pointer"
            >
              Close
            </button>
          </div>
        </div>
      </div>
    </div>
  );
};
