import { execFileSync } from 'node:child_process';
import { lstatSync, readFileSync } from 'node:fs';
import path from 'node:path';
import type { PilotAcceptanceBinding } from './local-pilot-acceptance-runner.js';

export const LOCAL_PILOT_ACCEPTANCE_POLICY_PATH = 'config/local_pilot_acceptance_policy.json';
export const LOCAL_PILOT_ACCEPTANCE_APPROVAL_PATH = 'C:\\ProgramData\\BlessingAI\\pilot-acceptance\\approval.json';
const UUID = /^[a-f0-9]{8}-[a-f0-9]{4}-4[a-f0-9]{3}-[89ab][a-f0-9]{3}-[a-f0-9]{12}$/;
const HASH = /^[a-f0-9]{64}$/;

export interface ApprovedPilotAcceptanceReceipt extends PilotAcceptanceBinding {
  schemaVersion: 1;
  snapshotId: string;
  volumeName: string;
  manifestSha256: string;
  sealSha256: string;
  checkerImageId: string;
  checkerBaseImageDigest: string;
  checkerLauncherSha256: string;
}

export interface PilotAcceptanceImagePolicy {
  version: 1;
  checker: { imageId: string; baseImageDigest: string; launcherSha256: string };
}

export function parsePilotAcceptanceImagePolicy(value: unknown): PilotAcceptanceImagePolicy {
  const fail = () => { throw new Error('LOCAL_PILOT_ACCEPTANCE_IMAGE_POLICY_UNAVAILABLE'); };
  if (!value || typeof value !== 'object' || Array.isArray(value)) return fail();
  const policy = value as Record<string, any>;
  const checker = policy.checker;
  if (policy.version !== 1 || Object.keys(policy).sort().join(',') !== 'checker,version'
    || !checker || typeof checker !== 'object' || Array.isArray(checker)
    || Object.keys(checker).sort().join(',') !== 'baseImageDigest,imageId,launcherSha256'
    || typeof checker.imageId !== 'string' || !/^sha256:[a-f0-9]{64}$/.test(checker.imageId)
    || typeof checker.baseImageDigest !== 'string' || !/^.+@sha256:[a-f0-9]{64}$/.test(checker.baseImageDigest)
    || typeof checker.launcherSha256 !== 'string' || !HASH.test(checker.launcherSha256)) return fail();
  return { version: 1, checker: { ...checker } };
}

export function parseApprovedPilotAcceptanceReceipt(
  value: unknown,
  expected: PilotAcceptanceBinding,
  policy: PilotAcceptanceImagePolicy,
): ApprovedPilotAcceptanceReceipt {
  const fail = () => { throw new Error('LOCAL_PILOT_ACCEPTANCE_APPROVAL_NOT_VALID'); };
  if (!value || typeof value !== 'object' || Array.isArray(value)) return fail();
  const receipt = value as Record<string, unknown>;
  const allowed = ['schemaVersion', 'gitSha', 'sourceSha256', 'dependencySha256', 'migrationSha256',
    'policySha256', 'snapshotId', 'volumeName', 'manifestSha256', 'sealSha256', 'checkerImageId',
    'checkerBaseImageDigest', 'checkerLauncherSha256'].sort();
  if (Object.keys(receipt).sort().join(',') !== allowed.join(',') || receipt.schemaVersion !== 1
    || receipt.gitSha !== expected.gitSha || receipt.sourceSha256 !== expected.sourceSha256
    || receipt.dependencySha256 !== expected.dependencySha256 || receipt.migrationSha256 !== expected.migrationSha256
    || receipt.policySha256 !== expected.policySha256 || typeof receipt.snapshotId !== 'string'
    || !UUID.test(receipt.snapshotId) || receipt.volumeName !== `blessing-acceptance-source-${receipt.snapshotId}`
    || typeof receipt.manifestSha256 !== 'string' || !HASH.test(receipt.manifestSha256)
    || typeof receipt.sealSha256 !== 'string' || !HASH.test(receipt.sealSha256)
    || receipt.manifestSha256 !== receipt.sealSha256
    || receipt.checkerImageId !== policy.checker.imageId
    || receipt.checkerBaseImageDigest !== policy.checker.baseImageDigest
    || receipt.checkerLauncherSha256 !== policy.checker.launcherSha256) return fail();
  return receipt as unknown as ApprovedPilotAcceptanceReceipt;
}

