// ---------------------------------------------------------------------
// One shared agent-event stream, for any number of consumers.
//
// WHY THIS IS POLLING AND NOT A WEBSOCKET ANY MORE
//
// It used to open `ws://<backend>:8000/api/dashboard/agent-events` directly from
// the browser. On the real deployment that connection NEVER OPENS:
//
//   * the frontend is served from Vercel over https;
//   * the backend has no TLS certificate (no domain), so it speaks plain http/ws;
//   * a browser on an https page refuses `ws://` outright — mixed content. It is
//     a browser policy, so nothing in this file could have worked around it;
//   * and a WebSocket cannot be proxied through a Vercel serverless function, so
//     there was no tunnel to build either.
//
// The visible symptom was the worst kind: no error anywhere, just an agent
// terminal, a debate visualizer and a trade history table that stayed empty
// forever while the backend was running perfectly and publishing events.
//
// So the browser now polls `/api/agent-events`, a SAME-ORIGIN https route that
// proxies to the backend server-to-server, where no browser rules apply. The
// backend buffers events with a monotonic cursor
// (`backend/services/event_buffer.py`) precisely so that polling can ask "what
// happened since?" rather than only catching what fires mid-request.
//
// THE BACKEND'S WEBSOCKET IS STILL THERE and still works for anything that can
// reach the host directly. To go back to it, point this module at
// `agentEventsWsUrl()` again — but only once the backend has a TLS hostname,
// because that, and nothing in this file, is what actually blocks it.
//
// WHAT IS UNCHANGED
//
// This module still keeps ONE connection at module scope, reference-counted by
// subscriber, because five components consume it (AgentTerminal,
// AgentActivityTerminal, DebateVisualizer, TradeHistoryTable, TradeLogPanel) and
// each used to open its own socket — five connections, five independently filled
// buffers, and two panels able to disagree about what had just happened. The
// last consumer to unsubscribe stops the loop. The public API is byte-identical
// so no consumer had to change.
// ---------------------------------------------------------------------

export type AgentStreamEvent = {
  event_type: string;
  timestamp?: string;
  agent_id?: string;
  agent?: string;
  [key: string]: unknown;
};

export type StreamState = {
  events: AgentStreamEvent[];
  isConnected: boolean;
};

const MAX_BUFFERED_EVENTS = 200;

/** How often to ask for new events while the backend is answering. */
const POLL_INTERVAL_MS = 2_000;

// Backoff rather than a fixed retry: a backend that is down stays down for a
// while, and hammering it from every open tab achieves nothing except filling
// the console. Applied only to FAILED polls; a successful one resets it.
const RETRY_BASE_MS = 2_000;
const RETRY_MAX_MS = 30_000;

type Listener = (state: StreamState) => void;

let listeners = new Set<Listener>();
let buffer: AgentStreamEvent[] = [];
let connected = false;
let pollTimer: ReturnType<typeof setTimeout> | null = null;
let failedAttempts = 0;
let running = false;

// The server-side sequence number of the last event received. `null` means "I
// have never polled" — the backend answers that with the current head and NO
// backlog, so a freshly opened tab does not replay ten minutes of history as
// though it were happening now.
let cursor: number | null = null;

// Guards against two loops running at once. `useEffect` runs twice per mount
// under React 18 Strict Mode, which is exactly how a "shared" connection quietly
// becomes two — the same hazard the WebSocket version had to guard against.
let inFlight = false;

function snapshot(): StreamState {
  // A fresh array each time: handing out the internal buffer would let a
  // consumer mutate every other consumer's view of history.
  return { events: [...buffer], isConnected: connected };
}

function emit(): void {
  const state = snapshot();
  listeners.forEach((fn) => fn(state));
}

function clearPollTimer(): void {
  if (pollTimer !== null) {
    clearTimeout(pollTimer);
    pollTimer = null;
  }
}

function scheduleNextPoll(delayMs: number): void {
  clearPollTimer();
  // No consumers left — do not reschedule. Without this a poll in flight when
  // the last consumer unmounts would restart the loop for nobody.
  if (listeners.size === 0 || !running) return;
  pollTimer = setTimeout(() => {
    pollTimer = null;
    void poll();
  }, delayMs);
}

