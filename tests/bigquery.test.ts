import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { describe, expect, it } from 'vitest';
import { PRESET_BIGQUERY_QUERIES } from '../src/lib/bigquery';

const backend = readFileSync(resolve(process.cwd(), 'src/backend/bigquery.ts'), 'utf8');
const client = readFileSync(resolve(process.cwd(), 'src/lib/bigquery.ts'), 'utf8');

describe('BigQuery evidence and cost boundary', () => {
  it('accepts symbolic direct timestamp partition filters', () => {
    const partitionFilter = /\b(?:timestamp|created_at)\b\s*(?:>=|>|=|BETWEEN)\s*/i;
    expect(partitionFilter.test('WHERE timestamp >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 7 DAY)')).toBe(true);
    expect(partitionFilter.test('WHERE created_at > TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 90 DAY)')).toBe(true);
  });

  it('keeps the query boundary to one explicit SELECT or WITH statement', () => {
    expect(backend).toContain("query.includes(';')");
    expect(backend).toContain('/^\\s*(SELECT|WITH)\\b/i');
    expect(backend).toContain('|EXECUTE|');
  });

  it('uses direct timestamp partition predicates in every preset', () => {
    for (const preset of PRESET_BIGQUERY_QUERIES) {
      expect(preset.sql).not.toMatch(/DATE\(\s*(timestamp|created_at)\s*\)\s*(>=|>|=|BETWEEN)/i);
      expect(preset.sql).toMatch(/\b(timestamp|created_at)\b\s*(>=|>|=|BETWEEN)/i);
    }
  });

  it('keeps Firebase identity, bounded result pages, and telemetry read-back in the backend contract', () => {
    expect(backend).toContain('Firebase ID token');
    expect(backend).toContain('MAX_RESULT_ROWS = 500');
    expect(backend).toContain('autoPaginate: false');
    expect(backend).toContain('verifyTelemetryReadback');
    expect(backend).toContain('requireTelemetryProducer');
    expect(backend).toContain('insertId: String(row[readbackSpec.identityField])');
    expect(backend).toContain('require_partition_filter');
    expect(backend).toContain('TIMESTAMP(@partitionStart)');
    expect(backend).toContain('telemetryReadbackSpec');
  });

  it('represents an unavailable browser buffer as an explicit safe no-op', () => {
    expect(client).toContain('body: JSON.stringify({ rows: [] })');
  });
});
