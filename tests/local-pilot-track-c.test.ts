import { readFileSync } from 'node:fs';
import { execFileSync } from 'node:child_process';
import path from 'node:path';
import { describe, expect, it } from 'vitest';
import { trackCPhases } from '../src/backend/local-pilot-attestation.js';

const cases = JSON.parse(readFileSync(new URL('./fixtures/local_pilot_track_c_phases.json', import.meta.url), 'utf8'));
describe('Track C shared phase contract (verified results, not untrusted receipts)', () => {
  for (const fixture of cases) it(fixture.name, () => {
    const result = trackCPhases(fixture.passClasses, fixture.sourceClean);
    expect(result.status === 'READY').toBe(fixture.ready);
    expect(result.canApprove).toBe(fixture.ready);
    expect(result.canStart).toBe(fixture.ready);
    expect(result.prepared.status).toBe('NOT_RUN');
  });
  it('duplicate and unknown classes cannot fill a missing class', () => {
    expect(trackCPhases(['CHECKS', 'CHECKS', 'LOCAL_PILOT_CI'], true).status).toBe('BLOCKED');
  });
  it('matches Python for the complete shared phase output', () => {
    const root = path.resolve(import.meta.dirname, '..');
    const source = `import sys,json;sys.path.insert(0,${JSON.stringify(root)});from scripts.local_pilot_track_c import track_c_phases;cases=json.load(open('tests/fixtures/local_pilot_track_c_phases.json'));print(json.dumps([track_c_phases(c['passClasses'],c['sourceClean']) for c in cases]))`;
    const environment = Object.fromEntries(Object.entries(process.env).filter(([k]) => ['PATH', 'PATHEXT', 'SYSTEMROOT', 'WINDIR', 'TEMP', 'TMP'].includes(k.toUpperCase())));
    const python = JSON.parse(execFileSync('python', ['-I', '-c', source], { cwd: root, env: environment, shell: false, encoding: 'utf8' }));
    expect(cases.map((c: any) => trackCPhases(c.passClasses, c.sourceClean))).toEqual(python);
  });
});
