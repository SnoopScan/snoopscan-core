import { mkdtempSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cmdDoctor } from '../src/cli.js';
import * as userConfig from '../src/config.js';
import { isNewer, notice } from '../src/update.js';

/** Mirrors the Python CLI's tests: keep people current, and one command that
 * checks a whole install and says how to fix it. */
const registry = (version: string | null) =>
  (async () => {
    if (version === null) throw new Error('offline');
    return new Response(JSON.stringify({ version }), { status: 200 });
  }) as unknown as typeof fetch;

describe('update notice', () => {
  beforeEach(() => {
    process.env.XDG_CONFIG_HOME = mkdtempSync(join(tmpdir(), 'snoop-'));
    delete process.env.SNOOPSCAN_NO_UPDATE_CHECK;
  });

  it('compares versions as numbers', () => {
    expect(isNewer('0.10.0', '0.9.9')).toBe(true);
    expect(isNewer('0.4.1', '0.4.1')).toBe(false);
  });

  it('announces a newer release once, then stays quiet for a while', async () => {
    const first = await notice('0.5.0', { now: 1_000_000, fetchImpl: registry('9.0.0') });
    expect(first).toContain('0.5.0 -> 9.0.0');
    expect(await notice('0.5.0', { now: 1_060_000, fetchImpl: registry('9.0.0') })).toBeUndefined();
    expect(await notice('0.5.0', { now: 1_000_000 + 13 * 3600_000, fetchImpl: registry('9.0.0') })).toBeDefined();
  });

  it('never shows when current, offline or switched off', async () => {
    expect(await notice('9.0.0', { now: 1, fetchImpl: registry('9.0.0') })).toBeUndefined();
    expect(await notice('0.1.0', { now: 99_000_000, fetchImpl: registry(null) })).toBeUndefined();
    process.env.SNOOPSCAN_NO_UPDATE_CHECK = '1';
    expect(await notice('0.1.0', { now: 199_000_000, fetchImpl: registry('9.0.0') })).toBeUndefined();
  });
});

describe('snoopscan doctor', () => {
  let out = '';
  beforeEach(() => {
    process.env.XDG_CONFIG_HOME = mkdtempSync(join(tmpdir(), 'snoop-'));
    delete process.env.SNOOPSCAN_API_KEY;
    out = '';
    vi.spyOn(process.stdout, 'write').mockImplementation((s) => ((out += String(s)), true));
  });
  afterEach(() => vi.restoreAllMocks());

  const api = (keyStatus: number) =>
    (async (url: string | URL) =>
      String(url).endsWith('/v1/templates')
        ? new Response('{}', { status: keyStatus })
        : new Response(JSON.stringify({ status: 'ok' }), { status: 200 })) as typeof fetch;

  it('passes a working install without printing the key', async () => {
    userConfig.save({ api_key: 'sk_live_abcdefghijkl' });
    expect(await cmdDoctor([], { fetch: api(200), latest: async () => undefined })).toBe(0);
    expect(out).toContain('accepted');
    expect(out).toContain('All good.');
    expect(out).not.toContain('sk_live_abcdefghijkl');
  });

  it('says how to fix a missing or refused key', async () => {
    expect(await cmdDoctor([], { fetch: api(200), latest: async () => undefined })).toBe(1);
    expect(out).toContain('not set. Fix: snoopscan login');
    userConfig.save({ api_key: 'sk_live_revoked_key_xx' });
    out = '';
    expect(await cmdDoctor([], { fetch: api(401), latest: async () => undefined })).toBe(1);
    expect(out).toContain('not recognised');
  });
});
