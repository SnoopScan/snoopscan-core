import { describe, expect, it } from 'vitest';
import { cmdSearch } from '../src/cli.js';
import type { SnoopScan } from '../src/client.js';

/**
 * `search --scrape` sent `scrapeResults: true`, a field the API's strict
 * SearchRequest model has never accepted — it takes `scrapeOptions`, an
 * object. Every search --scrape call was rejected with "Extra inputs are
 * not permitted"; the Python CLI had the identical bug, fixed separately.
 */
function stubClient(): { calls: Array<{ query: string; body: Record<string, unknown> }>; client: SnoopScan } {
  const calls: Array<{ query: string; body: Record<string, unknown> }> = [];
  const client = {
    search: async (query: string, body: Record<string, unknown> = {}) => {
      calls.push({ query, body });
      return { results: [], provider: 'test' };
    },
  } as unknown as SnoopScan;
  return { calls, client };
}

describe('cmdSearch', () => {
  it('sends scrapeOptions, not scrapeResults, when --scrape is passed', async () => {
    const { calls, client } = stubClient();
    await cmdSearch(client, ['widgets', '--scrape', '--json']);
    expect(calls).toHaveLength(1);
    const [call] = calls;
    expect(call?.body).toHaveProperty('scrapeOptions');
    expect(call?.body).not.toHaveProperty('scrapeResults');
  });

  it('sends no scrapeOptions without --scrape', async () => {
    const { calls, client } = stubClient();
    await cmdSearch(client, ['widgets', '--json']);
    const [call] = calls;
    expect(call?.body).not.toHaveProperty('scrapeOptions');
  });
});
