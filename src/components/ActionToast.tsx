import React, { useEffect } from 'react';
import { AlertTriangle, CheckCircle2, X } from 'lucide-react';

export interface ActionFeedback {
  type: 'error' | 'success';
  text: string;
}

interface ActionToastProps {
  feedback: ActionFeedback | null;
  onDismiss: () => void;
}

/**
 * Transient feedback toast for system-level actions (kill switch, pause risk,
 * engine arm/disarm). Safety-critical controls must never fail silently: any
 * rejected mutation is surfaced here until dismissed or auto-cleared.
 */
export const ActionToast: React.FC<ActionToastProps> = ({ feedback, onDismiss }) => {
  useEffect(() => {
    if (!feedback) return;
    const timer = window.setTimeout(onDismiss, 7000);
    return () => window.clearTimeout(timer);
  }, [feedback, onDismiss]);

  if (!feedback) return null;

  return (
    <div
      role="status"
      aria-live="polite"
      className={`fixed bottom-4 right-4 z-[200] max-w-sm w-full sm:w-auto flex items-start space-x-2.5 px-4 py-3 rounded-xl border shadow-2xl backdrop-blur-sm text-xs font-medium ${
        feedback.type === 'error'
          ? 'bg-rose-950/95 border-rose-800/70 text-rose-200 shadow-rose-950/40'
          : 'bg-emerald-950/95 border-emerald-800/70 text-emerald-200 shadow-emerald-950/40'
      }`}
    >
      {feedback.type === 'error' ? (
        <AlertTriangle className="w-4 h-4 mt-0.5 shrink-0 text-rose-400" />
      ) : (
        <CheckCircle2 className="w-4 h-4 mt-0.5 shrink-0 text-emerald-400" />
      )}
      <span className="flex-1 leading-relaxed">{feedback.text}</span>
      <button
        type="button"
        onClick={onDismiss}
        className="text-zinc-400 hover:text-zinc-100 transition-colors shrink-0"
        aria-label="Dismiss notification"
      >
        <X className="w-4 h-4" />
      </button>
    </div>
  );
};
