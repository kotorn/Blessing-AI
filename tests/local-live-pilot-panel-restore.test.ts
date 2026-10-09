import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { ReactElement } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';

// Minimal hook shim: the panel is invoked directly so its mount-time effects and
// state can be exercised without a DOM. Effects run once per mounted instance,
// which is the only lifecycle this test needs.
const hooks = vi.hoisted(() => {
  type Slot = { value: unknown };
  type Instance = { slots: Slot[]; cursor: number; effects: Array<() => void | (() => void)>; cleanups: Array<() => void> };
  const live: { current: Instance | null } = { current: null };
  return {
    live,
    useState<T>(initial: T | (() => T)) {
      const inst = live.current!;
      const index = inst.cursor++;
      if (!inst.slots[index]) {
        inst.slots[index] = { value: typeof initial === 'function' ? (initial as () => T)() : initial };
      }
      const slot = inst.slots[index];
      return [slot.value, (next: unknown) => {
        slot.value = typeof next === 'function' ? (next as (prev: unknown) => unknown)(slot.value) : next;
      }] as const;
    },
    useEffect(effect: () => void | (() => void)) {
      live.current!.effects.push(effect);
    },
    useCallback<T>(fn: T) {
      return fn;
    },
    useRef<T>(initial: T) {
      const [ref] = hooks.useState(() => ({ current: initial }));
      return ref;
    },
  };
});

vi.mock('react', async (importOriginal) => {
  const actual = await importOriginal<typeof import('react')>();
  return {
    ...actual,
    default: actual,
    useState: hooks.useState,
    useEffect: hooks.useEffect,
    useCallback: hooks.useCallback,
    useRef: hooks.useRef,
  };
});

const api = vi.hoisted(() => ({ get: vi.fn(), post: vi.fn() }));
vi.mock('../src/api/client', () => ({ apiClient: api }));

import { LocalLivePilotPanel } from '../src/components/LocalLivePilotPanel';

const STORAGE_KEY = 'local-live-pilot:campaignId';
const CAMPAIGN_ID = 'pilot-7f3a91c2';

type Instance = { slots: Array<{ value: unknown }>; cursor: number; effects: Array<() => void | (() => void)>; cleanups: Array<() => void> };

function mount(): Instance {
  const inst: Instance = { slots: [], cursor: 0, effects: [], cleanups: [] };
  hooks.live.current = inst;
  try {
    (LocalLivePilotPanel as unknown as () => ReactElement)();
  } finally {
    hooks.live.current = null;
  }
  for (const effect of inst.effects) {
    const cleanup = effect();
    if (typeof cleanup === 'function') inst.cleanups.push(cleanup);
  }
  inst.effects = [];
  return inst;
}

function unmount(inst: Instance) {
  inst.cleanups.forEach((cleanup) => cleanup());
}

function render(inst: Instance): ReactElement {
  hooks.live.current = inst;
  inst.cursor = 0;
  inst.effects = [];
  try {
    return (LocalLivePilotPanel as unknown as () => ReactElement)();
  } finally {
    hooks.live.current = null;
  }
}

function textOf(node: unknown): string {
  if (node == null || typeof node === 'boolean') return '';
  if (typeof node === 'string' || typeof node === 'number') return String(node);
  if (Array.isArray(node)) return node.map(textOf).join('');
  if (typeof node === 'object' && 'props' in node) return textOf((node as ReactElement<{ children?: unknown }>).props.children);
  return '';
}

function findButton(node: unknown, label: string): ReactElement<{ disabled?: boolean; onClick?: () => void }> | null {
  if (Array.isArray(node)) {
    for (const child of node) {
      const found = findButton(child, label);
      if (found) return found;
    }
    return null;
  }
  if (!node || typeof node !== 'object' || !('props' in node)) return null;
  const element = node as ReactElement<{ children?: unknown }>;
  if (element.type === 'button' && textOf(element.props.children).includes(label)) {
    return element as ReactElement<{ disabled?: boolean; onClick?: () => void }>;
  }
  return findButton(element.props.children, label);
}

function findAcknowledgement(node: unknown): ReactElement<{ checked: boolean; onChange: (event: { target: { checked: boolean } }) => void }> | null {
  if (Array.isArray(node)) {
    for (const child of node) {
      const found = findAcknowledgement(child);
      if (found) return found;
    }
    return null;
  }
  if (!node || typeof node !== 'object' || !('props' in node)) return null;
  const element = node as ReactElement<{ children?: unknown; name?: string }>;
  if (element.type === 'input' && element.props.name === 'papiProtectionRiskAcknowledged') {
    return element as ReturnType<typeof findAcknowledgement>;
  }
  return findAcknowledgement(element.props.children);
}

