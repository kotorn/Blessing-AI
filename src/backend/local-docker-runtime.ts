import { execFileSync } from 'node:child_process';
import { createHash } from 'node:crypto';
import { readFileSync } from 'node:fs';
import path from 'node:path';

const DOCKER_CLI = 'C:\\Program Files\\Docker\\Docker\\resources\\bin\\docker.exe';
const DOCKER_ENGINE_PIPE = 'npipe:////./pipe/dockerDesktopLinuxEngine';
const TRUSTED_DOCKER_SIGNER = 'CN=Docker Inc,';

export interface TrustedLocalDockerRuntime {
  executable: string;
  imageId: string;
  serverVersion: string;
  engineHost: string;
  expectedLabels: Readonly<Record<string, string>>;
  assertUnchanged(): void;
}

function buildVerificationEnvironment(source: NodeJS.ProcessEnv): NodeJS.ProcessEnv {
  const result: NodeJS.ProcessEnv = {};
  const allow = new Set(['PATH', 'PATHEXT', 'SYSTEMROOT', 'WINDIR', 'SYSTEMDRIVE', 'TEMP', 'TMP']);
  for (const [name, value] of Object.entries(source)) {
    if (allow.has(name.toUpperCase()) && typeof value === 'string') result[name] = value;
  }
  result.DOCKER_HOST = DOCKER_ENGINE_PIPE;
  return result;
}

function verifyDockerCli(source: NodeJS.ProcessEnv, run = execFileSync): string {
  const digestBefore = createHash('sha256').update(readFileSync(DOCKER_CLI)).digest('hex');
  const env = buildVerificationEnvironment(source);
  env.LOCAL_DOCKER_CLI = DOCKER_CLI;
  const script = [
    "$ErrorActionPreference='Stop'",
    "$PSModuleAutoLoadingPreference='None'",
    "$moduleRoot=$env:SYSTEMROOT+'\\System32\\WindowsPowerShell\\v1.0\\Modules\\'",
    "Import-Module -Name ($moduleRoot+'Microsoft.PowerShell.Utility\\Microsoft.PowerShell.Utility.psd1') -Force -ErrorAction Stop",
    "Import-Module -Name ($moduleRoot+'Microsoft.PowerShell.Management\\Microsoft.PowerShell.Management.psd1') -Force -ErrorAction Stop",
    "Import-Module -Name ($moduleRoot+'Microsoft.PowerShell.Security\\Microsoft.PowerShell.Security.psd1') -Force -ErrorAction Stop",
    "$principal=New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())",
    'if ($principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) { exit 1 }',
    '$root=Split-Path -Parent $env:LOCAL_DOCKER_CLI',
    '$items=@(Get-Item -LiteralPath $env:LOCAL_DOCKER_CLI); $cursor=Get-Item -LiteralPath $root; while ($cursor) { if ([IO.Path]::GetPathRoot($cursor.FullName) -eq $cursor.FullName) { break }; $items+= $cursor; $cursor=$cursor.Parent }',
    "$untrusted=@('S-1-1-0','S-1-5-11','S-1-5-4','S-1-5-32-545','Everyone','NT AUTHORITY\\Authenticated Users','NT AUTHORITY\\INTERACTIVE','BUILTIN\\Users',[Security.Principal.WindowsIdentity]::GetCurrent().User.Value)",
    "$owners=@('NT AUTHORITY\\SYSTEM','BUILTIN\\Administrators','NT SERVICE\\TrustedInstaller')",
    'foreach ($item in $items) { if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) { exit 1 }; $acl=Get-Acl -LiteralPath $item.FullName; if ($acl.Owner -notin $owners) { exit 1 }; foreach ($rule in $acl.Access) { $sid=$rule.IdentityReference.Value; try { $sid=$rule.IdentityReference.Translate([Security.Principal.SecurityIdentifier]).Value } catch { }; $rights=[int64]$rule.FileSystemRights; $writeMask=[int64]0x50000000 -bor [int64][Security.AccessControl.FileSystemRights]::WriteData -bor [int64][Security.AccessControl.FileSystemRights]::AppendData -bor [int64][Security.AccessControl.FileSystemRights]::WriteExtendedAttributes -bor [int64][Security.AccessControl.FileSystemRights]::CreateFiles -bor [int64][Security.AccessControl.FileSystemRights]::CreateDirectories -bor [int64][Security.AccessControl.FileSystemRights]::DeleteSubdirectoriesAndFiles -bor [int64][Security.AccessControl.FileSystemRights]::WriteAttributes -bor [int64][Security.AccessControl.FileSystemRights]::Delete -bor [int64][Security.AccessControl.FileSystemRights]::ChangePermissions -bor [int64][Security.AccessControl.FileSystemRights]::TakeOwnership; if ($rule.AccessControlType -eq [Security.AccessControl.AccessControlType]::Allow -and ($rights -band $writeMask) -ne 0 -and ($sid -in $untrusted -or $rule.IdentityReference.Value -in $untrusted)) { exit 1 } } }',
    '$signature=Get-AuthenticodeSignature -LiteralPath $env:LOCAL_DOCKER_CLI',
    `if ($signature.Status -eq 'Valid' -and $signature.SignerCertificate.Subject.StartsWith('${TRUSTED_DOCKER_SIGNER}',[StringComparison]::Ordinal)) { Write-Output TRUSTED } else { exit 1 }`,
  ].join('; ');
  const output = run(path.join('C:\\Windows', 'System32', 'WindowsPowerShell', 'v1.0', 'powershell.exe'), [
    '-NoProfile', '-NonInteractive', '-Command', script,
  ], {
    cwd: process.cwd(), env, encoding: 'utf8', stdio: ['ignore', 'pipe', 'ignore'], timeout: 30_000,
    maxBuffer: 1_048_576,
  }).trim();
  const digestAfter = createHash('sha256').update(readFileSync(DOCKER_CLI)).digest('hex');
  if (output !== 'TRUSTED' || digestBefore !== digestAfter) {
    throw new Error('LOCAL_DOCKER_CLI_NOT_TRUSTED');
  }
  return digestAfter;
}

