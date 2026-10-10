import { execFileSync } from 'node:child_process';

const output = execFileSync('git', ['ls-files', '--eol', '-z'], {
  encoding: 'utf8',
  maxBuffer: 16 * 1024 * 1024,
});

const crlfFiles = [];
for (const entry of output.split('\0')) {
  if (!entry) continue;
  if (entry.includes('w/crlf')) {
    const parts = entry.split('\t');
    const filename = parts.slice(1).join('\t').trim();
    crlfFiles.push(filename);
  }
}

if (crlfFiles.length > 0) {
  console.error(`ERROR: Found ${crlfFiles.length} file(s) with CRLF in working copy:`);
  for (const f of crlfFiles) {
    console.error(`  ${f}`);
  }
  process.exit(1);
}

console.log('EOL check passed: 0 files with w/crlf.');
