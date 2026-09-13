import { AlertTriangle } from 'lucide-react';

type IllustrativeEvidenceBannerProps = { message?: string };

export const IllustrativeEvidenceBanner = ({
  message = 'This panel contains illustrative research fixtures only. It is not verified launch evidence and cannot authorize execution.',
}: IllustrativeEvidenceBannerProps) => (
  <div className="flex items-start gap-2 rounded-lg border border-amber-800/70 bg-amber-950/30 px-3 py-2 text-[11px] text-amber-200">
    <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0 text-amber-400" />
    <span>
      <strong className="font-bold">ILLUSTRATIVE_ONLY</strong> — {message}
    </span>
  </div>
);
