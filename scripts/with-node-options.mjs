#!/usr/bin/env node
/**
 * Run a command with NODE_OPTIONS set, on any platform.
 *
 * WHY THIS EXISTS (F17). `package.json` used `set NODE_OPTIONS=... && next
 * build`, which is cmd.exe syntax. On macOS and Linux `set` is a shell builtin
 * that PRINTS variables and ignores its argument, so the command ran with no
 * heap flag at all and the failure was silent — the build either succeeded
 * anyway or died in `Collecting page data` with a fast-fail exit code that
 * reads as a stack overflow.
 *
 * `next.config.js` caps the build worker count for exactly this reason, and
 * that cap is documented as load-bearing: sixteen workers each loading the
 * whole app did not fit in memory. A heap flag that silently does nothing on
 * the deploy platform is the other half of the same problem.
 *
 * A wrapper rather than `cross-env` so this needs no dependency and no
 * install step — the repo must build from a fresh clone.
 */

import { spawn } from 'node:child_process';

const [, , options, ...command] = process.argv;

if (!options || command.length === 0) {
  console.error(
    'usage: node scripts/with-node-options.mjs "<node-options>" <command> [args...]',
  );
  process.exit(2);
}

// APPENDED, not replaced: an operator or CI may already have set something
// here, and clobbering it would be a second silent surprise.
const existing = process.env.NODE_OPTIONS ? `${process.env.NODE_OPTIONS} ` : '';

const child = spawn(command[0], command.slice(1), {
  stdio: 'inherit',
  // `shell: true` so `next` resolves through node_modules/.bin on every
  // platform without hardcoding a path.
  shell: true,
  env: { ...process.env, NODE_OPTIONS: `${existing}${options}` },
});

child.on('exit', (code, signal) => {
  // A signal is not an exit code; reporting 0 for a killed build would make a
  // failure look like a success in CI.
  if (signal) {
    console.error(`\nCommand terminated by signal ${signal}`);
    process.exit(1);
  }
  process.exit(code ?? 1);
});

child.on('error', (err) => {
  console.error(`Failed to start "${command.join(' ')}": ${err.message}`);
  process.exit(1);
});
