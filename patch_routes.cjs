const fs = require('fs');
let code = fs.readFileSync('src/app/Routes.tsx', 'utf-8');

code = code.replace(
  '        <StrategiesPage',
  '        <StrategiesPage\n          strategyIntents={(quantState as any)?.strategy_intents}\n          metaAllocations={(quantState as any)?.meta_allocations}'
);

fs.writeFileSync('src/app/Routes.tsx', code);
