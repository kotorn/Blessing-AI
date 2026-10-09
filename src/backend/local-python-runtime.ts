import { execFileSync } from 'node:child_process';
import { createHash } from 'node:crypto';
import { readFileSync } from 'node:fs';
import path from 'node:path';

const TRUSTED_PYTHON_SIGNER = 'CN=Python Software Foundation, O=Python Software Foundation, L=Beaverton, S=Oregon, C=US';
const WINDOWS_ROOT = 'C:\\Windows';

export interface TrustedLocalPythonRuntime {
  executable: string;
  assertUnchanged(): void;
}

/** Fixed, non-secret codes thrown by this module. Only these may reach operator-facing output. */
export const TRUSTED_PYTHON_RESOLVER_CODES = [
  'LOCAL_PILOT_TRUSTED_PYTHON_EXECUTABLE_NOT_VERIFIED',
  'LOCAL_PILOT_PYTHON_DEPENDENCIES_MISSING',
  'LOCAL_PILOT_TRUSTED_PYTHON_EXECUTABLE_CHANGED',
] as const;

function hasWorkerDependencies(executable: string, source: NodeJS.ProcessEnv, cwd: string): boolean {
  try {
    execFileSync(executable, ['-I', '-c', [
      'import importlib.util,sys',
      "required=('aiohttp','asyncpg','dotenv','fastapi','orjson','pydantic','uvicorn','websockets')",
      'sys.exit(0 if all(importlib.util.find_spec(name) for name in required) else 1)',
    ].join(';')], {
      cwd, env: buildTrustedPythonVerificationEnvironment(source, executable),
      stdio: 'ignore', timeout: 15_000, windowsHide: true,
    });
    return true;
  } catch {
    return false;
  }
}

export function buildTrustedPythonVerificationEnvironment(
  source: NodeJS.ProcessEnv,
  executable: string,
): NodeJS.ProcessEnv {
  const environment: NodeJS.ProcessEnv = {};
  const allowed = new Set(['PATH', 'PATHEXT', 'SYSTEMROOT', 'WINDIR', 'SYSTEMDRIVE', 'TEMP', 'TMP']);
  for (const [name, value] of Object.entries(source)) {
    const key = name.toUpperCase();
    if (allowed.has(key) && typeof value === 'string') environment[key] = value;
  }
  environment.BLESSING_PYTHON_PATH = executable;
  return environment;
}

export interface VerifyPythonDeps {
  run?: typeof execFileSync;
  now?: () => number;
}

// The recursive ACL walk of the interpreter tree takes tens of seconds on a real
// installation and every readiness evaluation repeated it several times on the
// control plane's event loop, starving the Worker heartbeat. A successful tree
// verification is reused for a short window; the interpreter's own digest is still
// re-hashed on every call, so a replaced python.exe is never accepted from cache.
// BLESSING_TRUSTED_PYTHON_CACHE_SECONDS=0 restores verify-every-time.
const treeVerifiedCache = new Map<string, { at: number; digest: string }>();

function treeCacheTtlMs(source: NodeJS.ProcessEnv): number {
  const seconds = Number(source.BLESSING_TRUSTED_PYTHON_CACHE_SECONDS ?? '60');
  return Number.isFinite(seconds) && seconds > 0 ? Math.min(seconds, 300) * 1000 : 0;
}

export function resetTrustedPythonVerificationCache(): void {
  treeVerifiedCache.clear();
}

