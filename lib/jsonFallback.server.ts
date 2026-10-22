// ---------------------------------------------------------------------
// The JSON-file half of every migrated store.
//
// Each `lib/*Store.server.ts` now reads and writes Postgres when `DATABASE_URL`
// is configured, and falls back to its `.data/*.json` file when it is not. This
// module is the fallback, extracted once instead of thirteen times — the atomic
// write below was already duplicated across every store, and a rename-based write
// that only *most* stores implement is the kind of inconsistency that shows up as
// one corrupted file.
//
// WHY KEEP A FALLBACK AT ALL, RATHER THAN REQUIRING POSTGRES
//
// A fresh clone with no database must still run. Making `DATABASE_URL` mandatory
// would turn "I just want to look at the UI" into a database-provisioning task,
// and the app already degrades honestly everywhere else. The fallback is
// SECONDARY and says so: `source` travels with every read so a page can state
// which store answered, because two stores holding different answers under one
// heading is the exact failure this migration exists to end.
//
// The JSON path is NOT durable on serverless (Vercel and friends give each
// invocation an ephemeral filesystem). That is not a new caveat — it was written
// on `tradeStore.server.ts` from the start. Postgres is the answer to it.
// ---------------------------------------------------------------------

import { promises as fs } from 'fs';
import path from 'path';

const DATA_DIR = path.join(process.cwd(), '.data');

/** Which store answered a read. Surfaced to callers, never hidden. */
export type StoreSource = 'postgres' | 'json';

export type Sourced<T> = {
  data: T;
  source: StoreSource;
  /** Set when Postgres was configured but the read failed and JSON answered. */
  degradedReason?: string;
};

/** Serialise writes per file. Not concurrency control — a same-process guard. */
const queues = new Map<string, Promise<unknown>>();

export function serialize<T>(key: string, fn: () => Promise<T>): Promise<T> {
  const previous = queues.get(key) ?? Promise.resolve();
  const result = previous.then(fn, fn);
  queues.set(
    key,
    result.catch(() => {}),
  );
  return result;
}

// F38: A READ-ONLY FILESYSTEM MADE EVEN A **GET** RETURN 500.
//
// `ensureFile` creates the file on first READ, so on Vercel — where the
// filesystem is read-only — `fs.mkdir`/`fs.writeFile` throws EROFS and the
// exception propagates out of the route handler. Live, `GET /api/collaboration`
// and `GET /api/volatility-history` both answered 500 for this reason, even
// though neither caller was writing anything.
//
// The module docstring above already said the JSON path "is NOT durable on
// serverless". It being non-durable is fine; it THROWING is not.
//
// Degraded to an in-memory store for the life of the process, and the reason
// is readable through `storageNote()` rather than swallowed — a caller that
// gets an empty list deserves to know whether that means "nothing stored" or
// "nothing can be stored here".
const memory = new Map<string, unknown>();

// PER FILE, NOT PROCESS-WIDE — and that distinction was a real bug.
//
// This used to be a single `readOnlyReason` string. One failed write on ANY
// store latched it for the life of the process, and `readJson` consulted it
// BEFORE touching the disk, so from that moment every JSON-backed store
// returned the in-memory copy — which is empty for any file that had only
// ever been read. Every store read EMPTY while the files on disk were
// perfectly fine. On the trade log that reads as "you have no trades".
//
// A write failing on one path says nothing about another, so the reason is
// recorded against the file it happened to.
const unwritable = new Map<string, string>();

/**
 * Why JSON-backed storage is not durable in this process, or null.
 *
 * Names the affected stores rather than implying all of them: "nothing can be
 * stored" and "this one store could not be stored" send a reader to different
 * places.
 */
export function storageNote(): string | null {
  if (unwritable.size === 0) return null;
  const names = [...unwritable.keys()].sort().join(', ');
  return (
    `writes failed for ${names} (${[...unwritable.values()][0]}), so those stores ` +
    `are held in memory for this process only and are NOT durable. Configure ` +
    `DATABASE_URL for durable storage.`
  );
}

function markUnwritable(name: string, e: unknown): void {
  if (unwritable.has(name)) return;
  const reason = e instanceof Error ? e.message : String(e);
  unwritable.set(name, reason);
  console.warn(`[jsonFallback] ${name} is not writable: ${reason}`);
}

