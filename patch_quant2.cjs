const fs = require('fs');
let code = fs.readFileSync('src/api/quant.ts', 'utf-8');

code = code.replace(
  '  meta_allocations?: any[];',
  '  meta_allocations?: any[];\n  exposure_recovery?: any;'
);

fs.writeFileSync('src/api/quant.ts', code);
