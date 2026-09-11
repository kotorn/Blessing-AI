const fs = require('fs');
let code = fs.readFileSync('apps/trading_worker/main.py', 'utf-8');

code = code.replace('            async def start(self):', '    async def start(self):');

fs.writeFileSync('apps/trading_worker/main.py', code);
