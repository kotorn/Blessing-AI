const fs = require('fs');
let code = fs.readFileSync('src/pages/StrategiesPage.tsx', 'utf-8');

// The props definition is:
// interface StrategiesPageProps {
//   baskets: BasketItem[];
//   instruments: Record<string, InstrumentData>;
//   account: AccountData;
// }
// We should add strategyIntents and metaAllocations

code = code.replace(
  '  account: AccountData;',
  '  account: AccountData;\n  strategyIntents?: any[];\n  metaAllocations?: any[];'
);

code = code.replace(
  '  account,',
  '  account,\n  strategyIntents,\n  metaAllocations,'
);

code = code.replace(
  '<StrategyIntentStream baskets={baskets} instruments={instruments} />',
  '<StrategyIntentStream baskets={baskets} instruments={instruments} intentsData={strategyIntents} />'
);

code = code.replace(
  '<MetaAllocationMatrix />',
  '<MetaAllocationMatrix allocationsData={metaAllocations} />'
);

fs.writeFileSync('src/pages/StrategiesPage.tsx', code);
