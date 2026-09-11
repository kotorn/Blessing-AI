const fs = require('fs');
let code = fs.readFileSync('src/components/StrategyIntentStream.tsx', 'utf-8');

code = code.replace(
  'interface StrategyIntentStreamProps {',
  'interface StrategyIntentStreamProps {\n  intentsData?: StrategyIntentItem[];'
);

code = code.replace(
  '  instruments,\n}) => {',
  '  instruments,\n  intentsData,\n}) => {'
);

const intentsArrayStart = `  const intents: StrategyIntentItem[] = [`;
const intentsArrayEnd = `  ];`;

// We'll just replace the declaration with: const intents: StrategyIntentItem[] = intentsData || [ ... ];
code = code.replace(
  intentsArrayStart,
  `  const intents: StrategyIntentItem[] = intentsData || [`
);

fs.writeFileSync('src/components/StrategyIntentStream.tsx', code);
