const fs = require('fs');
let code = fs.readFileSync('src/pages/RiskRecoveryPage.tsx', 'utf-8');

code = code.replace(
  '<ExposureRecoveryEngineCard baskets={baskets} />',
  '<ExposureRecoveryEngineCard baskets={baskets} recoveryData={(quantState as any)?.exposure_recovery} />'
);

fs.writeFileSync('src/pages/RiskRecoveryPage.tsx', code);
