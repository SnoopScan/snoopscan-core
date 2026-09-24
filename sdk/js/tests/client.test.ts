import { describe, expect, it } from 'vitest';
import { SnoopScan, SnoopScanError, Document, isCrawlFinished } from '../src/index.js';

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'content-type': 'application/json' },
  });
}

describe('SnoopScan.unwrap', () => {
  it('returns the data field on success', async () => {
    const result = await SnoopScan.unwrap(jsonResponse({ success: true, data: { markdown: 'hi' } }));
    expect(result).toEqual({ markdown: 'hi' });
  });

  it('raises SnoopScanError carrying the API code, not a generic HTTP error', async () => {
    const response = jsonResponse(
      { success: false, error: { code: 'BLOCKED', message: 'target refused us', detail: { tier: 'browser' } } },
      403,
    );
    await expect(SnoopScan.unwrap(response)).rejects.toMatchObject({
      code: 'BLOCKED',
      detail: { tier: 'browser' },
    });
  });

  it('exposes isBlocked / isTargetError / isRateLimited as branchable flags', () => {
    const blocked = new SnoopScanError('BLOCKED', 'x');
    const target = new SnoopScanError('TARGET_ERROR', 'x');
    expect(blocked.isBlocked).toBe(true);
    expect(blocked.isTargetError).toBe(false);
    expect(target.isTargetError).toBe(true);
    expect(target.isBlocked).toBe(false);
  });

  it('reports a non-JSON body as INTERNAL rather than throwing a raw parse error', async () => {
    const response = new Response('<html>gateway error</html>', {
      status: 502,
      headers: { 'content-type': 'text/html' },
    });
    await expect(SnoopScan.unwrap(response)).rejects.toMatchObject({ code: 'INTERNAL' });
  });
});

describe('Document', () => {
  it('exposes metadata as properties instead of making every caller dig into a dict', () => {
    const doc = new Document({
      markdown: '# hi',
      metadata: { title: 'Hi', pageType: 'article', wordCount: 120, extractionConfidence: 0.9 },
      cost: { tier: 'http', tiers_attempted: ['http'] },
    });
    expect(doc.title).toBe('Hi');
    expect(doc.pageType).toBe('article');
    expect(doc.wordCount).toBe(120);
    expect(doc.isSuspect).toBe(false);
    expect(doc.cost.tier).toBe('http');
    expect(doc.cost.tiersAttempted).toEqual(['http']);
  });

  it('flags low-confidence extractions as suspect, at the same 0.5 threshold as the Python client', () => {
    const doc = new Document({ metadata: { extractionConfidence: 0.3 } });
    expect(doc.isSuspect).toBe(true);
  });

  it('keeps raw exactly as it arrived, so a field the typed view drops is not lost', () => {
    const doc = new Document({ markdown: 'x', somethingNew: 'not in the typed view' });
    expect(doc.raw.somethingNew).toBe('not in the typed view');
  });
});

describe('constructor', () => {
  it('strips a trailing slash from baseUrl, the same normalisation the Python client does', async () => {
    let capturedUrl = '';
    const fakeFetch = (async (url: string | URL) => {
      capturedUrl = String(url);
      return jsonResponse({ success: true, data: {} });
    }) as typeof fetch;

    const snoop = new SnoopScan({ apiKey: 'k', baseUrl: 'http://localhost:1/', fetch: fakeFetch });
    await snoop.scrape('https://example.com');
    expect(capturedUrl).toBe('http://localhost:1/v1/scrape');
  });

  it('sends the Authorization bearer header on every request', async () => {
    let capturedAuth = '';
    const fakeFetch = (async (_url: string | URL, init?: RequestInit) => {
      capturedAuth = (init?.headers as Record<string, string>).Authorization ?? '';
      return jsonResponse({ success: true, data: { links: [] } });
    }) as typeof fetch;

    const snoop = new SnoopScan({ apiKey: 'sk_test_123', baseUrl: 'http://localhost:1', fetch: fakeFetch });
    await snoop.map('https://example.com');
    expect(capturedAuth).toBe('Bearer sk_test_123');
  });
});

