const fs = require('fs');
let code = fs.readFileSync('src/components/StrategyIntentStream.tsx', 'utf-8');

// Replace "Opp Score" with "ML Opp Score" for visual distinctness
code = code.replace(
  '<div className="text-[10px] text-zinc-500 font-sans">Opp Score</div>',
  '<div className="text-[10px] text-zinc-500 font-sans flex items-center justify-center gap-1"><Cpu className="w-3 h-3 text-cyan-500/70" /> ML Score</div>'
);

code = code.replace(
  'Strategy Intent Stream (Continuous Alpha Production)',
  'Strategy Intent Stream (ML Calibrated)'
);

fs.writeFileSync('src/components/StrategyIntentStream.tsx', code);
