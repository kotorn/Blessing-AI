import React from 'react';
import { ArchitectureReviewViewer } from '../components/ArchitectureReviewViewer';
import { Sliders } from 'lucide-react';

export const SettingsPage: React.FC = () => {
  return (
    <div className="space-y-6">
      <div>
        <h2 className="text-xl font-bold text-zinc-100 flex items-center space-x-2">
          <Sliders className="w-5 h-5 text-cyan-400" />
          <span>System Specifications & Configuration</span>
        </h2>
        <p className="text-xs text-zinc-400 mt-1">
          Blessing AI v0.2 architectural specifications, hard risk governor rules, and Google SaaS-First configuration.
        </p>
      </div>

      <ArchitectureReviewViewer />
    </div>
  );
};