async function poll(): Promise<void> {
  if (inFlight || !running) return;
  inFlight = true;

  try {
    const query = cursor === null ? '' : `?cursor=${cursor}`;
    const res = await fetch(`/api/agent-events${query}`, { cache: 'no-store' });
    const json = await res.json();

    if (!res.ok || json?.error) {
      throw new Error(json?.error ?? `agent-events returned ${res.status}`);
    }

    if (typeof json.cursor === 'number') cursor = json.cursor;

    const incoming: AgentStreamEvent[] = Array.isArray(json.events) ? json.events : [];
    if (incoming.length > 0) {
      // Newest first, matching what the socket version delivered. The backend
      // returns them oldest-first (sequence order), so the batch is reversed
      // before prepending — without that, a batch of five would appear in the
      // terminal upside down.
      buffer = [...incoming.reverse(), ...buffer].slice(0, MAX_BUFFERED_EVENTS);
    }

    // `missed` means the client fell far enough behind that the backend's ring
    // buffer evicted events. Surfaced as a synthetic entry rather than ignored:
    // a timeline with a silent gap misrepresents what the agent did, which is
    // the one thing this panel exists not to do.
    if (json.missed) {
      buffer = [
        {
          event_type: 'STREAM_GAP',
          timestamp: new Date().toISOString(),
          agent: 'event-stream',
          detail: 'Some events were missed — this client fell behind the backend buffer.',
        },
        ...buffer,
      ].slice(0, MAX_BUFFERED_EVENTS);
    }

    connected = true;
    failedAttempts = 0;
    emit();
    scheduleNextPoll(POLL_INTERVAL_MS);
  } catch {
    // A failed poll is not fatal and not logged per-attempt — a backend that is
    // down would otherwise produce one console error every two seconds per tab.
    // `isConnected: false` is how consumers learn about it, and every panel
    // already renders that state.
    if (connected) {
      connected = false;
      emit();
    }
    const delay = Math.min(RETRY_BASE_MS * 2 ** failedAttempts, RETRY_MAX_MS);
    failedAttempts += 1;
    scheduleNextPoll(delay);
  } finally {
    inFlight = false;
  }
}

/** Subscribe to the shared stream. Returns an unsubscribe function. */
export function subscribeToAgentEvents(listener: Listener): () => void {
  listeners.add(listener);
  listener(snapshot()); // deliver current state immediately

  if (typeof window !== 'undefined' && !running) {
    running = true;
    void poll();
  }

  return () => {
    listeners.delete(listener);
    if (listeners.size === 0) {
      running = false;
      clearPollTimer();
      failedAttempts = 0;
      connected = false;
      // The cursor is deliberately KEPT. A panel that unmounts and remounts —
      // switching routes, say — resumes where it left off instead of resetting
      // to the head and appearing to lose everything that happened in between.
    }
  };
}

/** Clear the shared buffer. Affects every consumer, which is intended —
 *  there is one history, not one per panel. */
export function clearAgentEvents(): void {
  buffer = [];
  emit();
}

// ---------------------------------------------------------------------
// Safe field accessors.
//
// Events arrive as JSON from the bus, so every field beyond `event_type` is
// genuinely optional — `backend/api/dashboard.py::_event_to_dict` only includes
// `timestamp` when the event carries one, and different event types carry
// different payloads. The old hooks typed the payload as
// `[key: string]: any` and `timestamp: string`, which compiled fine and then
// produced `new Date(undefined)` → "Invalid Date" in the UI, and
// `confidence.toFixed()` → TypeError when the field was absent.
// ---------------------------------------------------------------------

/** Local time string for an event, or a placeholder when it carries no timestamp. */
export function eventTimeLabel(event: AgentStreamEvent): string {
  const raw = event.timestamp;
  if (typeof raw !== 'string' && typeof raw !== 'number') return '--:--:--';
  const date = new Date(raw);
  // An unparseable timestamp yields NaN. Showing "--:--:--" is honest; showing
  // "Invalid Date" looks like a rendering fault rather than missing data.
  if (Number.isNaN(date.getTime())) return '--:--:--';
  return date.toLocaleTimeString([], { hour12: false });
}

/** Epoch ms for an event, or null when it has no usable timestamp. */
export function eventTimeMs(event: AgentStreamEvent): number | null {
  const raw = event.timestamp;
  if (typeof raw !== 'string' && typeof raw !== 'number') return null;
  const ms = new Date(raw).getTime();
  return Number.isNaN(ms) ? null : ms;
}

/** A numeric field, or null. Never coerces a missing field to 0 — a confidence
 *  of 0 and an unknown confidence are different facts. */
export function eventNumber(event: AgentStreamEvent, key: string): number | null {
  const value = event[key];
  if (typeof value === 'number' && Number.isFinite(value)) return value;
  if (typeof value === 'string' && value.trim() !== '') {
    const parsed = Number(value);
    return Number.isFinite(parsed) ? parsed : null;
  }
  return null;
}

/** A string field, or null. */
export function eventString(event: AgentStreamEvent, key: string): string | null {
  const value = event[key];
  return typeof value === 'string' && value.trim() !== '' ? value : null;
}

/** Test/debug helper: how many streams are active (0 or 1, never more).
 *
 *  Kept under its original name so existing tests and callers do not change.
 *  It counts the shared POLLING loop now rather than a socket — the invariant
 *  it guards is the same one it always guarded: five consumers must share one
 *  connection to the backend, not open five. */
export function _activeSocketCount(): number {
  return running ? 1 : 0;
}

export function _listenerCount(): number {
  return listeners.size;
}

/** Test helper: reset module state between cases. */
export function _resetStream(): void {
  listeners = new Set();
  buffer = [];
  connected = false;
  running = false;
  inFlight = false;
  failedAttempts = 0;
  cursor = null;
  clearPollTimer();
}
