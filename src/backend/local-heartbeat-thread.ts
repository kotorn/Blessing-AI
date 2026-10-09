import { Worker } from 'node:worker_threads';

/**
 * Supervisor heartbeat that survives a blocked control-plane event loop.
 *
 * Readiness verification uses synchronous child-process calls that can block the
 * main thread for many seconds; the Worker's watchdog disarms and exits when no
 * heartbeat arrives within its 15 s TTL. The heartbeat therefore runs on its own
 * thread. The main thread only forwards the latest signed verdict to it.
 */
const THREAD_SOURCE = `
const { parentPort, workerData } = require('node:worker_threads');
let verdict = null;
parentPort.on('message', (message) => {
  if (message && message.type === 'verdict') verdict = message.verdict ?? null;
});
async function beat() {
  try {
    await fetch(workerData.url, {
      method: 'POST',
      headers: { Authorization: 'Bearer ' + workerData.token, 'Content-Type': 'application/json' },
      body: JSON.stringify({ pilotReadinessVerdict: verdict }),
      signal: AbortSignal.timeout(2500),
    });
  } catch {
    // A failed heartbeat is intentionally not masked; the Worker watchdog decides.
  }
}
void beat();
setInterval(() => { void beat(); }, workerData.intervalMs);
`;

export interface HeartbeatThread {
  setVerdict(verdict: unknown): void;
  stop(): void;
}

export function startHeartbeatThread(options: {
  url: string;
  token: string;
  intervalMs: number;
  verdict?: unknown;
}): HeartbeatThread {
  const worker = new Worker(THREAD_SOURCE, {
    eval: true,
    workerData: { url: options.url, token: options.token, intervalMs: options.intervalMs },
  });
  worker.on('error', () => { /* the Worker watchdog is the authority on liveness */ });
  const post = (verdict: unknown) => {
    try { worker.postMessage({ type: 'verdict', verdict: verdict ?? null }); } catch { /* terminated */ }
  };
  if (options.verdict !== undefined) post(options.verdict);
  return {
    setVerdict: post,
    stop() { void worker.terminate(); },
  };
}