async function ensureFile(name: string, file: string, empty: string): Promise<void> {
  if (unwritable.has(name)) return;
  try {
    await fs.mkdir(DATA_DIR, { recursive: true });
    try {
      await fs.access(file);
    } catch {
      await fs.writeFile(file, empty, 'utf8');
    }
  } catch (e) {
    markUnwritable(name, e);
  }
}

export function dataFile(name: string): string {
  return path.join(DATA_DIR, name);
}

/** Read and parse a JSON store, returning `fallback` on a missing or bad file. */
export async function readJson<T>(name: string, fallback: T): Promise<T> {
  const file = dataFile(name);
  await ensureFile(name, file, Array.isArray(fallback) ? '[]' : '{}');

  // THE DISK IS TRIED FIRST, ALWAYS.
  //
  // This used to return the in-memory copy whenever ANY write had ever failed,
  // without reading the file — so one unrelated failure blanked every store
  // while the files were perfectly readable. A file that exists and parses is
  // the truth; what happened to some write is irrelevant to reading it.
  try {
    const raw = await fs.readFile(file, 'utf8');
    return JSON.parse(raw) as T;
  } catch {
    // Only now does the in-memory copy matter: a value THIS process wrote and
    // could not persist is more current than a file that is missing or
    // corrupt. Beyond that the fallback, because the caller's job is to serve
    // a page and a parse error must not 500 the route.
    if (memory.has(name)) return memory.get(name) as T;
    return fallback;
  }
}

/**
 * Write a JSON store ATOMICALLY.
 *
 * Temp file plus rename, never a direct write. A direct write truncates before
 * the new content lands, so a concurrent read can see a half-written file, fail
 * to parse, and silently get an empty list back — which on a trade log reads as
 * "you have no trades". `rename` is atomic: a reader sees the complete old file
 * or the complete new one.
 */
export async function writeJson<T>(name: string, value: T): Promise<void> {
  if (unwritable.has(name)) {
    memory.set(name, value);
    return;
  }
  const file = dataFile(name);
  try {
    await fs.mkdir(DATA_DIR, { recursive: true });
    const tmp = `${file}.${process.pid}.${Date.now()}.tmp`;
    await fs.writeFile(tmp, JSON.stringify(value, null, 2), 'utf8');
    await fs.rename(tmp, file);
  } catch (e) {
    // The write is kept in memory so the rest of THIS request behaves
    // consistently, and the reason is recorded so a reader is not told an
    // empty store means nothing happened.
    markUnwritable(name, e);
    memory.set(name, value);
  }
}

/**
 * Milliseconds since epoch from whatever Postgres or a JSON file returned.
 *
 * `timestamptz` comes back from `pg` as a `Date`; the JSON stores hold a number.
 * Returning `NaN` for an unparseable value would put a candle in the year 56000
 * or sort a table randomly, so an unusable timestamp becomes `null` and the
 * caller decides.
 */
export function toMillis(value: unknown): number | null {
  if (value instanceof Date) {
    const ms = value.getTime();
    return Number.isFinite(ms) ? ms : null;
  }
  if (typeof value === 'number') return Number.isFinite(value) ? value : null;
  if (typeof value === 'string') {
    const parsed = Date.parse(value);
    return Number.isFinite(parsed) ? parsed : null;
  }
  return null;
}

/**
 * A number from Postgres `numeric`, which `pg` returns as a STRING.
 *
 * `numeric` is arbitrary-precision, so node-postgres hands it back as text
 * rather than lose digits to a float. Reading it without this returns `"75.4"`
 * where a number was expected — string concatenation instead of arithmetic, and
 * `"75.4" * 2` silently working is what makes it hard to spot.
 *
 * Returns `null`, never 0, when there is no value: a missing price is not a
 * price of zero.
 */
export function toNumber(value: unknown): number | null {
  if (value === null || value === undefined) return null;
  if (typeof value === 'number') return Number.isFinite(value) ? value : null;
  if (typeof value === 'string') {
    const trimmed = value.trim();
    if (!trimmed) return null;
    const parsed = Number(trimmed);
    return Number.isFinite(parsed) ? parsed : null;
  }
  return null;
}
