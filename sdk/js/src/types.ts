/** What a request actually cost. Failed requests carry no cost. */
export interface Cost {
  tier: string | null;
  tiersAttempted: string[];
  proxyUsed: boolean;
  proxyType: string | null;
  proxyBytes: number;
  browserMs: number;
  extractionPath: string | null;
  cached: boolean;
}

function costFromPayload(raw: Record<string, unknown> | undefined | null): Cost {
  const c = raw ?? {};
  return {
    tier: (c.tier as string) ?? null,
    tiersAttempted: (c.tiers_attempted as string[]) ?? [],
    proxyUsed: Boolean(c.proxy_used),
    proxyType: (c.proxy_type as string) ?? null,
    proxyBytes: Number(c.proxy_bytes ?? 0),
    browserMs: Number(c.browser_ms ?? 0),
    extractionPath: (c.extraction_path as string) ?? null,
    cached: Boolean(c.cached),
  };
}

/**
 * A fetched, extracted page. The typed fields are a curated view — `raw`
 * keeps the payload exactly as it arrived, so anything the API adds later,
 * or anything a caller wants to dump verbatim, is never lost to parsing.
 */
export class Document {
  markdown: string | null;
  html: string | null;
  rawHtml: string | null;
  links: string[];
  json: Record<string, unknown> | null;
  metadata: Record<string, unknown>;
  cost: Cost;
  raw: Record<string, unknown>;

  constructor(data: Record<string, unknown>) {
    this.markdown = (data.markdown as string) ?? null;
    this.html = (data.html as string) ?? null;
    this.rawHtml = (data.rawHtml as string) ?? null;
    this.links = (data.links as string[]) ?? [];
    this.json = (data.json as Record<string, unknown>) ?? null;
    this.metadata = (data.metadata as Record<string, unknown>) ?? {};
    this.cost = costFromPayload(data.cost as Record<string, unknown>);
    this.raw = data;
  }

  get title(): string | null {
    return (this.metadata.title as string) ?? null;
  }

  get url(): string | null {
    return (this.metadata.url as string) ?? null;
  }

  get pageType(): string {
    return (this.metadata.pageType as string) ?? 'unknown';
  }

  get wordCount(): number {
    return Number(this.metadata.wordCount ?? 0);
  }

  /** 0-1. Treat anything below 0.5 as suspect and cross-check it. */
  get extractionConfidence(): number {
    return Number(this.metadata.extractionConfidence ?? 0);
  }

  get isSuspect(): boolean {
    return this.extractionConfidence < 0.5;
  }
}

export interface CrawlJob {
  id: string;
  status: string;
  total: number;
  completed: number;
  failed: number;
  cost: Record<string, unknown>;
  /** True once the job has stopped — completed, failed or cancelled. The
   * Python SDK's `CrawlJob.finished`; here it was only reachable through an
   * unexported helper, so a caller polling a crawl had to hard-code the
   * three terminal statuses themselves. */
  finished: boolean;
}

export function isCrawlFinished(job: Pick<CrawlJob, 'status'>): boolean {
  return job.status === 'completed' || job.status === 'failed' || job.status === 'cancelled';
}

export function crawlJobFromPayload(data: Record<string, unknown>): CrawlJob {
  const status = data.status as string;
  return {
    id: data.id as string,
    status,
    total: Number(data.total ?? 0),
    completed: Number(data.completed ?? 0),
    failed: Number(data.failed ?? 0),
    cost: (data.cost as Record<string, unknown>) ?? {},
    finished: isCrawlFinished({ status }),
  };
}

/** Option bags. Keys are the API's own names (camelCase), same promise the
 * Python SDK makes: an existing request body works unchanged. */
export type Options = Record<string, unknown>;
