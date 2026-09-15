import { spawnSync } from 'node:child_process';
import { readdirSync, readFileSync, statSync, writeFileSync } from 'node:fs';
import { join, resolve } from 'node:path';

const firebaseCommand = process.execPath;
const firebaseEntrypoint = resolve('node_modules/firebase-tools/lib/bin/firebase.js');
const result = spawnSync(
  firebaseCommand,
  [
    firebaseEntrypoint,
    'dataconnect:sdk:generate',
    '--service',
    'blessing-app',
    '--location',
    'asia-southeast1',
  ],
  { stdio: 'inherit' },
);

if (result.error) {
  console.error(`Failed to start Firebase CLI: ${result.error.message}`);
  process.exit(1);
}
if (result.status !== 0) {
  process.exit(result.status ?? 1);
}

const generatedRoot = resolve(process.cwd(), 'src/dataconnect-generated');

function normalizeGeneratedFiles(directory) {
  for (const entry of readdirSync(directory)) {
    const path = join(directory, entry);
    if (statSync(path).isDirectory()) {
      normalizeGeneratedFiles(path);
      continue;
    }
    const normalized = readFileSync(path, 'utf8')
      .replace(/\r\n/g, '\n')
      .replace(/[ \t]+$/gm, '')
      .replace(/\n*$/, '\n');
    writeFileSync(path, normalized, 'utf8');
  }
}

normalizeGeneratedFiles(generatedRoot);