// --------------------------------------------------------------------------
// Real integration tests. Skipped unless SNOOPSCAN_TEST_API_KEY is set —
// a scoped, rate-limited key minted for CI, never a real production key
// committed anywhere. This is the same rigor bar the Python SDK's tests
// hold (tested against the real app, not a mock), the honest version of it
// for a client that talks over the network rather than in-process: the
// engine's route-level behaviour is already covered by its own test suite,
// so this proves the SDK's own request/response wiring end to end, for
// real, against production — not that domain_intel or firmographics parse
// correctly, which is not this package's job to re-test.
// --------------------------------------------------------------------------

const liveKey = process.env.SNOOPSCAN_TEST_API_KEY;
const liveBaseUrl = process.env.SNOOPSCAN_TEST_BASE_URL ?? 'https://api.snoopscan.com';

describe.skipIf(!liveKey)('live integration (SNOOPSCAN_TEST_API_KEY set)', () => {
  const snoop = new SnoopScan({ apiKey: liveKey ?? '', baseUrl: liveBaseUrl });

  it('scrapes a real page over the real API', async () => {
    const page = await snoop.scrape('https://example.com', { tier: 'http' });
    expect(page.markdown).toContain('Example Domain');
    expect(page.cost.tier).toBe('http');
  });

  it('maps a real page', async () => {
    const links = await snoop.map('https://example.com');
    expect(Array.isArray(links)).toBe(true);
  });

  it('an invalid request surfaces as INVALID_REQUEST, not a generic failure', async () => {
    await expect(snoop.scrape('not-a-url')).rejects.toMatchObject({ code: 'INVALID_REQUEST' });
  });
});

describe('DEFAULT_BASE_URL', () => {
  it('is not localhost — a fresh install must work against the hosted API out of the box', async () => {
    const { DEFAULT_BASE_URL } = await import('../src/client.js');
    expect(DEFAULT_BASE_URL).not.toContain('localhost');
    expect(DEFAULT_BASE_URL).not.toContain('127.0.0.1');
  });
});

describe('per-request timeout widening', () => {
  it('widens the abort deadline when a request body asks the engine for longer than the client default', async () => {
    // Real bug: a caller passing `timeout: 120000` to scrape() got the
    // client's own fixed default (also 120000 by coincidence) as the ONLY
    // deadline — the two raced, and whichever fired first won. Simulated
    // here with a tiny client default (50ms) and a request that takes
    // 80ms: it must NOT be aborted, because the engine timeout (5000ms)
    // pushes the real deadline well past 80ms.
    const fakeFetch = (async () => {
      await new Promise((resolve) => setTimeout(resolve, 80));
      return jsonResponse({ success: true, data: { markdown: 'slow but real' } });
    }) as typeof fetch;

    const snoop = new SnoopScan({ apiKey: 'k', timeout: 50, fetch: fakeFetch });
    const doc = await snoop.scrape('https://example.com', { timeout: 5000 });
    expect(doc.markdown).toBe('slow but real');
  });

  it('still aborts at the client default when no engine timeout is given', async () => {
    // A real fetch rejects with an AbortError the moment its signal fires;
    // this fake has to do the same or the abort has nothing to reject.
    const fakeFetch = ((_url: string | URL, init?: RequestInit) =>
      new Promise<Response>((resolve, reject) => {
        const done = setTimeout(() => resolve(jsonResponse({ success: true, data: {} })), 80);
        init?.signal?.addEventListener('abort', () => {
          clearTimeout(done);
          reject(Object.assign(new Error('aborted'), { name: 'AbortError' }));
        });
      })) as typeof fetch;

    const snoop = new SnoopScan({ apiKey: 'k', timeout: 20, fetch: fakeFetch });
    await expect(snoop.scrape('https://example.com')).rejects.toMatchObject({ code: 'TIMEOUT' });
  });
});