const flush = () => new Promise((resolve) => setTimeout(resolve, 0));

const readiness = (canStart: boolean) => ({
  status: 'READY', canApprove: true, canStart, blockers: [],
});

const pendingCampaign = {
  campaignId: CAMPAIGN_ID,
  status: 'PENDING_APPROVAL',
  strategyId: 'trend',
  campaignExpiresAt: '2026-10-16T00:00:00.000Z',
  version: 1,
  evidence_status: 'VERIFIED',
  readiness: readiness(false),
  preparation: { status: 'NOT_RUN', observedAt: null },
  runtime: 'UNKNOWN',
  accounting: { status: 'UNKNOWN', reason: 'PILOT_ACCOUNTING_EVIDENCE_UNAVAILABLE' },
};

const activeCampaign = {
  ...pendingCampaign,
  status: 'ACTIVE',
  version: 4,
  evidence_status: 'UNVERIFIED',
};

let serverCampaign: Record<string, unknown> = pendingCampaign;
let supervisedCampaignId: string | null = null;
let storageMap: Map<string, string>;

function installSessionStorage(store: Storage | null) {
  Object.defineProperty(globalThis, 'sessionStorage', { configurable: true, get: () => {
    if (store === null) throw new Error('storage denied');
    return store;
  } });
}

beforeEach(() => {
  storageMap = new Map();
  const memory = {
    getItem: (key: string) => storageMap.get(key) ?? null,
    setItem: (key: string, value: string) => { storageMap.set(key, value); },
    removeItem: (key: string) => { storageMap.delete(key); },
    clear: () => storageMap.clear(),
    key: () => null,
    get length() { return storageMap.size; },
  } as unknown as Storage;
  installSessionStorage(memory);
  serverCampaign = pendingCampaign;
  supervisedCampaignId = null;
  api.get.mockReset();
  api.post.mockReset();
  api.get.mockImplementation(async (url: string) => {
    if (url === '/api/local/runtime') {
      return { supervisor: supervisedCampaignId ? { pilotCampaignId: supervisedCampaignId } : null };
    }
    if (url === '/api/local/pilot/readiness') return { readiness: readiness(false) };
    if (url === `/api/local/pilot/${CAMPAIGN_ID}`) return serverCampaign;
    throw new Error(`unexpected GET ${url}`);
  });
  api.post.mockImplementation(async (url: string) => {
    if (url === '/api/local/pilot/request') return pendingCampaign;
    throw new Error(`unexpected POST ${url}`);
  });
});

afterEach(() => {
  delete (globalThis as { sessionStorage?: Storage }).sessionStorage;
});

