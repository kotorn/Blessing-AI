import { execFileSync } from 'node:child_process';
import { existsSync } from 'node:fs';
import { describe, expect, it } from 'vitest';
import {
  buildTrustedPythonVerificationEnvironment,
  resolveTrustedLocalPythonRuntime,
  verifyPython,
} from '../src/backend/local-python-runtime.js';

function evaluateSddlInPowerShell(sddl: string, isParent: boolean): 'ACCEPT' | 'REJECT' {
  const script = `
    $sec = New-Object System.Security.AccessControl.DirectorySecurity;
    $sec.SetSecurityDescriptorSddlForm('${sddl}');
    $protected = @('S-1-5-18','S-1-5-32-544');
    $writeMask = [int64]0x50000000 -bor [int64][Security.AccessControl.FileSystemRights]::WriteData -bor [int64][Security.AccessControl.FileSystemRights]::AppendData -bor [int64][Security.AccessControl.FileSystemRights]::WriteExtendedAttributes -bor [int64][Security.AccessControl.FileSystemRights]::CreateFiles -bor [int64][Security.AccessControl.FileSystemRights]::CreateDirectories -bor [int64][Security.AccessControl.FileSystemRights]::DeleteSubdirectoriesAndFiles -bor [int64][Security.AccessControl.FileSystemRights]::WriteAttributes -bor [int64][Security.AccessControl.FileSystemRights]::Delete -bor [int64][Security.AccessControl.FileSystemRights]::ChangePermissions -bor [int64][Security.AccessControl.FileSystemRights]::TakeOwnership;
    foreach ($rule in $sec.Access) {
      $sid = try {
        $rule.IdentityReference.Translate([Security.Principal.SecurityIdentifier]).Value
      } catch {
        $raw = $rule.IdentityReference.Value;
        if ($raw -match 'ALL APPLICATION PACKAGES' -or $raw -match 'S-1-15-2-1') { 'S-1-15-2-1' }
        elseif ($raw -match 'ALL RESTRICTED APP' -or $raw -match 'S-1-15-2-2') { 'S-1-15-2-2' }
        else { $raw }
      };
      $rights = [int64]$rule.FileSystemRights;
      $isParent = ${isParent ? '$true' : '$false'};
      if ($isParent -and ($rule.PropagationFlags -band [Security.AccessControl.PropagationFlags]::InheritOnly)) { continue };
      $testRights = $rights;
      if ($isParent) { $testRights = $testRights -band (-bnot [int64]4) };
      $unsafeRights = ($testRights -band $writeMask) -ne 0;
      if ($rule.AccessControlType -eq [Security.AccessControl.AccessControlType]::Allow -and $unsafeRights -and $sid -notin $protected) {
        Write-Output 'REJECT';
        exit 0;
      }
    }
    Write-Output 'ACCEPT';
  `;
  return execFileSync('powershell.exe', ['-NoProfile', '-NonInteractive', '-Command', script], {
    encoding: 'utf8',
  }).trim() as 'ACCEPT' | 'REJECT';
}