export function verifyPython(
  executable: string,
  source: NodeJS.ProcessEnv = process.env,
  verifyTree = true,
  deps: VerifyPythonDeps = {},
): string {
  const run = deps.run ?? execFileSync;
  const now = deps.now ?? Date.now;
  const digestBefore = createHash('sha256').update(readFileSync(executable)).digest('hex');
  const cacheKey = `${path.resolve(executable).toLowerCase()}|${verifyTree ? 'tree' : 'file'}`;
  const ttl = treeCacheTtlMs(source);
  const cached = treeVerifiedCache.get(cacheKey);
  if (ttl > 0 && cached && cached.digest === digestBefore && now() - cached.at < ttl) return cached.digest;
  const environment = buildTrustedPythonVerificationEnvironment(source, executable);
  environment.BLESSING_VERIFY_PYTHON_TREE = verifyTree ? '1' : '0';
  const script = [
    "$ErrorActionPreference='Stop'",
    "$PSModuleAutoLoadingPreference='None'",
    "$moduleRoot=$env:SYSTEMROOT+'\\System32\\WindowsPowerShell\\v1.0\\Modules\\'",
    "Import-Module -Name ($moduleRoot+'Microsoft.PowerShell.Utility\\Microsoft.PowerShell.Utility.psd1') -Force -ErrorAction Stop",
    "Import-Module -Name ($moduleRoot+'Microsoft.PowerShell.Management\\Microsoft.PowerShell.Management.psd1') -Force -ErrorAction Stop",
    "Import-Module -Name ($moduleRoot+'Microsoft.PowerShell.Security\\Microsoft.PowerShell.Security.psd1') -Force -ErrorAction Stop",
    "$principal=New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())",
    'if ($principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) { exit 1 }',
    '$root=Split-Path -Parent $env:BLESSING_PYTHON_PATH',
    `$items=@(Get-Item -LiteralPath $env:BLESSING_PYTHON_PATH)`,
    `if ($env:BLESSING_VERIFY_PYTHON_TREE -eq '1') { $items=@(Get-Item -LiteralPath $root)+@(Get-ChildItem -LiteralPath $root -Directory -Recurse -Force)+@(Get-ChildItem -LiteralPath $root -File -Recurse -Force); `
      + '$cursor=Get-Item -LiteralPath $root; while ($cursor) { $items+= $cursor; '
      + 'if (-not $cursor.Parent) { break }; $cursor=$cursor.Parent } }',
    "$protected=@('S-1-5-18','S-1-5-32-544')",
    "$owners=@('NT AUTHORITY\\SYSTEM','BUILTIN\\Administrators','NT SERVICE\\TrustedInstaller')",
    'foreach ($item in $items) { if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) { exit 1 }; '
      + '$acl=Get-Acl -LiteralPath $item.FullName; '
      + '$sddl=$acl.GetSecurityDescriptorSddlForm([Security.AccessControl.AccessControlSections]::Access); '
      + 'if (-not $sddl.StartsWith("D:",[StringComparison]::Ordinal) -or $sddl.Contains("NO_ACCESS_CONTROL")) { exit 1 }; '
      + 'if ($acl.Owner -notin $owners) { exit 1 }; foreach ($rule in $acl.Access) { '
      + '$sid=try { $rule.IdentityReference.Translate([Security.Principal.SecurityIdentifier]).Value } '
      + 'catch { $raw=$rule.IdentityReference.Value; if ($raw -match "ALL APPLICATION PACKAGES" -or $raw -match "S-1-15-2-1") { "S-1-15-2-1" } '
      + 'elseif ($raw -match "ALL RESTRICTED APP" -or $raw -match "S-1-15-2-2") { "S-1-15-2-2" } else { $raw } }; '
      + '$rights=[int64]$rule.FileSystemRights; '
      + '$isParent=(-not $item.FullName.StartsWith($root,[StringComparison]::OrdinalIgnoreCase)); '
      + 'if ($isParent -and ($rule.PropagationFlags -band [Security.AccessControl.PropagationFlags]::InheritOnly)) { continue }; '
      + '$writeMask=[int64]0x50000000 -bor [int64][Security.AccessControl.FileSystemRights]::WriteData '
      + '-bor [int64][Security.AccessControl.FileSystemRights]::AppendData '
      + '-bor [int64][Security.AccessControl.FileSystemRights]::WriteExtendedAttributes '
      + '-bor [int64][Security.AccessControl.FileSystemRights]::CreateFiles '
      + '-bor [int64][Security.AccessControl.FileSystemRights]::CreateDirectories '
      + '-bor [int64][Security.AccessControl.FileSystemRights]::DeleteSubdirectoriesAndFiles '
      + '-bor [int64][Security.AccessControl.FileSystemRights]::WriteAttributes '
      + '-bor [int64][Security.AccessControl.FileSystemRights]::Delete '
      + '-bor [int64][Security.AccessControl.FileSystemRights]::ChangePermissions '
      + '-bor [int64][Security.AccessControl.FileSystemRights]::TakeOwnership; $testRights=$rights; '
      + 'if ($isParent) { $testRights=$testRights -band (-bnot [int64]4) }; '
      + '$unsafeRights=($testRights -band $writeMask) -ne 0; '
      + 'if ($rule.AccessControlType -eq [Security.AccessControl.AccessControlType]::Allow '
      + '-and $unsafeRights -and $sid -notin $protected) { exit 1 } } }',
    '$signature=Get-AuthenticodeSignature -LiteralPath $env:BLESSING_PYTHON_PATH',
    `if ($signature.Status -eq 'Valid' -and $signature.SignerCertificate.Subject -eq '${TRUSTED_PYTHON_SIGNER}') { Write-Output TRUSTED } else { exit 1 }`,
  ].join('; ');
  try {
    const trusted = run(path.join(WINDOWS_ROOT, 'System32', 'WindowsPowerShell', 'v1.0', 'powershell.exe'), [
      '-NoProfile', '-NonInteractive', '-Command', script,
    ], {
      cwd: process.cwd(), env: environment, encoding: 'utf8',
      stdio: ['ignore', 'pipe', 'ignore'], timeout: 120_000, maxBuffer: 1_048_576,
    }).trim();
    const digestAfter = createHash('sha256').update(readFileSync(executable)).digest('hex');
    if (String(trusted).trim() !== 'TRUSTED' || digestBefore !== digestAfter) throw new Error('python runtime changed');
    if (ttl > 0) treeVerifiedCache.set(cacheKey, { at: now(), digest: digestAfter });
    return digestAfter;
  } catch {
    throw new Error('LOCAL_PILOT_TRUSTED_PYTHON_EXECUTABLE_NOT_VERIFIED');
  }
}

