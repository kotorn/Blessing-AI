import { applicationDefault, getApps, initializeApp } from 'firebase-admin/app';
import { getFirestore, type Firestore } from 'firebase-admin/firestore';
import crypto from 'node:crypto';
import { EngineState, ExecutionMode } from './types';

export interface AuditEvent {
  eventId: string;
  timestamp: string;
  correlationId?: string;
  eventType: string;
  previousState?: EngineState;
  newState?: EngineState;
  executionMode?: ExecutionMode;
  reason?: string;
  metadata?: any;
  /** False when the durable write failed and the event was retained in memory only. */
  persisted?: boolean;
}

export interface AuditRepository {
  /** Persists one operator action; resolves after the durable write (or its fallback). */
  logEvent(event: Omit<AuditEvent, 'eventId' | 'timestamp'>): Promise<AuditEvent>;
  /** Most recent events, newest first (durable store) or insertion order (memory). */
  getEvents(): Promise<AuditEvent[]>;
}

const AUDIT_COLLECTION = 'operator_audit_events';
const MEMORY_CAPACITY = 1000;

function materializeEvent(event: Omit<AuditEvent, 'eventId' | 'timestamp'>): AuditEvent {
  return {
    ...event,
    eventId: `EVT-${crypto.randomUUID()}`,
    timestamp: new Date().toISOString(),
  };
}

function describeError(error: unknown): string {
  return error instanceof Error ? error.name : 'unknown';
}

function logConsole(event: AuditEvent) {
  console.log(`[AUDIT] ${event.eventType} | ${event.reason || ''}`, event.metadata || '');
}

export class InMemoryAuditRepository implements AuditRepository {
  private events: AuditEvent[] = [];

  constructor(private readonly capacity: number = MEMORY_CAPACITY) {}

  public async logEvent(event: Omit<AuditEvent, 'eventId' | 'timestamp'>): Promise<AuditEvent> {
    const fullEvent = materializeEvent(event);
    this.events.push(fullEvent);
    if (this.events.length > this.capacity) {
      this.events.splice(0, this.events.length - this.capacity);
    }
    logConsole(fullEvent);
    return { ...fullEvent, persisted: true };
  }

  public async getEvents(): Promise<AuditEvent[]> {
    return [...this.events].reverse();
  }
}

export class FirestoreAuditRepository implements AuditRepository {
  private fallback: InMemoryAuditRepository | null = null;

  constructor(private readonly dbProvider: () => Firestore = defaultDbProvider) {}

  public async logEvent(event: Omit<AuditEvent, 'eventId' | 'timestamp'>): Promise<AuditEvent> {
    const fullEvent = materializeEvent(event);
    try {
      await this.dbProvider().collection(AUDIT_COLLECTION).doc(fullEvent.eventId).create(fullEvent);
      logConsole(fullEvent);
      return { ...fullEvent, persisted: true };
    } catch (error) {
      // The operator action already executed on the Worker, so the route must
      // not report failure here. Retain the event in memory and mark it
      // un-persisted so the durability gap is observable and alertable.
      console.error(
        `monitor_event=audit_persistence_failed event_id=${fullEvent.eventId} ` +
        `event_type=${fullEvent.eventType} error_class=${describeError(error)}`
      );
      const retained = await this.memoryFallback().logEvent(event);
      return { ...retained, eventId: fullEvent.eventId, timestamp: fullEvent.timestamp, persisted: false };
    }
  }

  public async getEvents(): Promise<AuditEvent[]> {
    try {
      const snapshot = await this.dbProvider()
        .collection(AUDIT_COLLECTION)
        .orderBy('timestamp', 'desc')
        .limit(200)
        .get();
      return snapshot.docs.map((doc) => ({ ...(doc.data() as AuditEvent), persisted: true }));
    } catch (error) {
      console.error(`monitor_event=audit_read_failed error_class=${describeError(error)}`);
      return this.memoryFallback().getEvents();
    }
  }

  private memoryFallback(): InMemoryAuditRepository {
    if (!this.fallback) this.fallback = new InMemoryAuditRepository();
    return this.fallback;
  }
}

function defaultDbProvider(): Firestore {
  const app = getApps()[0] || initializeApp({ credential: applicationDefault() });
  return getFirestore(app);
}

let repositoryInstance: AuditRepository | null = null;

/** AUDIT_BACKEND=memory forces the in-process repository (tests, local dev). */
export function getAuditRepository(): AuditRepository {
  if (!repositoryInstance) {
    repositoryInstance =
      process.env.AUDIT_BACKEND === 'memory'
        ? new InMemoryAuditRepository()
        : new FirestoreAuditRepository();
  }
  return repositoryInstance;
}

export const auditRepository: AuditRepository = getAuditRepository();
