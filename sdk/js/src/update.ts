/**
 * A quiet "a newer version is available" line, as every comparable CLI has —
 * the JS twin of the Python CLI's `_update.py`. Asks the npm registry at most
 * once a day, shows the line at most twice a day, writes to stderr so it never
 * mixes with a command's output, and can never make a command fail.
 * SNOOPSCAN_NO_UPDATE_CHECK=1 turns it off.
 */
import { mkdirSync, readFileSync, writeFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { configPath } from './config.js';

export const REGISTRY_URL = 'https://registry.npmjs.org/snoopscan/latest';
const CHECK_EVERY_MS = 20 * 3600 * 1000;
const SHOW_EVERY_MS = 12 * 3600 * 1000;
export const DISABLE_ENV = 'SNOOPSCAN_NO_UPDATE_CHECK';

interface Cache {
  latest?: string;
  checkedAt?: number;
  shownVersion?: string;
  shownAt?: number;
}

/** This package's own version, read from the package.json beside the build. */
export function currentVersion(): string {
  try {
    return String(JSON.parse(readFileSync(new URL('../package.json', import.meta.url), 'utf8')).version ?? '');
  } catch {
    return '';
  }
}

function parse(v: string): number[] {
  return v.split('.').map((p) => Number(p.replace(/\D/g, '')) || 0);
}

export function isNewer(latest: string, current: string): boolean {
  const a = parse(latest);
  const b = parse(current);
  for (let i = 0; i < Math.max(a.length, b.length); i++) {
    if ((a[i] ?? 0) !== (b[i] ?? 0)) return (a[i] ?? 0) > (b[i] ?? 0);
  }
  return false;
}

export function disabled(): boolean {
  return ['1', 'true', 'yes'].includes(String(process.env[DISABLE_ENV] ?? '').toLowerCase());
}

const cachePath = (): string => join(dirname(configPath()), 'update-check.json');

function read(): Cache {
  try {
    return JSON.parse(readFileSync(cachePath(), 'utf8')) as Cache;
  } catch {
    return {};
  }
}

function write(cache: Cache): void {
  try {
    mkdirSync(dirname(cachePath()), { recursive: true });
    writeFileSync(cachePath(), JSON.stringify(cache));
  } catch {
    // a read-only home must not break a command
  }
}

export async function latestVersion(
  opts: { now?: number; force?: boolean; fetchImpl?: typeof fetch } = {},
): Promise<string | undefined> {
  const now = opts.now ?? Date.now();
  const cache = read();
  if (!opts.force && typeof cache.checkedAt === 'number' && now - cache.checkedAt < CHECK_EVERY_MS) {
    return cache.latest;
  }
  try {
    const res = await (opts.fetchImpl ?? fetch)(REGISTRY_URL, { signal: AbortSignal.timeout(2000) });
    const latest = String(((await res.json()) as { version?: string }).version ?? '');
    if (!latest) return undefined;
    write({ ...cache, latest, checkedAt: now });
    return latest;
  } catch {
    return undefined; // offline, blocked, slow: no notice, no error
  }
}

/** The line to print, or undefined. Shown again only after a while, or for a newer release. */
export async function notice(
  current: string,
  opts: { now?: number; fetchImpl?: typeof fetch } = {},
): Promise<string | undefined> {
  if (disabled() || !current) return undefined;
  const now = opts.now ?? Date.now();
  const latest = await latestVersion({ now, fetchImpl: opts.fetchImpl });
  if (!latest || !isNewer(latest, current)) return undefined;
  const cache = read();
  if (cache.shownVersion === latest && typeof cache.shownAt === 'number' && now - cache.shownAt < SHOW_EVERY_MS) {
    return undefined;
  }
  write({ ...cache, shownVersion: latest, shownAt: now });
  return (
    `A newer snoopscan is available: ${current} -> ${latest}\n` +
    '  Update: npm install -g snoopscan@latest   (npx snoopscan@latest always runs the latest)\n' +
    `  Silence this: ${DISABLE_ENV}=1\n`
  );
}
