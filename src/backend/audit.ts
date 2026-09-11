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
}

class InMemoryAuditRepository {
  private events: AuditEvent[] = [];

  public logEvent(event: Omit<AuditEvent, 'eventId' | 'timestamp'>) {
    const fullEvent: AuditEvent = {
      ...event,
      eventId: `EVT-${Date.now()}-${Math.floor(Math.random() * 10000)}`,
      timestamp: new Date().toISOString()
    };
    this.events.push(fullEvent);
    console.log(`[AUDIT] ${fullEvent.eventType} | ${fullEvent.reason || ''}`, fullEvent.metadata || '');
    return fullEvent;
  }

  public getEvents(): AuditEvent[] {
    return [...this.events];
  }
}

export const auditRepository = new InMemoryAuditRepository();
