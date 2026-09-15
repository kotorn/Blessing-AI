import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { describe, expect, it } from 'vitest';

const operations = readFileSync(resolve(process.cwd(), 'dataconnect/connector/operations.gql'), 'utf8');
const schema = readFileSync(resolve(process.cwd(), 'dataconnect/schema/schema.gql'), 'utf8');

describe('SQL Connect authorization contract', () => {
  it('keeps privileged profile fields out of USER operations', () => {
    expect(operations).toContain('mutation SetUserProfileAccess');
    expect(operations).toContain('@auth(level: NO_ACCESS)');
    const updateProfile = operations.match(/mutation UpdateMyProfile[\s\S]*?\n}\n/)?.[0] || '';
    expect(updateProfile).not.toContain('$role');
    expect(updateProfile).not.toContain('$isActive');
  });

  it('binds basket writes to auth.uid and does not expose owner replacement', () => {
    expect(operations).toContain('ownerUid_expr: "auth.uid"');
    expect(operations).toContain('ownerUid: { eq_expr: "auth.uid" }');
    expect(operations).not.toContain('UpsertMyBasket');
  });

  it('keeps the cutover and rollback flags explicit', () => {
    expect(schema).toContain('ownerUid: String!');
    expect(readFileSync(resolve(process.cwd(), 'docs/DATACONNECT-MIGRATION.md'), 'utf8')).toContain('VITE_DATA_CONNECT_ROLLBACK=true');
  });
});
