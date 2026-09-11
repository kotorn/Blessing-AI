import React from 'react';
import { AlertTriangle, X, Check } from 'lucide-react';

interface ConfirmationModalProps {
  isOpen: boolean;
  title: string;
  message: React.ReactNode;
  confirmText?: string;
  cancelText?: string;
  isDestructive?: boolean;
  requireTypedConfirmation?: string;
  onConfirm: () => void;
  onCancel: () => void;
  isLoading?: boolean;
}

export const ConfirmationModal: React.FC<ConfirmationModalProps> = ({
  isOpen,
  title,
  message,
  confirmText = 'Confirm',
  cancelText = 'Cancel',
  isDestructive = true,
  requireTypedConfirmation,
  onConfirm,
  onCancel,
  isLoading = false,
}) => {
  const [typedValue, setTypedValue] = React.useState('');

  if (!isOpen) return null;

  const isConfirmDisabled = 
    isLoading || 
    (requireTypedConfirmation && typedValue !== requireTypedConfirmation);

  return (
    <div className="fixed inset-0 z-[100] flex items-center justify-center bg-black/70 backdrop-blur-sm p-4">
      <div className="bg-zinc-900 border border-zinc-800 rounded-xl w-full max-w-md shadow-2xl overflow-hidden flex flex-col">
        <div className={`p-4 border-b ${isDestructive ? 'border-rose-900/30 bg-rose-950/10' : 'border-zinc-800 bg-zinc-950/50'} flex items-center justify-between`}>
          <div className="flex items-center space-x-2">
            {isDestructive ? (
              <AlertTriangle className="w-5 h-5 text-rose-500" />
            ) : (
              <AlertTriangle className="w-5 h-5 text-amber-500" />
            )}
            <h3 className={`font-bold ${isDestructive ? 'text-rose-400' : 'text-zinc-100'}`}>
              {title}
            </h3>
          </div>
          <button 
            onClick={onCancel}
            disabled={isLoading}
            className="text-zinc-500 hover:text-zinc-300 transition-colors"
          >
            <X className="w-5 h-5" />
          </button>
        </div>

        <div className="p-5 space-y-4">
          <div className="text-sm text-zinc-300">
            {message}
          </div>

          {requireTypedConfirmation && (
            <div className="space-y-2 mt-4">
              <label className="text-xs font-semibold text-zinc-400">
                Type <span className="text-zinc-200 font-mono select-all bg-zinc-800 px-1 py-0.5 rounded">{requireTypedConfirmation}</span> to confirm:
              </label>
              <input
                type="text"
                value={typedValue}
                onChange={(e) => setTypedValue(e.target.value)}
                placeholder={requireTypedConfirmation}
                className="w-full bg-zinc-950 border border-zinc-700 rounded-lg px-3 py-2 text-sm text-zinc-100 focus:outline-none focus:border-indigo-500 focus:ring-1 focus:ring-indigo-500 font-mono"
                autoComplete="off"
                disabled={isLoading}
              />
            </div>
          )}
        </div>

        <div className="p-4 border-t border-zinc-800 bg-zinc-950 flex justify-end space-x-3">
          <button
            onClick={onCancel}
            disabled={isLoading}
            className="px-4 py-2 rounded-lg text-sm font-semibold text-zinc-400 hover:text-zinc-200 transition-colors disabled:opacity-50"
          >
            {cancelText}
          </button>
          <button
            onClick={() => {
              onConfirm();
              setTypedValue('');
            }}
            disabled={Boolean(isConfirmDisabled)}
            className={`flex items-center space-x-2 px-4 py-2 rounded-lg text-sm font-bold transition-all disabled:opacity-50 disabled:cursor-not-allowed ${
              isDestructive
                ? 'bg-rose-600 hover:bg-rose-500 text-white shadow-lg shadow-rose-900/20'
                : 'bg-indigo-600 hover:bg-indigo-500 text-white shadow-lg shadow-indigo-900/20'
            }`}
          >
            {isLoading ? (
              <span className="animate-spin rounded-full h-4 w-4 border-2 border-white/20 border-t-white" />
            ) : (
              <Check className="w-4 h-4" />
            )}
            <span>{isLoading ? 'Processing...' : confirmText}</span>
          </button>
        </div>
      </div>
    </div>
  );
};
