import { promises as fs } from 'fs';
import path from 'path';
import type { CollaborationOpinion } from './collaborationAgent';

// Same persistence pattern as every other *.server.ts store in this
// app: a JSON file on local disk (./.data/collaboration.json). Append-
// only, same rationale as lib/decisionStore.server.ts — a record of
// every second-opinion request/response actually made, not something
// later edits should be allowed to rewrite.

const DATA_DIR = path.join(process.cwd(), '.data');
const DATA_FILE = path.join(DATA_DIR, 'collaboration.json');

export type CollaborationRecord = {
  id: string;
  ts: number;
  symbol: string;
  side: 'buy' | 'sell';
  ownConfidencePct: number;
  triggerReason: string; // why the second opinion was requested
  provider: string; // second-opinion Provider.id, for the audit record
  model: string;
  opinion: CollaborationOpinion | null; // null = request failed or response didn't parse
  error: string | null;
};


// F38: VERCEL'S FILESYSTEM IS READ-ONLY, AND EVEN A GET 500'd.
//
// `ensureFile` creates the file on first READ, so `fs.mkdir`/`fs.writeFile`
// throws EROFS on a serverless deployment and the exception propagates out of
// the route handler as an HTTP 500. Live, `GET /api/collaboration` and
// `GET /api/volatility-history` both returned 500 for this reason.
//
// Degraded to an IN-MEMORY store for the life of the process, with the reason
// surfaced rather than swallowed: a reader gets an empty list and is told the
// records are not durable here, which is the truth. Silently pretending to
// persist would be worse than the 500 — the operator would believe a record
// existed.
let readOnlyReason: string | null = null;
let memory: any[] | null = null;

export function storageNote(): string | null {
  return readOnlyReason;
}

async function ensureFile(): Promise<void> {
  if (readOnlyReason !== null) return;
  try {
    await fs.mkdir(DATA_DIR, { recursive: true });
    try {
      await fs.access(DATA_FILE);
    } catch {
      await fs.writeFile(DATA_FILE, '[]', 'utf8');
    }
  } catch (e) {
    readOnlyReason =
      `this deployment's filesystem is not writable (${
        e instanceof Error ? e.message : String(e)
      }), so collaboration records are held in memory for this process only ` +
      `and are NOT durable`;
    memory = memory ?? [];
    console.warn(`[collaboration] ${readOnlyReason}`);
  }
}

let queue: Promise<unknown> = Promise.resolve();
function serialize<T>(fn: () => Promise<T>): Promise<T> {
  const result = queue.then(fn, fn);
  queue = result.catch(() => {});
  return result;
}

async function readAll(): Promise<CollaborationRecord[]> {
  await ensureFile();

  // THE DISK IS TRIED FIRST, EVEN AFTER A WRITE HAS FAILED.
  //
  // This used to short-circuit to `memory` the moment any write failed, so one
  // failure made every subsequent read return the in-memory copy — empty for
  // anything this process had not itself written — while the file on disk was
  // perfectly readable. `lib/jsonFallback.server.ts` carried the identical bug
  // and is fixed the same way; the two are separate stores with one lesson.
  try {
    const raw = await fs.readFile(DATA_FILE, 'utf8');
    return JSON.parse(raw) as CollaborationRecord[];
  } catch {
    // Only now does the in-memory copy matter: a value this process wrote and
    // could not persist beats a file that is missing or malformed. Beyond that
    // an empty list, never a 500 — the route that reads this is a page load.
    if (memory !== null) return memory as CollaborationRecord[];
    return [];
  }
}

async function writeAll(records: CollaborationRecord[]): Promise<void> {
  await ensureFile();
  if (readOnlyReason !== null) {
    memory = records.slice();
    return;
  }
  try {
    await fs.writeFile(DATA_FILE, JSON.stringify(records, null, 2), 'utf8');
  } catch (e) {
    readOnlyReason =
      `could not write collaboration records (${
        e instanceof Error ? e.message : String(e)
      }); they are held in memory for this process only`;
    memory = records.slice();
    console.warn(`[collaboration] ${readOnlyReason}`);
  }
}

export async function listCollaborationRecords(): Promise<CollaborationRecord[]> {
  return readAll();
}

export async function appendCollaborationRecord(record: CollaborationRecord): Promise<CollaborationRecord> {
  return serialize(async () => {
    const all = await readAll();
    all.push(record);
    await writeAll(all);
    return record;
  });
}
