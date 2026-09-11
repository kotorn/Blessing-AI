const fs = require('fs');
let code = fs.readFileSync('src/components/ExposureRecoveryEngineCard.tsx', 'utf-8');

code = code.replace(
  'interface ExposureRecoveryEngineCardProps {',
  'interface ExposureRecoveryEngineCardProps {\n  recoveryData?: any;'
);

code = code.replace(
  '  baskets,\n}) => {',
  '  baskets,\n  recoveryData,\n}) => {'
);

const actionInsert = `  if (recoveryData?.status === 'ACTIVE_GRID_BRAKE') {
    assessment.currentDrawdownPct = recoveryData.current_drawdown_pct;
    assessment.recommendedAction = 'BLOCK_GRID_EXPANSION';
    assessment.actionComparison.decisionRationale = recoveryData.action_taken;
  }`;

code = code.replace(
  '  };',
  '  };\n\n' + actionInsert
);

fs.writeFileSync('src/components/ExposureRecoveryEngineCard.tsx', code);
