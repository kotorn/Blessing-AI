import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { describe, expect, it } from 'vitest';

const lakehouseSource = readFileSync(
  resolve(process.cwd(), 'src/components/BigQueryLakehouse.tsx'),
  'utf8',
);

describe('analytics route safety regression', () => {
  it('keeps the BigQuery loading hook setter in the setter slot', () => {
    expect(lakehouseSource).toContain(
      'const [, setLoadingConfig] = useState<boolean>(true);',
    );
    expect(lakehouseSource).not.toContain(
      'const [ setLoadingConfig] = useState<boolean>(true);',
    );
  });
});
