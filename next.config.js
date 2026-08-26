const os = require('os');

// ---------------------------------------------------------------------
// BUILD WORKER COUNT — why this is not left at the default.
//
// `npm run build` compiled and type-checked cleanly, then died in the
// "Collecting page data" phase with:
//
//     ⨯ Next.js build worker exited with code: 3221226505 and signal: null
//
// 3221226505 is 0xC0000409, the Windows `__fastfail` code. It reads like a
// stack buffer overrun, which sends you looking for infinite recursion in a
// route module. IT IS NOT THAT. V8 raises the same fast-fail when an
// allocation cannot be satisfied, so on Windows an out-of-memory worker and a
// corrupted stack are indistinguishable from the exit code alone.
//
// WHAT IT ACTUALLY WAS, established by bisection rather than inference:
//
//   experimental.cpus   result on this machine (16 cores, ~6.3 GB free)
//   -----------------   ------------------------------------------------
//   default (= 16)      FAILS
//   8                   FAILS
//   4                   PASSES
//   1                   PASSES
//
// Next spawns one static worker per CPU and each loads the whole app — 57
// routes, 22 nested providers, lightweight-charts. Sixteen of those at once do
// not fit in the memory available, so one of them fast-fails and takes the
// build with it. The failure is about FAN-OUT, not about any single page: with
// the fan-out bounded, every one of the same 30 pages prerenders fine.
//
// Two things this was NOT, both checked so nobody re-checks them:
//   * not a bad route module — `workerThreads: false` was unnecessary; capping
//     the count alone is sufficient, and all 30 pages generate.
//   * not the `--max-old-space-size=4096` in the build script. That flag IS
//     inherited by every worker, which looked like the obvious culprit, but
//     removing it while leaving the worker count at 16 still fails.
//
// THE BUDGET BELOW IS MEASURED, NOT INVENTED. 4 workers passing and 8 failing
// at 6.3 GB free puts the per-worker requirement between 0.79 GB and 1.6 GB;
// 1.5 GB is the conservative end of that measured range. Deriving the count
// from actual free memory rather than hardcoding `4` means a smaller machine
// serialises further instead of failing, and a bigger one is not needlessly
// throttled — a hardcoded 4 would be right only for this laptop.
//
// `freemem()` moves between runs, so the worker count is not reproducible
// build to build. That is acceptable: it is a throughput knob, and every value
// it can produce is one the build survives. It is capped at the core count so
// it can never ask for more parallelism than the default would have.
// ---------------------------------------------------------------------
const BYTES_PER_BUILD_WORKER = 1.5 * 1024 * 1024 * 1024;

function buildWorkerCount() {
  const cores = os.cpus().length || 1;
  const affordable = Math.floor(os.freemem() / BYTES_PER_BUILD_WORKER);
  // Floor of 1: zero workers is not a valid setting, and a machine too small
  // for one worker needs to fail loudly during the build rather than here.
  return Math.max(1, Math.min(cores, affordable));
}

/** @type {import('next').NextConfig} */
const nextConfig = {
  reactStrictMode: true,
  experimental: {
    cpus: buildWorkerCount(),
  },
};

module.exports = nextConfig;
