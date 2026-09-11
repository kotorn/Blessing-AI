const fs = require('fs');
let code = fs.readFileSync('server.ts', 'utf-8');

code = code.replace(
  /app\.get\('\/api\/system\/state', \(req, res\) => \{[\s\S]*?res\.json\(tradingSystemState\);\n\}\);/,
  `app.get('/api/system/state', (req, res) => {
  res.json({
    ...tradingSystemState,
    capabilities: require('./src/backend/system.js').EXECUTION_CAPABILITIES,
  });
});`
);

code = code.replace(
  /app\.get\('\/api\/system\/preflight', \(req, res\) => \{[\s\S]*?res\.json\(preflight\);\n\}\);/,
  `app.get('/api/system/preflight', (req, res) => {
  const requestedConfig = {
    executionMode: req.query.executionMode || 'PAPER'
  };
  const preflight = evaluatePreflight(tradingSystemState, requestedConfig);
  res.json({
    ...preflight,
    capabilities: require('./src/backend/system.js').EXECUTION_CAPABILITIES
  });
});`
);

fs.writeFileSync('server.ts', code);
