import { expect, it } from 'vitest';
import { hasPapiProtectionRiskAcknowledgement } from '../src/backend/local-pilot-risk-ack.js';

it('accepts only the explicit true PAPI protection-risk acknowledgement', () => {
  expect(hasPapiProtectionRiskAcknowledgement({ papiProtectionRiskAcknowledged: true })).toBe(true);
  for (const value of [undefined, null, {}, { papiProtectionRiskAcknowledged: false },
    { papiProtectionRiskAcknowledged: 1 }, { papiProtectionRiskAcknowledged: 'true' }]) {
    expect(hasPapiProtectionRiskAcknowledgement(value)).toBe(false);
  }
});
