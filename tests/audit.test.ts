import { describe, it, expect } from 'vitest';
import { InMemoryAuditRepository, FirestoreAuditRepository } from '../src/backend/audit';
import type { Firestore } from 'firebase-admin/firestore';
import type { AuditEvent } from '../src/backend/audit';

const sampleEvent = {
  eventType: 'KILL_SWITCH_ENGAGED',
  previousState: 'ARMED' as const,
  newState: 'EMERGENCY' as const,
  executionMode: 'LIVE' as const,
  reason: 'Kill switch engaged',
  metadata: { route: '/api/system/kill-switch' },
};

interface FakeCreateCall {
  id: string;
  data: unknown;
}

function fakeFirestore(options: { failCreate?: boolean; docs?: AuditEvent[] }) {
  const created: FakeCreateCall[] = [];
  const db = {
    collection: (_name: string) => ({
      doc: (id: string) => ({
        create: async (data: unknown) => {
          if (options.failCreate) throw new Error('no ADC available');
          created.push({ id, data });
        },
      }),
      orderBy: () => ({
        limit: () => ({
          get: async () => {
            if (options.failCreate) throw new Error('no ADC available');
            return { docs: (options.docs || []).map((d) => ({ data: () => d })) };
          },
        }),
      }),
    }),
  } as unknown as Firestore;
  return { db, created };
}

describe('InMemoryAuditRepository', () => {
  it('materializes eventId and timestamp and marks the event persisted', async () => {
    const repo = new InMemoryAuditRepository();
    const event = await repo.logEvent(sampleEvent);

    expect(event.eventId).toMatch(/^EVT-/);
    expect(event.timestamp).toBeTruthy();
    expect(event.persisted).toBe(true);
    expect(event.eventType).toBe('KILL_SWITCH_ENGAGED');
    expect(event.metadata).toEqual({ route: '/api/system/kill-switch' });
  });

  it('returns newest first and drops oldest events beyond capacity', async () => {
    const repo = new InMemoryAuditRepository(3);
    for (let i = 0; i < 5; i++) {
      await repo.logEvent({ ...sampleEvent, eventType: `EVT_TYPE_${i}` });
    }

    const events = await repo.getEvents();
    expect(events).toHaveLength(3);
    expect(events.map((e) => e.eventType)).toEqual(['EVT_TYPE_4', 'EVT_TYPE_3', 'EVT_TYPE_2']);
  });
});

describe('FirestoreAuditRepository', () => {
  it('persists events durably on the happy path', async () => {
    const { db, created } = fakeFirestore({});
    const repo = new FirestoreAuditRepository(() => db);

    const event = await repo.logEvent(sampleEvent);

    expect(event.persisted).toBe(true);
    expect(created).toHaveLength(1);
    expect(created[0].id).toBe(event.eventId);
    expect((created[0].data as AuditEvent).eventType).toBe('KILL_SWITCH_ENGAGED');
  });

  it('fails open with an in-memory fallback when the durable write rejects', async () => {
    const { db } = fakeFirestore({ failCreate: true });
    const repo = new FirestoreAuditRepository(() => db);

    const event = await repo.logEvent(sampleEvent);

    expect(event.persisted).toBe(false);
    expect(event.eventId).toMatch(/^EVT-/);

    const events = await repo.getEvents();
    expect(events).toHaveLength(1);
    expect(events[0].eventType).toBe('KILL_SWITCH_ENGAGED');
  });

  it('reads the newest durable events', async () => {
    const durable: AuditEvent[] = [
      { ...sampleEvent, eventId: 'EVT-a', timestamp: '2026-09-19T10:00:00.000Z' },
      { ...sampleEvent, eventId: 'EVT-b', timestamp: '2026-09-19T11:00:00.000Z' },
    ];
    const { db } = fakeFirestore({ docs: durable });
    const repo = new FirestoreAuditRepository(() => db);

    const events = await repo.getEvents();
    expect(events.map((e) => e.eventId)).toEqual(['EVT-a', 'EVT-b']);
    expect(events.every((e) => e.persisted)).toBe(true);
  });
});
