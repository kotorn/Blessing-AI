import { beforeEach, expect, it, vi } from 'vitest';
vi.mock('node:child_process', () => ({ execFileSync: vi.fn() }));
import { execFileSync } from 'node:child_process';
import { createHash } from 'node:crypto';
import { readPilotCommittedSource } from '../src/backend/local-pilot-committed-source.js';
const sha = 'a'.repeat(40);
const tree = 'b'.repeat(40);
const contents = Buffer.from('committed source');
const blob = createHash('sha1').update(`blob ${contents.length}\0`).update(contents).digest('hex');
beforeEach(() => vi.resetAllMocks());
function fixture(name = 'src/app.ts', mode = '100644') {
  vi.mocked(execFileSync).mockReturnValueOnce(Buffer.from(sha + '\n'))
    .mockReturnValueOnce(Buffer.from(tree + '\n'))
    .mockReturnValueOnce(Buffer.from(`${mode} blob ${blob}\t${name}\0`))
    .mockReturnValueOnce(contents);
}
it('reads blobs by object identity with no shell or checkout reads', () => {
  fixture();
  const source = readPilotCommittedSource(process.cwd(), sha);
  expect(source.treeId).toBe(tree);
  expect(source.files[0].bytes.toString()).toBe('committed source');
  expect(execFileSync).toHaveBeenLastCalledWith('git', ['cat-file', 'blob', blob],
    expect.objectContaining({ shell: false }));
});
it('rejects tracked credentials and links before reading blob contents', () => {
  fixture('.env.local');
  expect(() => readPilotCommittedSource(process.cwd(), sha)).toThrow('UNSAFE');
  expect(execFileSync).toHaveBeenCalledTimes(3);
  vi.resetAllMocks(); fixture('link', '120000');
  expect(() => readPilotCommittedSource(process.cwd(), sha)).toThrow('UNSAFE');
  expect(execFileSync).toHaveBeenCalledTimes(3);
});
it('rejects arbitrary refs before invoking Git', () => {
  expect(() => readPilotCommittedSource(process.cwd(), 'HEAD')).toThrow('COMMIT_INVALID');
  expect(execFileSync).not.toHaveBeenCalled();
});

it('rejects blob bytes that do not match the selected Git object identity', () => {
  vi.mocked(execFileSync).mockReturnValueOnce(Buffer.from(sha + '\n'))
    .mockReturnValueOnce(Buffer.from(tree + '\n'))
    .mockReturnValueOnce(Buffer.from(`100644 blob ${blob}\tsrc/app.ts\0`))
    .mockReturnValueOnce(Buffer.from('substituted bytes'));
  expect(() => readPilotCommittedSource(process.cwd(), sha)).toThrow('SOURCE_BLOB_MISMATCH');
});