describe('LocalLivePilotPanel campaign rediscovery after remount', () => {
  it('requires a fresh acknowledgement before approval and includes it in the request', async () => {
    supervisedCampaignId = CAMPAIGN_ID;
    const inst = mount();
    await flush();
    expect(findButton(render(inst), 'อนุมัติแคมเปญ')?.props.disabled).toBe(true);
    const acknowledgement = findAcknowledgement(render(inst));
    expect(acknowledgement).not.toBeNull();
    acknowledgement!.props.onChange({ target: { checked: true } });
    api.post.mockResolvedValue(pendingCampaign);
    findButton(render(inst), 'อนุมัติแคมเปญ')!.props.onClick?.();
    await flush();
    expect(api.post).toHaveBeenCalledWith('/api/local/pilot/approve', {
      campaignId: CAMPAIGN_ID, papiProtectionRiskAcknowledged: true,
    });
    unmount(inst);
    const restored = mount();
    await flush();
    expect(findAcknowledgement(render(restored))?.props.checked).toBe(false);
    unmount(restored);
  });

  it('shows PAPI uncertainty and restores only the server session deadlines', async () => {
    supervisedCampaignId = CAMPAIGN_ID;
    serverCampaign = { ...activeCampaign, session: {
      armedAt: '2026-10-09T10:00:00Z', entryCutoffAt: '2026-10-09T11:30:00Z',
      closeAfterAt: '2026-10-09T11:50:00Z', endAt: '2026-10-09T12:00:00Z', stage: 'CLOSE_ONLY',
    } };
    const inst = mount();
    await flush();
    const html = renderToStaticMarkup(render(inst));
    expect(html).toContain('SL/TP');
    expect(html).toContain('NOT_RUN');
    expect(html).toContain('CLOSE_ONLY');
    expect(html).toContain('Kill switch');
    unmount(inst);
  });

  it('restores the active campaign and its close-only/revoke controls after navigation', async () => {
    const first = mount();
    await flush();
    const requestButton = findButton(render(first), 'สร้างคำขออนุมัติ');
    expect(requestButton).not.toBeNull();
    requestButton!.props.onClick?.();
    await flush();
    unmount(first);

    expect(storageMap.get(STORAGE_KEY)).toBe(CAMPAIGN_ID);

    serverCampaign = activeCampaign;
    const second = mount();
    await flush();
    const html = renderToStaticMarkup(render(second));

    expect(api.get).toHaveBeenCalledWith(`/api/local/pilot/${CAMPAIGN_ID}`);
    expect(html).toContain(CAMPAIGN_ID);
    expect(findButton(render(second), 'หยุดเพิ่มความเสี่ยง')).not.toBeNull();
    expect(findButton(render(second), 'เพิกถอน')).not.toBeNull();
    expect(findButton(render(second), 'สร้างคำขออนุมัติ')?.props.disabled).toBe(true);
    // Restoring state must not enable the ARM button the server gate still disables.
    expect(findButton(render(second), 'เริ่ม Live Pilot')?.props.disabled).toBe(true);
  });

  it('does not restore a terminal campaign and clears the stored id so a new request is possible', async () => {
    storageMap.set(STORAGE_KEY, CAMPAIGN_ID);
    serverCampaign = { ...activeCampaign, status: 'REVOKED' };

    const inst = mount();
    await flush();
    const element = render(inst);

    expect(storageMap.has(STORAGE_KEY)).toBe(false);
    expect(renderToStaticMarkup(element)).not.toContain(CAMPAIGN_ID);
    expect(findButton(element, 'สร้างคำขออนุมัติ')?.props.disabled).toBe(false);
  });

  it('restores the campaign the server is supervising when browser storage is empty', async () => {
    supervisedCampaignId = CAMPAIGN_ID;
    serverCampaign = activeCampaign;

    const inst = mount();
    await flush();
    const html = renderToStaticMarkup(render(inst));

    expect(api.get).toHaveBeenCalledWith(`/api/local/pilot/${CAMPAIGN_ID}`);
    expect(html).toContain(CAMPAIGN_ID);
    expect(findButton(render(inst), 'หยุดเพิ่มความเสี่ยง')).not.toBeNull();
    expect(storageMap.get(STORAGE_KEY)).toBe(CAMPAIGN_ID);
  });

  it('does not overwrite a campaign the operator created while the restore was in flight', async () => {
    storageMap.set(STORAGE_KEY, CAMPAIGN_ID);
    let releaseRestore: () => void = () => {};
    let campaignReads = 0;
    api.get.mockImplementation(async (url: string) => {
      if (url === '/api/local/runtime') return { supervisor: null };
      if (url === '/api/local/pilot/readiness') return { readiness: readiness(false) };
      if (url === `/api/local/pilot/${CAMPAIGN_ID}`) {
        campaignReads += 1;
        if (campaignReads === 1) {
          await new Promise<void>((resolve) => { releaseRestore = resolve; });
          return activeCampaign;
        }
        return serverCampaign;
      }
      throw new Error(`unexpected GET ${url}`);
    });

    const inst = mount();
    await flush();
    findButton(render(inst), 'สร้างคำขออนุมัติ')!.props.onClick?.();
    await flush();
    releaseRestore();
    await flush();
    const element = render(inst);

    expect(findButton(element, 'อนุมัติแคมเปญ')).not.toBeNull();
    expect(findButton(element, 'หยุดเพิ่มความเสี่ยง')).toBeNull();
  });

  it('forgets a stored campaign id that the server reports as not found', async () => {
    storageMap.set(STORAGE_KEY, CAMPAIGN_ID);
    api.get.mockImplementation(async (url: string) => {
      if (url === '/api/local/runtime') return { supervisor: null };
      if (url === '/api/local/pilot/readiness') return { readiness: readiness(false) };
      throw Object.assign(new Error('LOCAL_PILOT_NOT_FOUND'), { status: 404, data: { error: 'LOCAL_PILOT_NOT_FOUND' } });
    });

    const inst = mount();
    await flush();
    renderToStaticMarkup(render(inst));

    expect(storageMap.has(STORAGE_KEY)).toBe(false);
  });

  it('renders normally when sessionStorage is unavailable', async () => {
    installSessionStorage(null);

    const inst = mount();
    await flush();
    const element = render(inst);

    expect(renderToStaticMarkup(element)).toContain('สร้างคำขออนุมัติ');
    expect(findButton(element, 'เพิกถอน')).toBeNull();
  });
});
