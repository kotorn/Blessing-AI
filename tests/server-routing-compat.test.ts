import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import express from 'express';
import { describe, expect, it } from 'vitest';

const server = readFileSync(resolve(process.cwd(), 'server.ts'), 'utf8');

describe('Express fallback route compatibility', () => {
  it('uses Express 5 wildcard syntax for API and SPA fallbacks', () => {
    expect(server).toContain("app.all('/api/{*splat}'");
    expect(server).toContain("app.get('/{*splat}'");
    expect(server).not.toContain("app.all('/api/*'");
    expect(server).not.toContain("app.get('*'");

    const app = express();
    expect(() => app.all('/api/{*splat}', (_req, res) => res.status(404).end())).not.toThrow();
    expect(() => app.get('/{*splat}', (_req, res) => res.status(200).end())).not.toThrow();
  });
});
