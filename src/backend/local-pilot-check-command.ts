import { existsSync, realpathSync } from 'node:fs';
import path from 'node:path';

/** Resolve npm beside the running Node binary; never consult browser input or a shell. */
export function localPilotNpmCommand(script: 'test' | 'lint' | 'build'): [string, string[]] {
  if (!['test', 'lint', 'build'].includes(script)) throw new Error('LOCAL_PILOT_CHECK_NOT_ALLOWED');
  const executable = realpathSync(process.execPath);
  const candidate = process.platform === 'win32'
    ? path.join(path.dirname(executable), 'node_modules', 'npm', 'bin', 'npm-cli.js')
    : path.resolve(path.dirname(executable), '..', 'lib', 'node_modules', 'npm', 'bin', 'npm-cli.js');
  if (!existsSync(candidate)) throw new Error('LOCAL_PILOT_NPM_CLI_UNAVAILABLE');
  // A substituted/symlinked CLI outside the adjacent npm installation is not accepted.
  if (realpathSync(candidate) !== path.resolve(candidate)) throw new Error('LOCAL_PILOT_NPM_CLI_UNTRUSTED');
  return [executable, [candidate, 'run', script]];
}
