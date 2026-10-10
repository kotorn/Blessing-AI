import http from 'node:http';
import type { AddressInfo } from 'node:net';
import { afterEach, describe, expect, it } from 'vitest';
import { startHeartbeatThread } from '../src/backend/local-heartbeat-thread.js';

describe('supervisor heartbeat thread', () => {
  let server: http.Server | undefined;
  afterEach(() => new Promise<void>((resolve) => (server ? server.close(() => resolve()) : resolve())));

  const listen = async () => {
    const received: Array<{ auth: string | undefined; body: string }> = [];
    server = http.createServer((req, res) => {
      let body = '';
      req.on('data', (c) => { body += c; });
      req.on('end', () => { received.push({ auth: req.headers.authorization, body }); res.end('{}'); });
    });
    await new Promise<void>((r) => server!.listen(0, '127.0.0.1', r));
    return { received, url: `http://127.0.0.1:${(server!.address() as AddressInfo).port}/supervisor/heartbeat` };
  };

  it('keeps beating while the main event loop is blocked', async () => {
    const { received, url } = await listen();
    const beat = startHeartbeatThread({ url, token: 't0k', intervalMs: 100 });
    await new Promise((r) => setTimeout(r, 300));
    const before = received.length;
    const end = Date.now() + 2_000;
    while (Date.now() < end) { /* synchronous block, like execFileSync */ }
    await new Promise((r) => setTimeout(r, 50));
    beat.stop();
    expect(before).toBeGreaterThan(0);
    // the server's loop is blocked too, but requests queue in the socket and the thread keeps sending
    expect(received.length - before).toBeGreaterThanOrEqual(5);
    expect(received[0].auth).toBe('Bearer t0k');
  }, 15_000);

  it('forwards the latest verdict', async () => {
    const { received, url } = await listen();
    const beat = startHeartbeatThread({ url, token: 'x', intervalMs: 50 });
    beat.setVerdict({ id: 'v1' });
    await new Promise((r) => setTimeout(r, 400));
    beat.stop();
    expect(received.some((r) => r.body.includes('"id":"v1"'))).toBe(true);
  }, 15_000);
});
