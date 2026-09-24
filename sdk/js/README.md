# snoopscan

JavaScript/TypeScript client for SnoopScan, the web scraping API for AI agents.

MIT licensed. The server is AGPL-3.0; a client library must not be, or every
application that imports it inherits the copyleft.

```bash
npm install snoopscan
npx snoopscan login    # opens your browser: sign in or sign up (free, no card), approve, key saved
npx snoopscan doctor   # checks the install, the key and the API, and says how to fix anything
```

Needs Node 18+.

```ts
import { SnoopScan } from 'snoopscan';

// `npx snoopscan config get api_key` prints the key login saved; put it in your environment.
const snoop = new SnoopScan({ apiKey: process.env.SNOOPSCAN_API_KEY! });

const page = await snoop.scrape('https://example.com/');
console.log(page.markdown);

for (const link of await snoop.map('https://example.com/', { limit: 100 })) {
  console.log(link);
}

const pages = await snoop.crawlAndWait('https://example.com/', { limit: 50 });
console.log(pages.length, 'pages');
```

Works in Node 18+ and in the browser — the client is built on native `fetch`
with zero runtime dependencies. `require()` and `import` both work; types
ship in the package, no `@types/snoopscan` to install separately.

## Templates

Structured fields without writing a schema. A template names a kind of page —
`product`, `article`, `jobPosting`, `localBusiness`, `event`, `recipe` — and its
fields are the schema.org names sites already publish, so they usually come
straight from the page's own markup, with no model call.

```ts
const rows = await snoop.extract(['https://shop.example/product/123'], null, {
  template: 'product',
});
console.log(rows[0].data); // { name, price, currency, ... }

for (const t of await snoop.templates()) {
  console.log(t.name, t.fields); // what this deployment offers
}
```

Give a `schema` **or** a `template`, never both — a template *is* a schema.
Passing both, or neither, throws before the request is sent.

## Polling a crawl yourself

`crawlAndWait` does this for you. When you want control over the wait:

```ts
let job = await snoop.crawl('https://example.com/', { limit: 500 });
while (!(job = await snoop.crawlStatus(job.id)).finished) {  // completed, failed or cancelled
  await new Promise((r) => setTimeout(r, 5000));
}
```

`isCrawlFinished(job)` is exported too, for a job you only hold the status of.

## Platforms

A site that publishes its data as JSON is asked, not crawled:

```ts
const catalogue = await snoop.products('https://store.example.com'); // Shopify, WooCommerce, Squarespace, Magento
for (const p of catalogue.products as Record<string, unknown>[]) {
  console.log(p.title, p.price, p.currency, p.available);
}

const posts = await snoop.posts('https://blog.example.com'); // WordPress, Substack, Squarespace, Discourse; else the feed
console.log(posts.source, (posts.posts as unknown[]).length);
```

Every page's `metadata.platform` says what built it, and a `scrape()` of a
Shopify, WooCommerce or Amazon product page carries `product` beside the
markdown. Amazon shows the honest client no price — pass `tier: 'browser'`.

## Company & domain

Two lookups that answer questions a single page can't:

```ts
const lead = await snoop.company('acme.com'); // firmographics + contacts, from the site itself
const company = lead.company as Record<string, unknown>;
console.log(company.name, company.headcount);

const info = await snoop.domain('acme.com'); // registration, DNS, backlinks — not a page fetch
console.log((info.registration as Record<string, unknown>).registrar);
```

`company()` takes `{ contacts: false }` to skip contact discovery and return
only firmographics. `domain()`'s three lookups are each opt-out —
`registration: false`, `dns: false`, `backlinks: false` — since a caller
asking about a domain usually wants all of it, not a form to fill in.

## Monitors

```ts
const m = await snoop.createMonitor('Pricing', ['https://example.com/pricing'], {
  intervalMinutes: 60,
  webhook: 'https://hooks.example.com/snoop',
});
const check = await snoop.runMonitor(m.id as string); // a check now: {counts, pages}
await snoop.monitorChecks(m.id as string); // recent checks
await snoop.deleteMonitor(m.id as string);
```

Each page in a check is `same`, `changed` (with a git diff), `new` or
`error`. The webhook `monitor.check.completed` fires only when a check has
something to say.

## Base URL

Defaults to `http://localhost:8099`, the engine's own dev port (Node only —
a browser build has no environment variable to read, so pass `baseUrl`
explicitly). Point it elsewhere with `SNOOPSCAN_BASE_URL`, or per client:

```ts
const snoop = new SnoopScan({ apiKey: process.env.SNOOPSCAN_API_KEY!, baseUrl: 'https://api.example.com' });
```

## Errors

Every failure throws `SnoopScanError` carrying the API's machine-readable
code, so callers can branch on what actually happened:

```ts
import { SnoopScanError } from 'snoopscan';

try {
  const page = await snoop.scrape(url);
} catch (err) {
  if (err instanceof SnoopScanError) {
    if (err.isBlocked) {
      // BLOCKED — the target refused us; retrying as-is will not help
    } else if (err.code === 'FETCH_FAILED') {
      // the target could not be reached; retrying may help
    } else if (err.code === 'INVALID_REQUEST') {
      // our request was wrong; fix it, do not retry
    }
  }
}
```

## Cost

Every response carries what it cost to produce — the tier that answered,
every tier attempted, proxy bytes, browser milliseconds, and whether it came
from cache. A cache hit reports the accounting of the fetch that filled it,
so `cost.tier` is never null on a page that was really fetched once.

## CLI

Installing the package also installs a `snoopscan` command — the same verbs
as the API and the Python CLI, so a shell example pastes into either:

```bash
snoopscan login                    # browser sign-in; saves the key
snoopscan scrape https://example.com
snoopscan crawl https://example.com --wait --limit 50
snoopscan company acme.com
```

Run `snoopscan --help` for the full command list, or `--help` after any
subcommand isn't wired up yet — check `-o`, `--json`, `--pretty` and `-q`,
which work on every command. One caveat if you also use the Python SDK's
CLI: both ship a binary literally named `snoopscan`, so installing both
globally means whichever comes later on `PATH` wins; they share the same
config file (`~/.config/snoopscan/config.toml`) so at least there's nothing
to reconfigure either way.

## Development

From a checkout of the engine repo:

```bash
cd sdk/js
npm install
npm run build
npm test              # unit tests; set SNOOPSCAN_TEST_API_KEY to also run
                       # the live integration tests against a real key
```
