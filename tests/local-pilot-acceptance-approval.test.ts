import { expect, it } from 'vitest';
import { parseApprovedPilotAcceptanceReceipt, parsePilotAcceptanceImagePolicy }
  from '../src/backend/local-pilot-acceptance-approval.js';

const policy = parsePilotAcceptanceImagePolicy({ version: 1, checker: {
  imageId: `sha256:${'a'.repeat(64)}`,
  baseImageDigest: `node:22-alpine@sha256:${'b'.repeat(64)}`,
  launcherSha256: 'c'.repeat(64),
} });
const binding = { gitSha: 'd'.repeat(40), sourceSha256: 'e'.repeat(64), dependencySha256: 'f'.repeat(64),
  migrationSha256: '1'.repeat(64), policySha256: '2'.repeat(64) };
const receipt = { schemaVersion: 1, ...binding, snapshotId: '33333333-3333-4333-8333-333333333333',
  volumeName: 'blessing-acceptance-source-33333333-3333-4333-8333-333333333333',
  manifestSha256: '4'.repeat(64), sealSha256: '4'.repeat(64), checkerImageId: policy.checker.imageId,
  checkerBaseImageDigest: policy.checker.baseImageDigest, checkerLauncherSha256: policy.checker.launcherSha256 };

it('accepts only a protected receipt bound to the exact clean source, dependencies, image and seal', () => {
  expect(parseApprovedPilotAcceptanceReceipt(receipt, binding, policy)).toEqual(receipt);
  expect(() => parseApprovedPilotAcceptanceReceipt({ ...receipt, sourceSha256: '6'.repeat(64) }, binding, policy))
    .toThrow('LOCAL_PILOT_ACCEPTANCE_APPROVAL_NOT_VALID');
  expect(() => parseApprovedPilotAcceptanceReceipt({ ...receipt, checkerImageId: `sha256:${'9'.repeat(64)}` }, binding, policy))
    .toThrow('LOCAL_PILOT_ACCEPTANCE_APPROVAL_NOT_VALID');
  expect(() => parseApprovedPilotAcceptanceReceipt({ ...receipt, sealSha256: 'invalid' }, binding, policy))
    .toThrow('LOCAL_PILOT_ACCEPTANCE_APPROVAL_NOT_VALID');
});

it('fails closed when checker image policy contains placeholders or mutable tags', () => {
  expect(() => parsePilotAcceptanceImagePolicy({ version: 1, checker: {
    imageId: '', baseImageDigest: 'node:latest', launcherSha256: '',
  } })).toThrow('LOCAL_PILOT_ACCEPTANCE_IMAGE_POLICY_UNAVAILABLE');
});
