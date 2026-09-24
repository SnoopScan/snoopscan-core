/**
 * JavaScript/TypeScript client for the SnoopScan web scraping API.
 *
 * Deliberately mirrors the shape of the established clients in this space —
 * `scrape`, `crawl`, `map`, `extract`, `search`, plus a blocking
 * `crawlAndWait` — because the migration promise is that existing code
 * changes a base URL and keeps working. Method and option names are the
 * API's names; only the method NAME casing follows JS convention
 * (`crawlStatus`, not `crawl_status` — the Python client does the reverse
 * for the same reason, following ITS language's convention).
 *
 * Written from our own OpenAPI surface, not from any other client's source
 * (constraint C1).
 *
 *   import { SnoopScan } from 'snoopscan';
 *
 *   const snoop = new SnoopScan({ apiKey: 'sk_...' });
 *   const page = await snoop.scrape('https://example.com');
 *   console.log(page.markdown);
 */

import { SnoopScanError } from './errors.js';
import {
  Cost,
  CrawlJob,
  Document,
  Options,
  crawlJobFromPayload,
  isCrawlFinished,
} from './types.js';

// The hosted API, api.snoopscan.com — see sdk/python/snoopscan/client.py's
// DEFAULT_BASE_URL for the full reasoning. This defaulted to
// `http://localhost:8099` until 15 Sep 2026, which failed every real
// customer's first call before it got anywhere; a self-hoster of this
// open-core engine sets SNOOPSCAN_BASE_URL=http://localhost:8099 instead —
// one env var, for exactly the audience capable of setting it.
// `process.env` is Node/Bun/Deno-only, so this is guarded for the browser
// build, where a base URL must be passed explicitly.
function envBaseUrl(): string | undefined {
  if (typeof process === 'undefined' || !process.env) return undefined;
  return process.env.SNOOPSCAN_BASE_URL || process.env.SNOOP_BASE_URL;
}

export const DEFAULT_BASE_URL = envBaseUrl() || 'https://api.snoopscan.com';
const DEFAULT_TIMEOUT_MS = 120_000;

// `timeout` (ms) in a request body is the ENGINE's own budget — how long it
// may keep trying tiers server-side. It has nothing to do with
// DEFAULT_TIMEOUT_MS, this client's own abort deadline for waiting on THAT
// response — two clocks that happened to agree by coincidence whenever a
// caller asked for exactly 120s. Ask for 120s (or more) and the two raced:
// whichever fired first won, sometimes surfacing this client's own abort
// instead of the engine's clean JSON error. Matches the identical bug fixed
// in the Python SDK the same day. 10s of margin covers connection setup and
// response transfer that sit outside the engine's own accounting.
const TIMEOUT_MARGIN_MS = 10_000;

function httpTimeoutFor(body: Record<string, unknown> | undefined, clientDefault: number): number {
  const engineTimeout = body?.timeout;
  if (typeof engineTimeout !== 'number') return clientDefault;
  return Math.max(clientDefault, engineTimeout + TIMEOUT_MARGIN_MS);
}

export interface SnoopScanOptions {
  apiKey: string;
  baseUrl?: string;
  /** Milliseconds. */
  timeout?: number;
  /** Bring your own fetch — Node <18, a proxying fetch, a test double. */
  fetch?: typeof fetch;
}

export class SnoopScan {
  private baseUrl: string;
  private apiKey: string;
  private timeoutMs: number;
  private fetchImpl: typeof fetch;

  constructor(options: SnoopScanOptions) {
    this.apiKey = options.apiKey;
    this.baseUrl = (options.baseUrl ?? DEFAULT_BASE_URL).replace(/\/+$/, '');
    this.timeoutMs = options.timeout ?? DEFAULT_TIMEOUT_MS;
    this.fetchImpl = options.fetch ?? globalThis.fetch;
    if (!this.fetchImpl) {
      throw new Error(
        'No fetch implementation available. Pass one via `fetch` in the constructor options ' +
          '(e.g. on Node < 18, or a custom test double).',
      );
    }
  }

  // -- plumbing -----------------------------------------------------------