function verifyProtectedWindowsPath(filePath: string): void {
  const canonical = path.win32.normalize(filePath);
  if (canonical !== path.win32.normalize(LOCAL_PILOT_ACCEPTANCE_APPROVAL_PATH)) {
    throw new Error('LOCAL_PILOT_ACCEPTANCE_APPROVAL_PATH_INVALID');
  }
  const parts = [
    'C:\\', 'C:\\ProgramData', 'C:\\ProgramData\\BlessingAI',
    'C:\\ProgramData\\BlessingAI\\pilot-acceptance', canonical,
  ];
  for (const item of parts) {
    const stat = lstatSync(item);
    if (stat.isSymbolicLink() || (stat as typeof stat & { isReparsePoint?: boolean }).isReparsePoint) {
      throw new Error('LOCAL_PILOT_ACCEPTANCE_APPROVAL_PATH_UNTRUSTED');
    }
  }
  const script = [
    "$ErrorActionPreference='Stop'",
    "$PSModuleAutoLoadingPreference='None'",
    "$moduleRoot=$env:SYSTEMROOT+'\\System32\\WindowsPowerShell\\v1.0\\Modules\\'",
    "Import-Module -Name ($moduleRoot+'Microsoft.PowerShell.Utility\\Microsoft.PowerShell.Utility.psd1') -Force -ErrorAction Stop",
    "Import-Module -Name ($moduleRoot+'Microsoft.PowerShell.Management\\Microsoft.PowerShell.Management.psd1') -Force -ErrorAction Stop",
    "Import-Module -Name ($moduleRoot+'Microsoft.PowerShell.Security\\Microsoft.PowerShell.Security.psd1') -Force -ErrorAction Stop",
    "$owners=@('NT AUTHORITY\\SYSTEM','BUILTIN\\Administrators','NT SERVICE\\TrustedInstaller')",
    "$untrusted=@('S-1-1-0','S-1-5-11','S-1-5-4','S-1-5-32-545',[Security.Principal.WindowsIdentity]::GetCurrent().User.Value)",
    "$items=@('C:\\ProgramData','C:\\ProgramData\\BlessingAI','C:\\ProgramData\\BlessingAI\\pilot-acceptance',$env:LOCAL_PILOT_APPROVAL_FILE)",
    "foreach($path in $items){$item=Get-Item -LiteralPath $path;if($item.Attributes -band [IO.FileAttributes]::ReparsePoint){exit 1};$acl=Get-Acl -LiteralPath $path;if($acl.Owner -notin $owners){exit 1};foreach($rule in $acl.Access){$sid=$rule.IdentityReference.Value;try{$sid=$rule.IdentityReference.Translate([Security.Principal.SecurityIdentifier]).Value}catch{};$rights=[int64]$rule.FileSystemRights;$writeMask=[int64]0x50000000 -bor [int64][Security.AccessControl.FileSystemRights]::WriteData -bor [int64][Security.AccessControl.FileSystemRights]::AppendData -bor [int64][Security.AccessControl.FileSystemRights]::WriteExtendedAttributes -bor [int64][Security.AccessControl.FileSystemRights]::CreateFiles -bor [int64][Security.AccessControl.FileSystemRights]::CreateDirectories -bor [int64][Security.AccessControl.FileSystemRights]::DeleteSubdirectoriesAndFiles -bor [int64][Security.AccessControl.FileSystemRights]::WriteAttributes -bor [int64][Security.AccessControl.FileSystemRights]::Delete -bor [int64][Security.AccessControl.FileSystemRights]::ChangePermissions -bor [int64][Security.AccessControl.FileSystemRights]::TakeOwnership;if($rule.AccessControlType -eq [Security.AccessControl.AccessControlType]::Allow -and ($rights -band $writeMask) -ne 0 -and ($sid -in $untrusted -or $rule.IdentityReference.Value -in $untrusted)){exit 1}}};Write-Output TRUSTED",
  ].join('; ');
  const result = execFileSync(path.join('C:\\Windows', 'System32', 'WindowsPowerShell', 'v1.0', 'powershell.exe'),
    ['-NoProfile', '-NonInteractive', '-Command', script], {
      cwd: process.cwd(), env: { SYSTEMROOT: process.env.SYSTEMROOT, LOCAL_PILOT_APPROVAL_FILE: canonical },
      encoding: 'utf8', stdio: ['ignore', 'pipe', 'ignore'], timeout: 20_000, maxBuffer: 65_536,
    }).trim();
  if (result !== 'TRUSTED') throw new Error('LOCAL_PILOT_ACCEPTANCE_APPROVAL_PATH_UNTRUSTED');
}

export function readProtectedPilotAcceptanceReceipt(
  expected: PilotAcceptanceBinding,
  policy: PilotAcceptanceImagePolicy,
  options: { platform?: string; verifyPath?: (filePath: string) => void; filePath?: string } = {},
): ApprovedPilotAcceptanceReceipt {
  if ((options.platform || process.platform) !== 'win32') {
    throw new Error('LOCAL_PILOT_ACCEPTANCE_APPROVAL_UNAVAILABLE');
  }
  const filePath = options.filePath || LOCAL_PILOT_ACCEPTANCE_APPROVAL_PATH;
  try {
    (options.verifyPath || verifyProtectedWindowsPath)(filePath);
    const stat = lstatSync(filePath);
    if (!stat.isFile() || stat.isSymbolicLink()) throw new Error('LOCAL_PILOT_ACCEPTANCE_APPROVAL_PATH_UNTRUSTED');
    const value = JSON.parse(readFileSync(filePath, 'utf8')) as unknown;
    return parseApprovedPilotAcceptanceReceipt(value, expected, policy);
  } catch {
    throw new Error('LOCAL_PILOT_ACCEPTANCE_APPROVAL_NOT_VALID');
  }
}