describe('trusted Local Python runtime', () => {
  it('normalizes environment names and strips inherited credential variables', () => {
    expect(buildTrustedPythonVerificationEnvironment({
      Path: 'C:\\Windows\\System32', SystemRoot: 'C:\\Windows',
      BINANCE_MAINNET_API_SECRET: 'must-not-forward',
    }, 'C:\\Python\\python.exe')).toMatchObject({
      PATH: 'C:\\Windows\\System32', SYSTEMROOT: 'C:\\Windows',
      BLESSING_PYTHON_PATH: 'C:\\Python\\python.exe',
    });
    expect(buildTrustedPythonVerificationEnvironment({ BINANCE_MAINNET_API_SECRET: 'x' }, '')
      .BINANCE_MAINNET_API_SECRET).toBeUndefined();
  });

  it.runIf(process.platform === 'win32' && process.env.LOCAL_WORKER_RUNTIME === 'HOST_PYTHON')(
    'verifies the signer and protected runtime installation before explicit host-Python use', () => {
    const runtime = resolveTrustedLocalPythonRuntime(process.env, process.cwd());

    expect(runtime.executable).toMatch(/^[A-Z]:\\.+\\python\.exe$/i);
    expect(() => runtime.assertUnchanged()).not.toThrow();
    },
  );

  it.runIf(process.platform === 'win32')(
    'accepts parent directory with InheritOnly ACE in SDDL',
    () => {
      // (A;OICIIO;GA;;;WD) is an InheritOnly (IO) ACE granting GenericAll to Everyone (WD)
      expect(evaluateSddlInPowerShell('D:P(A;OICIIO;GA;;;WD)', true)).toBe('ACCEPT');
    },
  );

  it.runIf(process.platform === 'win32')(
    'rejects non-parent root directory with InheritOnly write ACE in SDDL',
    () => {
      expect(evaluateSddlInPowerShell('D:P(A;OICIIO;GA;;;WD)', false)).toBe('REJECT');
    },
  );

  it.runIf(process.platform === 'win32')(
    'rejects parent directory with non-inheriting unsafe write ACE in SDDL',
    () => {
      // (A;OICI;FA;;;WD) has ObjectInherit + ContainerInherit but not InheritOnly, applying directly
      expect(evaluateSddlInPowerShell('D:P(A;OICI;FA;;;WD)', true)).toBe('REJECT');
    },
  );

  it.runIf(process.platform === 'win32')(
    'accepts AppContainer SIDs in SDDL without throwing unhandled exceptions',
    () => {
      expect(
        evaluateSddlInPowerShell('D:P(A;;0x1200a9;;;S-1-15-2-1)(A;;0x1200a9;;;S-1-15-2-2)', false),
      ).toBe('ACCEPT');
    },
  );

  it.runIf(process.platform === 'win32')(
    'falls back to raw SIDs when Translate fails for AppContainer packages',
    () => {
      const script = `
        $acc1 = New-Object Security.Principal.NTAccount('APPLICATION PACKAGE AUTHORITY\\ALL APPLICATION PACKAGES');
        $rule1 = New-Object Security.AccessControl.FileSystemAccessRule($acc1, [Security.AccessControl.FileSystemRights]::ReadAndExecute, [Security.AccessControl.AccessControlType]::Allow);
        $sid1 = try { $rule1.IdentityReference.Translate([Security.Principal.SecurityIdentifier]).Value } catch { $raw = $rule1.IdentityReference.Value; if ($raw -match 'ALL APPLICATION PACKAGES' -or $raw -match 'S-1-15-2-1') { 'S-1-15-2-1' } elseif ($raw -match 'ALL RESTRICTED APP' -or $raw -match 'S-1-15-2-2') { 'S-1-15-2-2' } else { $raw } };
        $acc2 = New-Object Security.Principal.NTAccount('APPLICATION PACKAGE AUTHORITY\\ALL RESTRICTED APP PACKAGES');
        $rule2 = New-Object Security.AccessControl.FileSystemAccessRule($acc2, [Security.AccessControl.FileSystemRights]::ReadAndExecute, [Security.AccessControl.AccessControlType]::Allow);
        $sid2 = try { $rule2.IdentityReference.Translate([Security.Principal.SecurityIdentifier]).Value } catch { $raw = $rule2.IdentityReference.Value; if ($raw -match 'ALL APPLICATION PACKAGES' -or $raw -match 'S-1-15-2-1') { 'S-1-15-2-1' } elseif ($raw -match 'ALL RESTRICTED APP' -or $raw -match 'S-1-15-2-2') { 'S-1-15-2-2' } else { $raw } };
        Write-Output "$sid1,$sid2";
      `;
      const result = execFileSync('powershell.exe', ['-NoProfile', '-NonInteractive', '-Command', script], {
        encoding: 'utf8',
      }).trim();
      expect(result).toBe('S-1-15-2-1,S-1-15-2-2');
    },
  );

  it('throws LOCAL_PILOT_TRUSTED_PYTHON_EXECUTABLE_NOT_VERIFIED when no python executable is found on PATH', () => {
    expect(() => resolveTrustedLocalPythonRuntime({
      PATH: 'C:\\Windows\\System32',
      SystemRoot: 'C:\\Windows',
    }, process.cwd())).toThrow('LOCAL_PILOT_TRUSTED_PYTHON_EXECUTABLE_NOT_VERIFIED');
  });

  it.runIf(process.platform === 'win32' && existsSync('C:\\Python314\\python.exe'))(
    'verifies trusted Python executable hash without recursive tree check when verifyTree is false',
    () => {
      const digest = verifyPython('C:\\Python314\\python.exe', process.env, false);
      expect(digest).toMatch(/^[a-f0-9]{64}$/);
    },
  );

  it.runIf(process.platform === 'win32' && existsSync('C:\\Python314\\python.exe'))(
    'throws LOCAL_PILOT_PYTHON_DEPENDENCIES_MISSING when candidate passes ACL verification but lacks worker dependencies',
    () => {
      expect(() => resolveTrustedLocalPythonRuntime({
        PATH: 'C:\\Python314;C:\\Windows\\System32',
        SystemRoot: 'C:\\Windows',
      }, process.cwd())).toThrow('LOCAL_PILOT_PYTHON_DEPENDENCIES_MISSING');
    },
    60_000,
  );
});