  private async request(
    method: 'GET' | 'POST' | 'DELETE',
    path: string,
    body?: Record<string, unknown>,
    params?: Record<string, string | number>,
  ): Promise<unknown> {
    let url = `${this.baseUrl}${path}`;
    if (params) {
      const qs = new URLSearchParams(
        Object.entries(params).map(([k, v]) => [k, String(v)]),
      ).toString();
      if (qs) url += `?${qs}`;
    }

    const requestTimeoutMs = httpTimeoutFor(body, this.timeoutMs);
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), requestTimeoutMs);
    let response: Response;
    try {
      response = await this.fetchImpl(url, {
        method,
        headers: {
          Authorization: `Bearer ${this.apiKey}`,
          'Content-Type': 'application/json',
        },
        body: body !== undefined ? JSON.stringify(body) : undefined,
        signal: controller.signal,
      });
    } catch (cause) {
      if (cause instanceof Error && cause.name === 'AbortError') {
        throw new SnoopScanError('TIMEOUT', `Request to ${path} timed out after ${requestTimeoutMs}ms`);
      }
      throw new SnoopScanError('NETWORK_ERROR', `Could not reach ${this.baseUrl}: ${(cause as Error).message}`);
    } finally {
      clearTimeout(timer);
    }

    return SnoopScan.unwrap(response);
  }

  /**
   * Never leak a raw fetch/HTTP failure for an API-level failure: callers
   * branch on `SnoopScanError.code`, and an uncaught non-2xx would bypass
   * that entirely. A non-JSON body means something in front of the API
   * answered — a proxy, a load balancer, an nginx error page — so it is
   * reported as INTERNAL with the status.
   */
  static async unwrap(response: Response): Promise<unknown> {
    let payload: { success?: boolean; data?: unknown; error?: Record<string, unknown> };
    try {
      payload = await response.json();
    } catch {
      throw new SnoopScanError(
        'INTERNAL',
        `Non-JSON response from the API (HTTP ${response.status})`,
        { status_code: response.status },
      );
    }

    if (!payload.success) {
      const error = payload.error ?? {};
      throw new SnoopScanError(
        (error.code as string) ?? 'INTERNAL',
        (error.message as string) ?? 'Unknown error',
        (error.detail as Record<string, unknown>) ?? null,
      );
    }
    return payload.data;
  }

  private post(path: string, body: Record<string, unknown>): Promise<any> {
    return this.request('POST', path, body) as Promise<any>;
  }

  private get(path: string, params?: Record<string, string | number>): Promise<any> {
    return this.request('GET', path, undefined, params) as Promise<any>;
  }

  private del(path: string): Promise<any> {
    return this.request('DELETE', path) as Promise<any>;
  }

  // -- pages ----------------------------------------------------------------

  /** Fetch and extract one URL. */
  async scrape(url: string, options: Options = {}): Promise<Document> {
    const data = await this.post('/v1/scrape', { url, ...options });
    return new Document(data);
  }

  /** Start a crawl. Returns immediately with a job id. */
  async crawl(url: string, options: Options = {}): Promise<CrawlJob> {
    const data = await this.post('/v1/crawl', { url, ...options });
    return crawlJobFromPayload(data);
  }

  async crawlStatus(jobId: string): Promise<CrawlJob> {
    const data = await this.get(`/v1/crawl/${jobId}`);
    return crawlJobFromPayload(data);
  }

  /**
   * One page of results, plus the cursor for the next. Results are
   * paginated rather than inlined because a 10,000-page crawl must not
   * arrive as a single JSON body.
   */
  async crawlPages(
    jobId: string,
    cursor?: string,
    limit = 50,
  ): Promise<{ documents: Document[]; nextCursor: string | null }> {
    const params: Record<string, string | number> = { limit };
    if (cursor) params.cursor = cursor;
    const data = await this.get(`/v1/crawl/${jobId}/pages`, params);
    const documents = ((data.pages as Record<string, unknown>[]) ?? []).map((row) => new Document(row));
    const nextLink = data.next as string | undefined;
    const nextCursor = nextLink ? nextLink.split('cursor=').pop() ?? null : null;
    return { documents, nextCursor };
  }

  async crawlErrors(jobId: string): Promise<Record<string, unknown>[]> {
    const data = await this.get(`/v1/crawl/${jobId}/errors`);
    return data.errors ?? [];
  }

  async cancelCrawl(jobId: string): Promise<CrawlJob> {
    const data = await this.del(`/v1/crawl/${jobId}`);
    return crawlJobFromPayload(data);
  }

  /**
   * Start a crawl and wait until it finishes, then return every page.
   *
   * Convenience only. For anything large, start the crawl and read pages as
   * they arrive rather than holding the whole result in memory.
   */
  async crawlAndWait(
    url: string,
    options: Options & { pollIntervalMs?: number; maxWaitMs?: number } = {},
  ): Promise<Document[]> {
    const { pollIntervalMs = 3_000, maxWaitMs = 900_000, ...crawlOptions } = options;
    const job = await this.crawl(url, crawlOptions);
    const deadline = Date.now() + maxWaitMs;

    let current = job;
    while (Date.now() < deadline) {
      current = await this.crawlStatus(job.id);
      if (isCrawlFinished(current)) break;
      await new Promise((resolve) => setTimeout(resolve, pollIntervalMs));
    }
    if (!isCrawlFinished(current)) {
      throw new SnoopScanError('TIMEOUT', `Crawl ${job.id} did not finish within ${maxWaitMs}ms`);
    }

    const documents: Document[] = [];
    let cursor: string | null | undefined;
    for (;;) {
      const page = await this.crawlPages(job.id, cursor ?? undefined);
      documents.push(...page.documents);
      cursor = page.nextCursor;
      if (!cursor) return documents;
    }
  }

  /** Discover URLs without fetching page bodies. Fast and cheap. */
  async map(url: string, options: Options = {}): Promise<string[]> {
    const data = await this.post('/v1/map', { url, ...options });
    return data.links ?? [];
  }

  /** Every product a Shopify or WooCommerce store publishes, from its own
   * catalogue endpoint. Returns {platform, total, products, pagesFetched, cost}. */
  async products(url: string, options: Options = {}): Promise<Record<string, unknown>> {
    return this.post('/v1/products', { url, ...options });
  }

  /** A site's posts from its API (WordPress, Substack, Squarespace,
   * Discourse) or its RSS/Atom feed. Returns {platform, source, posts, cost}. */
  async posts(url: string, options: Options = {}): Promise<Record<string, unknown>> {
    return this.post('/v1/posts', { url, ...options });
  }

  /** Everything a company's own site says about itself: firmographics
   * (name, phone, address, LinkedIn, headcount, industry) plus, unless
   * `contacts: false`, the emails, social links and contact form it
   * publishes. Returns {domain, company, people, contacts, pagesRead, cost}. */
  async company(url: string, options: Options = {}): Promise<Record<string, unknown>> {
    return this.post('/v1/company', { url, ...options });
  }

  /** What is known about a domain rather than a page: registration, DNS
   * records, and who links to it in our own crawl graph. Every part is
   * opt-out — pass `registration: false`, `dns: false` or `backlinks:
   * false` to skip one. Returns {domain, registration, dns, backlinks, cost}. */
  async domain(domainName: string, options: Options = {}): Promise<Record<string, unknown>> {
    return this.post('/v1/domain', { domain: domainName, ...options });
  }

  // -- monitors: watch pages for changes on a schedule ---------------------

  /** intervalMinutes (>= 5, default 60), goal, webhook. Returns the monitor. */
  async createMonitor(
    name: string,
    urls: string[] | string,
    options: Options = {},
  ): Promise<Record<string, unknown>> {
    const body: Record<string, unknown> = { name, ...options };
    body[Array.isArray(urls) ? 'urls' : 'url'] = urls;
    return this.post('/v1/monitor', body);
  }

  async monitors(): Promise<Record<string, unknown>[]> {
    const data = await this.get('/v1/monitor');
    return data.monitors ?? [];
  }

  async monitor(monitorId: string): Promise<Record<string, unknown>> {
    return this.get(`/v1/monitor/${monitorId}`);
  }

  async deleteMonitor(monitorId: string): Promise<void> {
    await this.del(`/v1/monitor/${monitorId}`);
  }

  /** A check right now; returns it. */
  async runMonitor(monitorId: string): Promise<Record<string, unknown>> {
    return this.post(`/v1/monitor/${monitorId}/run`, {});
  }

  async monitorChecks(monitorId: string, limit = 20): Promise<Record<string, unknown>[]> {
    const data = await this.get(`/v1/monitor/${monitorId}/checks`, { limit });
    return data.checks ?? [];
  }

  // -- bulk / structured ----------------------------------------------------

  async batchScrape(urls: string[], options: Options = {}): Promise<CrawlJob> {
    const data = await this.post('/v1/batch/scrape', { urls, ...options });
    return crawlJobFromPayload(data);
  }

  /**
   * Schema-constrained extraction. Output is validated against the schema
   * before it is returned, so a row with an `error` means the page
   * genuinely lacked the fields — not that the request should be retried.
   */
  async extract(
    urls: string[],
    schema: Record<string, unknown> | null,
    options: Options & { prompt?: string; template?: string } = {},
  ): Promise<Record<string, unknown>[]> {
    const { prompt, template, ...rest } = options;
    if ((schema == null) === (template == null)) {
      throw new Error(
        'give either `schema` or `template`, not both and not neither: a template ' +
          'IS a schema, and silently picking one would return fields you did not ask for',
      );
    }
    const body: Record<string, unknown> = { urls, ...rest };
    if (schema != null) body.schema = schema;
    if (template != null) body.template = template;
    if (prompt) body.prompt = prompt;
    return this.post('/v1/extract', body);
  }

  /** The named templates this deployment offers, with their fields. */
  async templates(): Promise<Record<string, unknown>[]> {
    const answer = await this.get('/v1/templates');
    return answer.templates ?? [];
  }

  async search(query: string, options: Options = {}): Promise<Record<string, unknown>> {
    return this.post('/v1/search', { query, ...options });
  }

  /**
   * Turn a document into markdown: PDF, DOCX, HTML, TXT or Markdown.
   *
   * Exactly one of:
   *   - `content`: the document itself (a string, bytes or a Blob), sent as a
   *     multipart upload; `filename` says what kind it is.
   *   - `url`: a document on the web. The engine fetches it with every tier
   *     it has and reads PDFs, HTML and text, answering in the same shape.
   *
   * `/v1/parse` takes one uploaded file. This used to post JSON to it, so
   * every call failed, URL or file alike (found 23 Sep 2026).
   */
  async parse(input: {
    url?: string;
    content?: string | Uint8Array | ArrayBuffer | Blob;
    filename?: string;
  }): Promise<Record<string, unknown>> {
    const given = [input.url, input.content].filter((v) => v !== undefined && v !== '');
    if (given.length !== 1) {
      throw new SnoopScanError('INVALID_REQUEST', 'parse takes exactly one of url or content');
    }
    if (input.url !== undefined) {
      if ((input.url.toLowerCase().split('?')[0] ?? '').endsWith('.docx')) {
        throw new SnoopScanError(
          'INVALID_REQUEST',
          'A Word document is read from a file, not a URL: download it and pass its content.',
        );
      }
      const data = (await this.post('/v1/scrape', {
        url: input.url,
        formats: ['markdown'],
        parsers: ['pdf'],
      })) as Record<string, unknown>;
      const meta = (data.metadata as Record<string, unknown>) ?? {};
      return {
        markdown: (data.markdown as string) ?? '',
        url: (meta.sourceURL as string) ?? (meta.url as string) ?? input.url,
        kind: 'url',
        metadata: meta,
        cost: data.cost,
      };
    }
    const content = input.content as string | Uint8Array | ArrayBuffer | Blob;
    const name = input.filename ?? (typeof content === 'string' ? 'document.txt' : 'document');
    const blob =
      content instanceof Blob
        ? content
        : new Blob([typeof content === 'string' ? content : (content as BlobPart)]);
    const form = new FormData();
    form.append('file', blob, name);
    return (await this.upload('/v1/parse', form)) as Record<string, unknown>;
  }

  /** A multipart POST. No Content-Type of our own: fetch writes the boundary. */
  private async upload(path: string, form: FormData): Promise<unknown> {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), this.timeoutMs);
    let response: Response;
    try {
      response = await this.fetchImpl(`${this.baseUrl}${path}`, {
        method: 'POST',
        headers: { Authorization: `Bearer ${this.apiKey}` },
        body: form,
        signal: controller.signal,
      });
    } catch (cause) {
      if (cause instanceof Error && cause.name === 'AbortError') {
        throw new SnoopScanError('TIMEOUT', `Request to ${path} timed out after ${this.timeoutMs}ms`);
      }
      throw new SnoopScanError('NETWORK_ERROR', `Could not reach ${this.baseUrl}: ${(cause as Error).message}`);
    } finally {
      clearTimeout(timer);
    }
    return SnoopScan.unwrap(response);
  }
}

export type { Cost, CrawlJob, Options };
export { Document, SnoopScanError };