export interface TrustedLocalDockerEngine {
  executable: string;
  engineHost: string;
  serverVersion: string;
  assertUnchanged(): void;
}

/** Verified Docker Desktop transport shared by the worker and acceptance supervisor. */
export function resolveTrustedLocalDockerEngine(options: {
  environment?: NodeJS.ProcessEnv;
  execFileSync?: typeof execFileSync;
  dockerExecutable?: string;
} = {}): TrustedLocalDockerEngine {
  if (process.platform !== 'win32' && !options.dockerExecutable) {
    throw new Error('LOCAL_DOCKER_CLI_NOT_TRUSTED');
  }
  const source = options.environment || process.env;
  const run = options.execFileSync || execFileSync;
  const executable = options.dockerExecutable || DOCKER_CLI;
  const cliHash = options.dockerExecutable ? 'injected-test-runtime' : verifyDockerCli(source, run);
  const env = buildVerificationEnvironment(source);
  const runDocker = (args: string[]) => run(executable, args, {
    cwd: process.cwd(), env, encoding: 'utf8', stdio: ['ignore', 'pipe', 'ignore'],
    timeout: 20_000, maxBuffer: 65_536,
  }).trim();
  try {
    const serverVersion = runDocker(['info', '--format', '{{.ServerVersion}}']);
    if (!serverVersion || runDocker(['info', '--format', '{{.OSType}}']) !== 'linux'
      || runDocker(['info', '--format', '{{.Name}}']) !== 'docker-desktop') {
      throw new Error('LOCAL_DOCKER_ENGINE_IDENTITY_MISMATCH');
    }
    return {
      executable,
      engineHost: DOCKER_ENGINE_PIPE,
      serverVersion,
      assertUnchanged() {
        if (!options.dockerExecutable && verifyDockerCli(source, run) !== cliHash) {
          throw new Error('LOCAL_DOCKER_CLI_CHANGED');
        }
        if (runDocker(['info', '--format', '{{.ServerVersion}}']) !== serverVersion
          || runDocker(['info', '--format', '{{.OSType}}']) !== 'linux'
          || runDocker(['info', '--format', '{{.Name}}']) !== 'docker-desktop') {
          throw new Error('LOCAL_DOCKER_ENGINE_IDENTITY_CHANGED');
        }
      },
    };
  } catch {
    throw new Error('LOCAL_DOCKER_RUNTIME_NOT_VERIFIED');
  }
}