describe('CrawlJob.finished', () => {
  // Python's CrawlJob has had `.finished` all along. Here the check lived in an
  // unexported helper, so a caller polling a crawl had to hard-code the three
  // terminal statuses — and forgetting `cancelled` loops for ever.
  const job = (status: string) =>
    new SnoopScan({ apiKey: 'sk_test', fetch: (async () =>
      new Response(JSON.stringify({ success: true, data: { id: 'c1', status } }), {
        status: 200,
        headers: { 'content-type': 'application/json' },
      })) as unknown as typeof fetch }).crawlStatus('c1');

  it('is true for every terminal status, cancelled included', async () => {
    for (const s of ['completed', 'failed', 'cancelled']) {
      expect((await job(s)).finished).toBe(true);
    }
  });

  it('is false while the crawl is still going', async () => {
    for (const s of ['queued', 'running']) {
      expect((await job(s)).finished).toBe(false);
    }
  });

  it('is exported, so a caller holding a plain status can ask too', () => {
    expect(isCrawlFinished({ status: 'cancelled' })).toBe(true);
    expect(isCrawlFinished({ status: 'running' })).toBe(false);
  });
});

describe('parse', () => {
  it('uploads a local document as a multipart file, byte for byte', async () => {
    const seen: { url: string; init: RequestInit }[] = [];
    const client = new SnoopScan({
      apiKey: 'sk_test',
      baseUrl: 'https://api.example.test',
      fetch: (async (url: string, init: RequestInit) => {
        seen.push({ url, init });
        return jsonResponse({ success: true, data: { markdown: '# Report', pages: 1, kind: 'pdf' } });
      }) as unknown as typeof fetch,
    });
    const pdf = new Uint8Array([0x25, 0x50, 0x44, 0x46, 0xff, 0xfe, 0x00, 0x0a]);
    const out = await client.parse({ content: pdf, filename: 'report.pdf' });

    expect(out.markdown).toBe('# Report');
    expect(seen[0]!.url).toBe('https://api.example.test/v1/parse');
    const headers = seen[0]!.init.headers as Record<string, string>;
    expect(headers['Content-Type']).toBeUndefined();
    const body = seen[0]!.init.body as FormData;
    const file = body.get('file') as File;
    expect(file.name).toBe('report.pdf');
    expect(new Uint8Array(await file.arrayBuffer())).toEqual(pdf);
  });

  it('fetches a URL through scrape and answers in parse shape', async () => {
    const seen: { url: string; body: Record<string, unknown> }[] = [];
    const client = new SnoopScan({
      apiKey: 'sk_test',
      baseUrl: 'https://api.example.test',
      fetch: (async (url: string, init: RequestInit) => {
        seen.push({ url, body: JSON.parse(init.body as string) });
        return jsonResponse({ success: true, data: { markdown: '# Annual', metadata: { sourceURL: 'https://ex.test/r.pdf' } } });
      }) as unknown as typeof fetch,
    });
    const out = await client.parse({ url: 'https://ex.test/r.pdf' });
    expect(seen[0]!.url).toBe('https://api.example.test/v1/scrape');
    expect(seen[0]!.body.parsers).toEqual(['pdf']);
    expect(out.markdown).toBe('# Annual');
    expect(out.url).toBe('https://ex.test/r.pdf');
  });

  it('refuses ambiguous input before sending anything', async () => {
    const client = new SnoopScan({ apiKey: 'sk_test', fetch: (async () => { throw new Error('sent'); }) as unknown as typeof fetch });
    await expect(client.parse({})).rejects.toMatchObject({ code: 'INVALID_REQUEST' });
    await expect(client.parse({ url: 'https://ex.test/memo.docx' })).rejects.toMatchObject({ code: 'INVALID_REQUEST' });
  });
});
