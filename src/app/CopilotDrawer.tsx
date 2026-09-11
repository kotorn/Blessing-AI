import React from 'react';
import { X, Bot, Maximize2, Minimize2 } from 'lucide-react';
import { AIQuantCopilot } from '../components/AIQuantCopilot';

interface CopilotDrawerProps {
  isOpen: boolean;
  onClose: () => void;
}

export const CopilotDrawer: React.FC<CopilotDrawerProps> = ({ isOpen, onClose }) => {
  const [isExpanded, setIsExpanded] = React.useState(false);

  if (!isOpen) return null;

  return (
    <div className="fixed inset-y-0 right-0 z-40 flex shadow-2xl animate-slideLeft">
      {/* Backdrop for smaller screens */}
      <div
        className="fixed inset-0 bg-black/40 backdrop-blur-[2px] lg:hidden"
        onClick={onClose}
      />

      <div
        className={`relative z-10 bg-zinc-950 border-l border-zinc-800 flex flex-col h-full transition-all duration-200 ${
          isExpanded ? 'w-screen lg:w-[850px]' : 'w-full sm:w-[480px] lg:w-[520px]'
        }`}
      >
        {/* Drawer Header */}
        <div className="px-4 py-3 border-b border-zinc-800 bg-zinc-900/90 flex items-center justify-between shrink-0">
          <div className="flex items-center space-x-2">
            <div className="p-1.5 rounded-lg bg-cyan-950/80 border border-cyan-800/60 text-cyan-400">
              <Bot className="w-4 h-4" />
            </div>
            <div>
              <div className="flex items-center space-x-2">
                <span className="text-sm font-bold text-zinc-100">Quant Copilot</span>
                <span className="px-1.5 py-0.5 rounded text-[9px] font-bold bg-cyan-950 text-cyan-300 border border-cyan-800/60">
                  ASSISTANT
                </span>
              </div>
              <p className="text-[10px] text-zinc-400">Gemini 2.5 • Read-Only Risk & Strategy Analysis</p>
            </div>
          </div>

          <div className="flex items-center space-x-1.5">
            <button
              type="button"
              onClick={() => setIsExpanded(!isExpanded)}
              className="p-1.5 rounded-lg text-zinc-400 hover:text-zinc-200 hover:bg-zinc-800 transition-colors cursor-pointer"
              title={isExpanded ? 'Restore width' : 'Expand drawer width'}
            >
              {isExpanded ? <Minimize2 className="w-4 h-4" /> : <Maximize2 className="w-4 h-4" />}
            </button>
            <button
              type="button"
              onClick={onClose}
              className="p-1.5 rounded-lg text-zinc-400 hover:text-rose-400 hover:bg-zinc-800 transition-colors cursor-pointer"
              title="Close drawer"
            >
              <X className="w-4 h-4" />
            </button>
          </div>
        </div>

        {/* Drawer Content */}
        <div className="flex-1 overflow-y-auto p-3 sm:p-4">
          <AIQuantCopilot />
        </div>
      </div>
    </div>
  );
};