export function resolveTrustedLocalDockerRuntime(options: {
  imageId: string;
  expectedLabels: Readonly<Record<string, string>>;
  environment?: NodeJS.ProcessEnv;
  execFileSync?: typeof execFileSync;
  dockerExecutable?: string;
}): TrustedLocalDockerRuntime {
  if (process.platform !== 'win32' && !options.dockerExecutable) {
    throw new Error('LOCAL_DOCKER_CLI_NOT_TRUSTED');
  }
  if (!/^sha256:[a-f0-9]{64}$/i.test(options.imageId)) {
    throw new Error('LOCAL_WORKER_IMAGE_ID_INVALID');
  }
  const expectedLabels = options.expectedLabels;
  if (!expectedLabels || Object.keys(expectedLabels).sort().join(',')
    !== 'org.blessing.git.sha,org.blessing.source.sha256'
    || !/^[a-f0-9]{40}$/.test(expectedLabels['org.blessing.git.sha'])
    || !/^[a-f0-9]{64}$/.test(expectedLabels['org.blessing.source.sha256'])) {
    throw new Error('LOCAL_WORKER_IMAGE_ATTESTATION_INVALID');
  }
  const source = options.environment || process.env;
  const run = options.execFileSync || execFileSync;
  const executable = options.dockerExecutable || DOCKER_CLI;
  const cliHash = options.dockerExecutable ? 'injected-test-runtime' : verifyDockerCli(source, run);
  const env = buildVerificationEnvironment(source);
  const runDocker = (args: string[]) => run(executable, args, {
    cwd: process.cwd(), env, encoding: 'utf8', stdio: ['ignore', 'pipe', 'ignore'],
    timeout: 20_000, maxBuffer: 65_536,
  }).trim();
  try {
    const serverVersion = runDocker(['info', '--format', '{{.ServerVersion}}']);
    const engineOS = runDocker(['info', '--format', '{{.OSType}}']);
    const engineName = runDocker(['info', '--format', '{{.Name}}']);
    const inspectImage = () => {
      const inspected = JSON.parse(runDocker(['image', 'inspect', options.imageId])) as unknown;
      if (!Array.isArray(inspected) || inspected.length !== 1 || !inspected[0]
        || typeof inspected[0] !== 'object') throw new Error('LOCAL_WORKER_IMAGE_INSPECT_INVALID');
      const image = inspected[0] as { Id?: unknown; Config?: { Labels?: unknown } };
      const labels = image.Config?.Labels;
      if (image.Id !== options.imageId) throw new Error('LOCAL_WORKER_IMAGE_CHANGED');
      if (!labels || typeof labels !== 'object' || Array.isArray(labels)
        || Object.entries(expectedLabels).some(([key, value]) => (labels as Record<string, unknown>)[key] !== value)) {
        throw new Error('LOCAL_WORKER_IMAGE_ATTESTATION_CHANGED');
      }
      return image.Id;
    };
    const imageId = inspectImage();
    if (!serverVersion || engineOS !== 'linux' || engineName !== 'docker-desktop'
      || imageId !== options.imageId) {
      throw new Error('LOCAL_DOCKER_RUNTIME_IDENTITY_MISMATCH');
    }
    return {
      executable,
      imageId,
      expectedLabels: Object.freeze({ ...expectedLabels }),
      serverVersion,
      engineHost: DOCKER_ENGINE_PIPE,
      assertUnchanged() {
        if (!options.dockerExecutable && verifyDockerCli(source, run) !== cliHash) {
          throw new Error('LOCAL_DOCKER_CLI_CHANGED');
        }
        const currentServerVersion = runDocker(['info', '--format', '{{.ServerVersion}}']);
        const currentEngineOS = runDocker(['info', '--format', '{{.OSType}}']);
        const currentEngineName = runDocker(['info', '--format', '{{.Name}}']);
        const currentImage = inspectImage() as string;
        if (currentServerVersion !== serverVersion || currentEngineOS !== 'linux'
          || currentEngineName !== 'docker-desktop') {
          throw new Error('LOCAL_DOCKER_ENGINE_IDENTITY_CHANGED');
        }
        if (currentImage !== options.imageId) throw new Error('LOCAL_WORKER_IMAGE_CHANGED');
      },
    };
  } catch {
    throw new Error('LOCAL_DOCKER_RUNTIME_NOT_VERIFIED');
  }
}