export function resolveTrustedLocalPythonRuntime(
  source: NodeJS.ProcessEnv = process.env,
  cwd = process.cwd(),
): TrustedLocalPythonRuntime {
  if (process.platform !== 'win32') throw new Error('LOCAL_PILOT_TRUSTED_PYTHON_EXECUTABLE_NOT_VERIFIED');
  const environment = buildTrustedPythonVerificationEnvironment(source, '');
  let candidates: string[];
  try {
    candidates = execFileSync(path.join(WINDOWS_ROOT, 'System32', 'where.exe'), ['python.exe'], {
      cwd, env: environment, encoding: 'utf8', stdio: ['ignore', 'pipe', 'ignore'], timeout: 10_000,
    }).split(/\r?\n/).map((item) => item.trim()).filter((item) => item && path.isAbsolute(item))
      // The Windows Store execution alias is a reparse stub, not an interpreter.
      .filter((item) => !item.toLowerCase().includes(`${path.sep}microsoft${path.sep}windowsapps${path.sep}`));
    if (candidates.length === 0) throw new Error('no concrete Python runtime found');
  } catch {
    throw new Error('LOCAL_PILOT_TRUSTED_PYTHON_EXECUTABLE_NOT_VERIFIED');
  }
  let executable: string | undefined;
  let runtimeHash: string | undefined;
  let hasTrustedCandidateWithoutDependencies = false;
  for (const candidate of candidates) {
    try {
      const candidatePath = path.resolve(candidate);
      const candidateHash = verifyPython(candidatePath, source, true);
      if (!hasWorkerDependencies(candidatePath, source, cwd)) {
        hasTrustedCandidateWithoutDependencies = true;
        continue;
      }
      executable = candidatePath;
      runtimeHash = candidateHash;
      break;
    } catch {
      // Continue only to another independently verified, protected PSF runtime.
    }
  }
  if (!executable || !runtimeHash) {
    if (hasTrustedCandidateWithoutDependencies) {
      throw new Error('LOCAL_PILOT_PYTHON_DEPENDENCIES_MISSING');
    }
    throw new Error('LOCAL_PILOT_TRUSTED_PYTHON_EXECUTABLE_NOT_VERIFIED');
  }
  return {
    executable,
    assertUnchanged() {
      if (verifyPython(executable, source, true) !== runtimeHash) {
        throw new Error('LOCAL_PILOT_TRUSTED_PYTHON_EXECUTABLE_CHANGED');
      }
    },
  };
}
