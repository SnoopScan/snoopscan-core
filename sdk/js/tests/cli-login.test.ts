import { mkdtempSync, readFileSync, statSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cmdLogin, NO_KEY, type LoginDeps } from '../src/cli.js';

/**
 * `snoopscan login`: browser sign-in that saves a real key, so nobody copies
 * one. Before this the only answer to "no key" was `config set api_key <key>`
 * and an agent handed the placeholder straight to the person. Mirrors the
 * Python CLI's test of the same command.
 */
const REAL_KEY = 'sk_live_from_the_browser';

function site(states: string[]): { deps: LoginDeps; opened: string[] } {
  const queue = [...states];
  const opened: string[] = [];
  const json = (data: unknown) => new Response(JSON.stringify({ success: true, data }), { status: 200 });
  const deps: LoginDeps = {
    fetch: (async (url: string | URL, init?: RequestInit) => {
      if (String(url).endsWith('/api/cli/login/start')) {
        return json({ deviceCode: 'd'.repeat(48), userCode: 'ABCD-EFGH', verifyUrl: 'https://site.test/app/cli/ABCD-EFGH', interval: 1, expiresIn: 60 });
      }
      expect(JSON.parse(String(init?.body)).deviceCode).toBe('d'.repeat(48));
      const status = queue.shift();
      return json(status === 'approved' ? { status, apiKey: REAL_KEY } : { status });
    }) as typeof fetch,
    open: (url) => (opened.push(url), true),
    sleep: async () => {},
  };
  return { deps, opened };
}

describe('snoopscan login', () => {
  let out = '';
  beforeEach(() => {
    process.env.XDG_CONFIG_HOME = mkdtempSync(join(tmpdir(), 'snoop-'));
    delete process.env.SNOOPSCAN_API_KEY;
    out = '';
    vi.spyOn(process.stdout, 'write').mockImplementation((s) => ((out += String(s)), true));
    vi.spyOn(process.stderr, 'write').mockImplementation((s) => ((out += String(s)), true));
  });
  afterEach(() => vi.restoreAllMocks());

  it('opens the browser, waits for approval and saves the key without printing it', async () => {
    const { deps, opened } = site(['pending', 'pending', 'approved']);
    expect(await cmdLogin(['--account-url', 'https://site.test'], deps)).toBe(0);
    expect(out).toContain('ABCD-EFGH');
    expect(out).not.toContain(REAL_KEY);
    expect(opened).toEqual(['https://site.test/app/cli/ABCD-EFGH']);
    const file = join(process.env.XDG_CONFIG_HOME!, 'snoopscan', 'config.toml');
    expect(readFileSync(file, 'utf8')).toContain(REAL_KEY);
    expect(statSync(file).mode & 0o777).toBe(0o600);
  });

  it('saves nothing when the person cancels, and does not open a browser with --no-browser', async () => {
    const { deps, opened } = site(['pending', 'denied']);
    expect(await cmdLogin(['--account-url', 'https://site.test', '--no-browser'], deps)).toBe(1);
    expect(opened).toEqual([]);
    expect(out).toContain('Nothing was saved');
  });

  it('points a keyless caller at login, never at a placeholder', () => {
    expect(NO_KEY).toContain('snoopscan login');
    expect(NO_KEY).not.toContain('sk_...');
  });
});
