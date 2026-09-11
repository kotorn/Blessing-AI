const fs = require('fs');
let code = fs.readFileSync('src/components/MetaAllocationMatrix.tsx', 'utf-8');

code = code.replace(
  'export const MetaAllocationMatrix: React.FC = () => {',
  'export const MetaAllocationMatrix: React.FC<{ allocationsData?: MetaAllocationWeight[] }> = ({ allocationsData }) => {'
);

const allocationsArrayStart = `  const allocations: MetaAllocationWeight[] = [`;

code = code.replace(
  allocationsArrayStart,
  `  const allocations: MetaAllocationWeight[] = allocationsData || [`
);

fs.writeFileSync('src/components/MetaAllocationMatrix.tsx', code);
