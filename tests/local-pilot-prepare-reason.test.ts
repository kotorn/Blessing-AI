import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { describe, expect, it, vi } from 'vitest';
import { createElement } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import * as pilotPanel from '../src/components/LocalLivePilotPanel';
vi.mock('dotenv', () => ({ default: { config: vi.fn() } }));

import { sanitizePilotPrepareReason } from '../server.js';

describe('Local Pilot Prepare Reason Sanitization and UI Display (M1)', () => {
  it('never logs the raw Prepare error object', () => {
    const source = readFileSync(resolve(process.cwd(), 'server.ts'), 'utf8');
    const block = source.split("app.post('/api/local/pilot/prepare'")[1]?.split("app.post('/api/local/pilot/start'")[0] ?? '';
    expect(block).not.toContain("console.error('PILOT_PREPARE_FAILED:', message, error)");
  });

  it('does not treat arbitrary preflight-shaped text as an approved diagnostic ID', () => {
    expect(sanitizePilotPrepareReason(new Error('LOCAL_PILOT_SIGNED_PREFLIGHT_FAILED: CHK-PREFLIGHT-SECRET-CANARY; CHK-PREFLIGHT-AUTH')))
      .toBe('LOCAL_PILOT_SIGNED_PREFLIGHT_FAILED: CHK-PREFLIGHT-AUTH');
  });

  it('renders a failed response reason next to its error code in the actual status component', () => {
    const components = pilotPanel as unknown as Record<string, any>;
    expect(components.LocalPilotStatusMessage).toBeTypeOf('function');
    expect(components.formatLocalPilotFailure).toBeTypeOf('function');
    const message = components.formatLocalPilotFailure({ data: {
      error: 'LOCAL_PILOT_PREPARE_FAILED', reason: 'LOCAL_PILOT_FINGERPRINT_CHANGED',
    } }, 'failed');
    const html = renderToStaticMarkup(createElement(components.LocalPilotStatusMessage, { busy: false, evidence: 'FAIL', message }));
    expect(html).toContain('LOCAL_PILOT_PREPARE_FAILED · LOCAL_PILOT_FINGERPRINT_CHANGED');
    expect(html).toContain('role="status"');
    expect(components.formatLocalPilotFailure(new Error('Bearer SYNTHETIC_CANARY'), 'failed')).toBe('failed');
  });
  it('returns known error codes unchanged', () => {
    expect(sanitizePilotPrepareReason(new Error('LOCAL_PILOT_REQUIRES_REVIEWED_CLEAN_COMMIT')))
      .toBe('LOCAL_PILOT_REQUIRES_REVIEWED_CLEAN_COMMIT');
    expect(sanitizePilotPrepareReason(new Error('LOCAL_PILOT_FINGERPRINT_CHANGED')))
      .toBe('LOCAL_PILOT_FINGERPRINT_CHANGED');
    expect(sanitizePilotPrepareReason(new Error('LOCAL_PILOT_NOT_FOUND')))
      .toBe('LOCAL_PILOT_NOT_FOUND');
    expect(sanitizePilotPrepareReason(new Error('LOCAL_PILOT_DURABLE_POSTGRES_UNAVAILABLE')))
      .toBe('LOCAL_PILOT_DURABLE_POSTGRES_UNAVAILABLE');
  });

  it('sanitizes preflight failures to only extract safe check IDs and never echoes raw payload', () => {
    const errorWithChecks = new Error('LOCAL_PILOT_SIGNED_PREFLIGHT_FAILED: CHK-PREFLIGHT-RULES: rule mismatch; CHK-PREFLIGHT-AUTH: unverified token XYZ123');
    const sanitized = sanitizePilotPrepareReason(errorWithChecks);
    expect(sanitized).toBe('LOCAL_PILOT_SIGNED_PREFLIGHT_FAILED: CHK-PREFLIGHT-RULES, CHK-PREFLIGHT-AUTH');
    expect(sanitized).not.toContain('unverified token');
    expect(sanitized).not.toContain('XYZ123');
  });

  it('sanitizes raw library errors, network errors, or secrets to a fixed internal code', () => {
    const rawNetworkError = new Error('getaddrinfo ENOTFOUND api.binance.com: headers={Authorization: Bearer secret_12345}');
    expect(sanitizePilotPrepareReason(rawNetworkError)).toBe('LOCAL_PILOT_PREPARE_INTERNAL_ERROR');

    const rawSecretError = new Error('SecretManagerRpcError: projects/p/secrets/my-key/versions/1 payload invalid');
    expect(sanitizePilotPrepareReason(rawSecretError)).toBe('LOCAL_PILOT_PREPARE_INTERNAL_ERROR');

    expect(sanitizePilotPrepareReason('unexpected string error')).toBe('LOCAL_PILOT_PREPARE_UNKNOWN_ERROR');
    expect(sanitizePilotPrepareReason(null)).toBe('LOCAL_PILOT_PREPARE_UNKNOWN_ERROR');
  });

  it('verifies server.ts uses sanitizePilotPrepareReason in /api/local/pilot/prepare', () => {
    const serverSource = readFileSync(resolve(process.cwd(), 'server.ts'), 'utf8');
    const prepareBlock = serverSource.split("app.post('/api/local/pilot/prepare'")[1]?.split("app.post('/api/local/pilot/start'")[0] ?? '';
    expect(prepareBlock).toContain('sanitizePilotPrepareReason');
    expect(prepareBlock).toContain("reason: sanitizePilotPrepareReason(error)");
  });

  it('verifies LocalLivePilotPanel.tsx renders the reason next to the error code on failure', () => {
    const panelSource = readFileSync(resolve(process.cwd(), 'src/components/LocalLivePilotPanel.tsx'), 'utf8');
    expect(panelSource).toMatch(/reason/);
    expect(panelSource).toContain('setMessage');
  });
});
