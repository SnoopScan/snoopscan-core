#!/usr/bin/env node
/**
 * The `snoopscan` command line.
 *
 * One vocabulary across the REST API, the SDK and this: `scrape` here is
 * `/v1/scrape` there is `snoop.scrape()` in JS. Learning one surface teaches
 * the other two, and a docs example pastes into any of them.
 *
 * Mirrors the Python CLI's commands and behaviour exactly — same flags, same
 * receipts on stderr, same config file — so which language's CLI is
 * installed is an implementation detail, not a second thing to learn.
 *
 * No dependency on a CLI framework: `node:util`'s `parseArgs` is the
 * standard-library equivalent of Python's argparse, and a wrapper that drags
 * a dependency tree into every project that installs the SDK is a tax on
 * people who only wanted the client.
 */

import { parseArgs, type ParseArgsOptionsConfig } from 'node:util';
import { readFileSync, realpathSync, writeFileSync } from 'node:fs';
import { spawn } from 'node:child_process';
import { hostname } from 'node:os';
import { pathToFileURL } from 'node:url';
import { SnoopScan, SnoopScanError, DEFAULT_BASE_URL, type Options } from './client.js';
import * as userConfig from './config.js';
import * as update from './update.js';
import { basename } from 'node:path';

const ENV_KEY = 'SNOOPSCAN_API_KEY';
const ENV_URL = 'SNOOPSCAN_BASE_URL';
// Where accounts live. The API is on its own host; signing in happens on the site.
const ENV_ACCOUNT = 'SNOOPSCAN_ACCOUNT_URL';
const DEFAULT_ACCOUNT_URL = 'https://snoopscan.com';

export const NO_KEY =
  'No API key yet. Get a free one (no card needed):\n' +
  '  snoopscan login          opens your browser to sign in or sign up, and saves the key\n' +
  'Or paste one from https://snoopscan.com/app/keys:\n' +
  '  snoopscan config set api_key <key>      (or set $SNOOPSCAN_API_KEY)\n';

// 1 is "we failed", 2 is "the target refused us" — different problems, and a
// caller retrying the second one forever is exactly the waste this separates.
const EXIT_OK = 0;
const EXIT_ERROR = 1;
const EXIT_TARGET_ERROR = 2;

/** `positionals[0]` is `string | undefined` under noUncheckedIndexedAccess
 * because a caller genuinely can leave off the argument — `snoopscan scrape`
 * with no URL is a real thing someone will type. Fail with a usage line
 * instead of letting `undefined` reach fetch()/readFileSync() as a string. */
function requirePositional(value: string | undefined, usage: string): string {
  if (value === undefined) {
    process.stderr.write(`${usage}\n`);
    process.exit(EXIT_ERROR);
  }
  return value;
}

interface GlobalFlags {
  apiKey?: string;
  baseUrl?: string;
  output?: string;
  json?: boolean;
  pretty?: boolean;
  quiet?: boolean;
}

// Global flags work on either side of the subcommand — every command's own
// option spec spreads this in, the same way argparse's `parents=[common]`
// puts them on each subparser rather than only the top-level one.
const GLOBAL_OPTION_SPEC = {
  'api-key': { type: 'string' as const },
  'base-url': { type: 'string' as const },
  output: { type: 'string' as const, short: 'o' },
  json: { type: 'boolean' as const },
  pretty: { type: 'boolean' as const },
  quiet: { type: 'boolean' as const, short: 'q' },
} satisfies ParseArgsOptionsConfig;

function globalFlagsFrom(values: Record<string, unknown>): GlobalFlags {
  return {
    apiKey: values['api-key'] as string | undefined,
    baseUrl: values['base-url'] as string | undefined,
    output: values.output as string | undefined,
    json: values.json as boolean | undefined,
    pretty: values.pretty as boolean | undefined,
    quiet: values.quiet as boolean | undefined,
  };
}

function note(message: string, flags: GlobalFlags): void {
  if (!flags.quiet) process.stderr.write(message + '\n');
}

function emit(value: unknown, flags: GlobalFlags): void {
  const text = typeof value === 'string' ? value : JSON.stringify(value, null, flags.pretty ? 2 : undefined);
  if (flags.output) {
    writeFileSync(flags.output, text, 'utf-8');
    note(`written to ${flags.output} (${text.length.toLocaleString()} chars)`, flags);
    return;
  }
  process.stdout.write(text + '\n');
}

/** How the page was got, and what it cost. On stderr deliberately:
 * `snoopscan scrape url > page.md` must produce a clean file. */
function receipt(payload: Record<string, unknown>, flags: GlobalFlags): void {
  if (flags.quiet || typeof payload !== 'object' || payload === null) return;
  const cost = (payload.cost as Record<string, unknown>) ?? {};
  const meta = (payload.metadata as Record<string, unknown>) ?? {};
  const bits: string[] = [];
  const tier = cost.tier ?? meta.tier;
  if (tier) bits.push(`tier=${tier}`);
  if (cost.credits !== undefined && cost.credits !== null) bits.push(`credits=${cost.credits}`);
  const ms = cost.durationMs ?? cost.duration_ms;
  if (ms !== undefined && ms !== null) bits.push(`${(Number(ms) / 1000).toFixed(1)}s`);
  if (bits.length) process.stderr.write('  ' + bits.join('  ') + '\n');
}

/** A readable line per item. The default output of a catalogue command must
 * not be the catalogue — full fidelity is one flag away (`--json`). */
function listing(rows: Record<string, unknown>[], titleKeys: string[]): string {
  return rows
    .map((row) => {
      const title = titleKeys.map((k) => row[k]).find((v) => v) ?? '(untitled)';
      const url = row.url ?? row.link ?? '';
      const price = row.price;
      return `${title}${price ? `  ${price}` : ''}\n  ${url}`;
    })
    .join('\n');
}

function resolveClient(flags: GlobalFlags): SnoopScan {
  const stored = userConfig.load();
  const apiKey = flags.apiKey || process.env[ENV_KEY] || stored.api_key;
  const baseUrl = flags.baseUrl || process.env[ENV_URL] || stored.base_url || DEFAULT_BASE_URL;
  if (!apiKey) {
    process.stderr.write(NO_KEY);
    process.exit(EXIT_ERROR);
  }
  return new SnoopScan({ apiKey, baseUrl });
}

function pageOptions(values: Record<string, unknown>): Options {
  const out: Options = {};
  if (values.tier) out.tier = values.tier;
  if (values.timeout) out.timeout = Number(values.timeout);
  if (values['max-age'] !== undefined) out.maxAge = Number(values['max-age']);
  if (values.formats) out.formats = String(values.formats).split(',');
  return out;
}

const PAGE_OPTION_SPEC = {
  tier: { type: 'string' as const },
  timeout: { type: 'string' as const },
  'max-age': { type: 'string' as const },
  formats: { type: 'string' as const },
} satisfies ParseArgsOptionsConfig;

// --------------------------------------------------------------------------
// Commands
//
// Each takes the client and the RAW args after the subcommand (global flags
// included, unparsed) and does its own single parseArgs call against its own
// option spec + GLOBAL_OPTION_SPEC — mirroring argparse's parents=[common].
// --------------------------------------------------------------------------

type Command = (client: SnoopScan, args: string[]) => Promise<number>;

async function cmdScrape(client: SnoopScan, args: string[]): Promise<number> {
  const { values, positionals } = parseArgs({
    args,
    options: { ...GLOBAL_OPTION_SPEC, ...PAGE_OPTION_SPEC },
    allowPositionals: true,
  });
  const flags = globalFlagsFrom(values);
  const url = requirePositional(positionals[0], 'Usage: snoopscan scrape <url>');
  const doc = await client.scrape(url, pageOptions(values));
  receipt(doc.raw, flags);
  if (doc.isSuspect && !flags.quiet) {
    note(`  low confidence (${doc.extractionConfidence.toFixed(2)}) — check it`, flags);
  }
  if (flags.json) {
    emit(doc.raw, flags);
    return EXIT_OK;
  }
  const formats = (pageOptions(values).formats as string[] | undefined) ?? ['markdown'];
  // The typed fields first, then anything else the payload carries.
  const source: Record<string, unknown> = {
    ...doc.raw,
    markdown: doc.markdown ?? doc.raw.markdown,
    html: doc.html ?? doc.raw.html,
    rawHtml: doc.rawHtml ?? doc.raw.rawHtml,
    links: doc.links.length ? doc.links : doc.raw.links,
  };
  if (formats.every((name) => !source[name])) {
    // Loud even under --quiet: an empty answer must never pass for "none".
    process.stderr.write(`  nothing came back for ${formats.join(', ')}. Try --json for the whole response.\n`);
    return EXIT_ERROR;
  }
  emit(readable(source, formats), flags);
  return EXIT_OK;
}

/** One format, readable: links one per line, text as-is, the rest as JSON. */
function asText(value: unknown): string {
  if (value === null || value === undefined) return '';
  if (typeof value === 'string') return value;
  if (Array.isArray(value)) {
    return value
      .map((item) => (item && typeof item === 'object' ? String((item as Record<string, unknown>).url ?? (item as Record<string, unknown>).href ?? JSON.stringify(item)) : String(item)))
      .join('\n');
  }
  return JSON.stringify(value, null, 2);
}

/** What was asked for, not always the markdown. `--formats links,rawHtml`
 * used to print the (unrequested, so empty) markdown and exit 0 — which read
 * as "this page has no links". */
export function readable(raw: Record<string, unknown>, formats: string[]): string {
  const parts = formats.map((name) => [name, asText(raw[name])] as const);
  if (parts.length === 1) return parts[0]?.[1] ?? '';
  return parts.map(([name, text]) => `--- ${name} ---\n${text}`).join('\n\n');
}

async function cmdMap(client: SnoopScan, args: string[]): Promise<number> {
  const { values, positionals } = parseArgs({
    args,
    options: { ...GLOBAL_OPTION_SPEC, ...PAGE_OPTION_SPEC },
    allowPositionals: true,
  });
  const flags = globalFlagsFrom(values);
  const url = requirePositional(positionals[0], 'Usage: snoopscan map <url>');
  const links = await client.map(url, pageOptions(values));
  note(`  ${links.length} links`, flags);
  emit(flags.json ? links : links.map((l) => String(l)).join('\n'), flags);
  return EXIT_OK;
}

export async function cmdSearch(client: SnoopScan, args: string[]): Promise<number> {
  const { values, positionals } = parseArgs({
    args,
    options: { ...GLOBAL_OPTION_SPEC, limit: { type: 'string' }, scrape: { type: 'boolean' } },
    allowPositionals: true,
  });
  const flags = globalFlagsFrom(values);
  const body: Options = { limit: values.limit ? Number(values.limit) : 10 };
  // The API takes ScrapeOptions here, not a bare flag — an empty object
  // opts in with its own defaults. `scrapeResults: true` was rejected
  // outright by the strict request model (extra="forbid"), so
  // `--scrape` never actually worked.
  if (values.scrape) body.scrapeOptions = {};
  const query = requirePositional(positionals[0], 'Usage: snoopscan search <query>');
  const data = await client.search(query, body);
  const results = (Array.isArray(data) ? data : data.results) as Record<string, unknown>[] | undefined;
  const list = results ?? [];
  if (!Array.isArray(data) && data.provider) {
    note(`  provider=${data.provider}  results=${list.length}`, flags);
  }
  emit(flags.json ? data : list.map((r) => `${r.title ?? ''}\n  ${r.url ?? ''}`).join('\n'), flags);
  return EXIT_OK;
}

async function cmdCrawl(client: SnoopScan, args: string[]): Promise<number> {
  const { values, positionals } = parseArgs({
    args,
    options: {
      ...GLOBAL_OPTION_SPEC,
      ...PAGE_OPTION_SPEC,
      limit: { type: 'string' },
      wait: { type: 'boolean' },
      'max-wait': { type: 'string' },
    },
    allowPositionals: true,
  });
  const flags = globalFlagsFrom(values);
  const url = requirePositional(positionals[0], 'Usage: snoopscan crawl <url>');
  // A crawl takes page options under scrapeOptions: the API is strict, and
  // `--formats` sent at the top level was rejected on every call.
  const page = pageOptions(values);
  const options: Options = Object.keys(page).length ? { scrapeOptions: page } : {};
  if (values.limit) options.limit = Number(values.limit);

  if (values.wait) {
    const maxWaitMs = values['max-wait'] ? Number(values['max-wait']) * 1000 : undefined;
    const docs = await client.crawlAndWait(url, { ...options, maxWaitMs });
    note(`  ${docs.length} pages`, flags);
    const joined = docs.map((d) => d.markdown ?? '').join('\n\n---\n\n');
    emit(flags.json ? docs.map((d) => d.raw) : joined, flags);
    return EXIT_OK;
  }
  const job = await client.crawl(url, options);
  note(`  job ${job.id} started — snoopscan crawl-status ${job.id}`, flags);
  emit({ id: job.id, status: job.status }, flags);
  return EXIT_OK;
}

async function cmdCrawlStatus(client: SnoopScan, args: string[]): Promise<number> {
  const { values, positionals } = parseArgs({ args, options: GLOBAL_OPTION_SPEC, allowPositionals: true });
  const flags = globalFlagsFrom(values);
  const jobId = requirePositional(positionals[0], 'Usage: snoopscan crawl-status <job-id>');
  const job = await client.crawlStatus(jobId);
  emit(
    { id: job.id, status: job.status, total: job.total, completed: job.completed, failed: job.failed },
    flags,
  );
  return EXIT_OK;
}

async function cmdExtract(client: SnoopScan, args: string[]): Promise<number> {
  const { values, positionals } = parseArgs({
    args,
    options: { ...GLOBAL_OPTION_SPEC, schema: { type: 'string' }, prompt: { type: 'string' } },
    allowPositionals: true,
  });
  const flags = globalFlagsFrom(values);
  const schemaArg = requirePositional(values.schema, 'Usage: snoopscan extract <url...> --schema <json-or-path>');
  const schema = schemaArg.trim().startsWith('{')
    ? JSON.parse(schemaArg)
    : JSON.parse(readFileSync(schemaArg, 'utf-8'));
  const rows = await client.extract(positionals, schema, { prompt: values.prompt });
  emit(rows, flags);
  return EXIT_OK;
}

async function cmdParse(client: SnoopScan, args: string[]): Promise<number> {
  const { values, positionals } = parseArgs({ args, options: GLOBAL_OPTION_SPEC, allowPositionals: true });
  const flags = globalFlagsFrom(values);
  const target = requirePositional(positionals[0], 'Usage: snoopscan parse <url-or-file>');
  const data = target.startsWith('http://') || target.startsWith('https://')
    ? await client.parse({ url: target })
    // The file as it is on disk: reading it as UTF-8 text broke every PDF.
    : await client.parse({ content: new Uint8Array(readFileSync(target)), filename: basename(target) });
  receipt(data, flags);
  emit(flags.json ? data : (data.markdown as string) ?? '', flags);
  return EXIT_OK;
}

async function cmdProducts(client: SnoopScan, args: string[]): Promise<number> {
  const { values, positionals } = parseArgs({ args, options: GLOBAL_OPTION_SPEC, allowPositionals: true });
  const flags = globalFlagsFrom(values);
  const url = requirePositional(positionals[0], 'Usage: snoopscan products <url>');
  const data = await client.products(url);
  const products = (data.products as Record<string, unknown>[]) ?? [];
  note(`  platform=${data.platform}  total=${data.total}  returned=${products.length}`, flags);
  emit(flags.json ? data : listing(products, ['title', 'name']), flags);
  return EXIT_OK;
}

async function cmdPosts(client: SnoopScan, args: string[]): Promise<number> {
  const { values, positionals } = parseArgs({ args, options: GLOBAL_OPTION_SPEC, allowPositionals: true });
  const flags = globalFlagsFrom(values);
  const url = requirePositional(positionals[0], 'Usage: snoopscan posts <url>');
  const data = await client.posts(url);
  const posts = (data.posts as Record<string, unknown>[]) ?? [];
  note(`  platform=${data.platform}  source=${data.source}  returned=${posts.length}`, flags);
  emit(flags.json ? data : listing(posts, ['title', 'name']), flags);
  return EXIT_OK;
}

async function cmdCompany(client: SnoopScan, args: string[]): Promise<number> {
  const { values, positionals } = parseArgs({
    args,
    options: { ...GLOBAL_OPTION_SPEC, 'no-contacts': { type: 'boolean' } },
    allowPositionals: true,
  });
  const flags = globalFlagsFrom(values);
  const url = requirePositional(positionals[0], 'Usage: snoopscan company <url>');
  const data = await client.company(url, { contacts: !values['no-contacts'] });
  const company = (data.company as Record<string, unknown>) ?? {};
  const contacts = (data.contacts as Record<string, unknown>) ?? {};
  const emails = (contacts.emails as unknown[]) ?? [];
  note(`  name=${JSON.stringify(company.name)}  emails=${emails.length}  pagesRead=${data.pagesRead}`, flags);
  emit(data, flags);
  return EXIT_OK;
}

async function cmdDomain(client: SnoopScan, args: string[]): Promise<number> {
  const { values, positionals } = parseArgs({
    args,
    options: {
      ...GLOBAL_OPTION_SPEC,
      'no-registration': { type: 'boolean' },
      'no-dns': { type: 'boolean' },
      'no-backlinks': { type: 'boolean' },
    },
    allowPositionals: true,
  });
  const flags = globalFlagsFrom(values);
  const domainName = requirePositional(positionals[0], 'Usage: snoopscan domain <domain>');
  const data = await client.domain(domainName, {
    registration: !values['no-registration'],
    dns: !values['no-dns'],
    backlinks: !values['no-backlinks'],
  });
  note(`  domain=${data.domain}`, flags);
  emit(data, flags);
  return EXIT_OK;
}

async function cmdMonitor(client: SnoopScan, args: string[]): Promise<number> {
  const { values, positionals } = parseArgs({
    args,
    options: {
      ...GLOBAL_OPTION_SPEC,
      name: { type: 'string' },
      urls: { type: 'string' },
      interval: { type: 'string' },
      goal: { type: 'string' },
    },
    allowPositionals: true,
  });
  const flags = globalFlagsFrom(values);
  const action = positionals[0];
  const monitorIdUsage = 'Usage: snoopscan monitor <get|run|checks|delete> <monitor-id>';

  switch (action) {
    case 'list':
      emit(await client.monitors(), flags);
      break;
    case 'get':
      emit(await client.monitor(requirePositional(positionals[1], monitorIdUsage)), flags);
      break;
    case 'run':
      emit(await client.runMonitor(requirePositional(positionals[1], monitorIdUsage)), flags);
      break;
    case 'checks':
      emit(await client.monitorChecks(requirePositional(positionals[1], monitorIdUsage)), flags);
      break;
    case 'delete': {
      const monitorId = requirePositional(positionals[1], monitorIdUsage);
      await client.deleteMonitor(monitorId);
      note(`  deleted ${monitorId}`, flags);
      break;
    }
    case 'create':
      emit(
        await client.createMonitor(values.name ?? '', (values.urls ?? '').split(','), {
          intervalMinutes: values.interval ? Number(values.interval) : undefined,
          goal: values.goal,
        }),
        flags,
      );
      break;
    default:
      process.stderr.write('Usage: snoopscan monitor <list|get|run|checks|delete|create> [id]\n');
      return EXIT_ERROR;
  }
  return EXIT_OK;
}

async function cmdConfig(args: string[]): Promise<number> {
  const { values, positionals } = parseArgs({ args, options: GLOBAL_OPTION_SPEC, allowPositionals: true });
  void values;
  const action = positionals[0];
  const path = userConfig.configPath();

  if (action === 'path') {
    process.stdout.write(path + '\n');
    return EXIT_OK;
  }
  if (action === 'show') {
    const stored = userConfig.load();
    process.stdout.write(`  file     ${path}\n`);
    for (const key of userConfig.KNOWN) {
      const value = stored[key] ?? '';
      const shown = key.endsWith('key') ? userConfig.redact(value) : value;
      process.stdout.write(`  ${key.padEnd(9)}${shown || '—'}\n`);
    }
    for (const [env, key] of [[ENV_KEY, 'api_key'], [ENV_URL, 'base_url']] as const) {
      if (process.env[env]) process.stdout.write(`  NOTE     $${env} is set and overrides ${key} above\n`);
    }
    return EXIT_OK;
  }
  if (action === 'get') {
    // Prints the raw value, so an agent can put the real key into an MCP config
    // or .env itself rather than hand anyone a placeholder.
    const name = positionals[1];
    if (!name || !(userConfig.KNOWN as readonly string[]).includes(name)) {
      process.stderr.write(`Usage: snoopscan config get <${userConfig.KNOWN.join('|')}>\n`);
      return EXIT_ERROR;
    }
    const value =
      (name === 'api_key' ? process.env[ENV_KEY] : undefined) || userConfig.load()[name as userConfig.KnownKey];
    if (!value) {
      process.stderr.write(name === 'api_key' ? NO_KEY : `${name} is not set.\n`);
      return EXIT_ERROR;
    }
    process.stdout.write(value + '\n');
    return EXIT_OK;
  }
  if (action === 'set') {
    const name = positionals[1];
    const value = positionals[2];
    if (!name || !value) {
      process.stderr.write('Usage: snoopscan config set <api_key|base_url> <value>\n');
      return EXIT_ERROR;
    }
    if (!(userConfig.KNOWN as readonly string[]).includes(name)) {
      process.stderr.write(`Unknown setting ${name}. Known: ${userConfig.KNOWN.join(', ')}\n`);
      return EXIT_ERROR;
    }
    const written = userConfig.save({ [name]: value } as Partial<Record<userConfig.KnownKey, string>>);
    const shown = name.endsWith('key') ? userConfig.redact(value) : value;
    process.stdout.write(`  ${name} = ${shown}\n  saved to ${written} (0600)\n`);
    return EXIT_OK;
  }
  process.stderr.write('Usage: snoopscan config <show|get|set|path>\n');
  return EXIT_ERROR;
}

export interface LoginDeps {
  fetch: typeof fetch;
  open: (url: string) => boolean;
  sleep: (ms: number) => Promise<void>;
}

function openInBrowser(url: string): boolean {
  const cmd = process.platform === 'darwin' ? 'open' : process.platform === 'win32' ? 'cmd' : 'xdg-open';
  const argv = process.platform === 'win32' ? ['/c', 'start', '', url] : [url];
  try {
    spawn(cmd, argv, { stdio: 'ignore', detached: true }).on('error', () => {}).unref();
    return true;
  } catch {
    return false;
  }
}

const defaultLoginDeps: LoginDeps = {
  fetch: (...a) => fetch(...a),
  open: openInBrowser,
  sleep: (ms) => new Promise((r) => setTimeout(r, ms)),
};

/** Sign in through the browser and save a fresh key: nothing to copy.
 * The device-code pattern, mirroring the Python CLI: ask the site for a pair
 * of codes, open the browser at the short one, poll with the long one until
 * the person approves. The short code is printed here and shown on the approve
 * page, so they can see the request they approve is this one. */
export async function cmdLogin(args: string[], deps: LoginDeps = defaultLoginDeps): Promise<number> {
  const { values } = parseArgs({
    args,
    options: { 'no-browser': { type: 'boolean' }, 'account-url': { type: 'string' } },
    allowPositionals: true,
    strict: false,
  });
  const account = String(values['account-url'] || process.env[ENV_ACCOUNT] || DEFAULT_ACCOUNT_URL).replace(/\/+$/, '');
  const post = (path: string, body: unknown) =>
    deps.fetch(account + path, { method: 'POST', headers: { 'Content-Type': 'application/json', Accept: 'application/json' }, body: JSON.stringify(body) });
  try {
    const started = await post('/api/cli/login/start', { host: hostname() });
    if (started.status === 429) {
      process.stderr.write('Too many login attempts from here. Wait a minute and try again.\n');
      return EXIT_ERROR;
    }
    if (!started.ok) throw new Error(`HTTP ${started.status}`);
    const data = ((await started.json()) as { data: Record<string, unknown> }).data;
    const verifyUrl = String(data.verifyUrl);
    process.stdout.write('Sign in to SnoopScan in your browser to connect this terminal.\n');
    process.stdout.write(`  Code: ${data.userCode}   (check it matches the one on the page)\n`);
    process.stdout.write(`  Link: ${verifyUrl}\n`);
    if (!values['no-browser'] && !deps.open(verifyUrl)) process.stdout.write('  Open the link above in any browser.\n');
    let interval = Math.max(1, Number(data.interval ?? 2)) * 1000;
    const deadline = Date.now() + Number(data.expiresIn ?? 600) * 1000;
    process.stdout.write('Waiting for you to approve…\n');
    while (Date.now() < deadline) {
      await deps.sleep(interval);
      const polled = await post('/api/cli/login/poll', { deviceCode: data.deviceCode });
      if (polled.status === 429) {
        interval += 1000;
        continue;
      }
      if (!polled.ok) throw new Error(`HTTP ${polled.status}`);
      const state = ((await polled.json()) as { data: { status?: string; apiKey?: string } }).data;
      if (state.status === 'approved' && state.apiKey) {
        const written = userConfig.save({ api_key: state.apiKey });
        process.stdout.write(`Logged in. Key saved to ${written} (0600).\nNext: snoopscan status\n`);
        return EXIT_OK;
      }
      if (state.status === 'denied') {
        process.stderr.write('Cancelled in the browser. Nothing was saved.\n');
        return EXIT_ERROR;
      }
      if (state.status === 'expired') break;
    }
  } catch (err) {
    process.stderr.write(`Could not reach ${account}: ${(err as Error).message}\n`);
    return EXIT_ERROR;
  }
  process.stderr.write('The login link expired. Run snoopscan login again.\n');
  return EXIT_ERROR;
}

export interface DoctorDeps {
  fetch: typeof fetch;
  latest: () => Promise<string | undefined>;
}

/** Check everything a first run depends on, and say how to fix what is wrong —
 * the JS twin of the Python CLI's `doctor`. The key is checked on a free call
 * (GET /v1/templates: no fetch, no charge) and never printed. */
export async function cmdDoctor(
  args: string[],
  deps: DoctorDeps = { fetch: (...a) => fetch(...a), latest: () => update.latestVersion({ force: true }) },
): Promise<number> {
  const { values } = parseArgs({ args, options: GLOBAL_OPTION_SPEC, allowPositionals: true, strict: false });
  const flags = globalFlagsFrom(values);
  const stored = userConfig.load();
  const baseUrl = (flags.baseUrl || process.env[ENV_URL] || stored.base_url || DEFAULT_BASE_URL).replace(/\/+$/, '');
  let ok = true;
  const line = (label: string, text: string, good = true) => {
    ok = ok && good;
    process.stdout.write(`  ${good ? 'ok ' : 'FIX'}  ${label.padEnd(10)}${text}\n`);
  };

  const current = update.currentVersion();
  const latest = await deps.latest();
  if (latest && update.isNewer(latest, current)) {
    line('snoopscan', `${current}, and ${latest} is out. Update: npm install -g snoopscan@latest (npx snoopscan@latest is always current)`, false);
  } else {
    line('snoopscan', `${current}${latest ? ' (the latest)' : ' (could not check npm)'}`);
  }
  const major = Number(process.versions.node.split('.')[0]);
  line('node', process.versions.node + (major >= 18 ? '' : '. Needs 18 or newer: update Node from https://nodejs.org, or use uvx snoopscan'), major >= 18);
  line('config', userConfig.configPath());

  const key = (flags.apiKey || process.env[ENV_KEY] || stored.api_key || '').trim();
  const source = flags.apiKey ? '--api-key' : process.env[ENV_KEY] ? `$${ENV_KEY}` : 'config';
  if (!key) {
    line('key', 'not set. Fix: snoopscan login', false);
  } else {
    try {
      const r = await deps.fetch(`${baseUrl}/v1/templates`, { headers: { Authorization: `Bearer ${key}` } });
      if (r.status === 200) line('key', `${userConfig.redact(key)} from ${source}, accepted`);
      else if (r.status === 401) line('key', `${userConfig.redact(key)} from ${source} is not recognised. Fix: snoopscan login`, false);
      else line('key', `could not check (HTTP ${r.status})`, false);
    } catch (err) {
      line('key', `could not reach ${baseUrl} (${(err as Error).name})`, false);
    }
  }
  try {
    const health = (await (await deps.fetch(`${baseUrl}/health`)).json()) as { status?: string };
    line('engine', `${health.status ?? '?'} at ${baseUrl}`);
  } catch (err) {
    line('engine', `unreachable at ${baseUrl} (${(err as Error).name})`, false);
  }
  process.stdout.write(ok ? '  All good.\n' : '  Fix the lines marked FIX, then run: snoopscan doctor\n');
  return ok ? EXIT_OK : EXIT_ERROR;
}

/** Whether this is configured AND whether the engine can do the hard work —
 * the second half matters the same way it does for the Python CLI. */
async function cmdStatus(client: SnoopScan, args: string[]): Promise<number> {
  const { values } = parseArgs({ args, options: GLOBAL_OPTION_SPEC, allowPositionals: true });
  void values;
  const base = (client as unknown as { baseUrl: string }).baseUrl;
  process.stdout.write(`  api      ${base}\n`);
  try {
    const res = await fetch(`${base}/health`, { signal: AbortSignal.timeout(10_000) });
    const health = (await res.json()) as Record<string, unknown>;
    process.stdout.write(`  engine   ${health.status ?? '?'}  v${health.version ?? '?'}\n`);
    const tiers = (health.tiers as string[]) ?? [];
    process.stdout.write(`  tiers    ${tiers.length ? tiers.join(', ') : 'unknown'}\n`);
    if (health.deepTiersAvailable === false) {
      process.stdout.write('  WARNING  the deep rungs are missing — hard sites will report BLOCKED\n');
    }
    const degraded = health.searchProvidersDegraded as string[] | undefined;
    if (degraded?.length) process.stdout.write(`  WARNING  search rungs degraded: ${degraded.join(', ')}\n`);
    if (health.saturated) process.stdout.write(`  WARNING  engine saturated (loop lag ${health.recentLagMs}ms)\n`);
  } catch (err) {
    process.stdout.write(`  engine   unreachable (${(err as Error).name})\n`);
    return EXIT_ERROR;
  }
  return EXIT_OK;
}

const COMMANDS: Record<string, Command> = {
  scrape: cmdScrape,
  crawl: cmdCrawl,
  'crawl-status': cmdCrawlStatus,
  map: cmdMap,
  search: cmdSearch,
  extract: cmdExtract,
  parse: cmdParse,
  products: cmdProducts,
  posts: cmdPosts,
  company: cmdCompany,
  domain: cmdDomain,
  monitor: cmdMonitor,
  status: cmdStatus,
};

/** Run a command, then (on stderr, never in piped output) say if a newer release is out. */
async function withNotice(run: Promise<number>): Promise<number> {
  const code = await run;
  const line = await update.notice(update.currentVersion());
  if (line) process.stderr.write(line);
  return code;
}

async function main(): Promise<number> {
  const [command, ...rest] = process.argv.slice(2);
  if (!command || command === '-h' || command === '--help') {
    process.stdout.write(
      'usage: snoopscan <login|doctor|scrape|crawl|crawl-status|map|search|extract|parse|products|posts|company|domain|monitor|config|status> ...\n',
    );
    return command ? EXIT_OK : EXIT_ERROR;
  }

  if (command === 'config') {
    return withNotice(cmdConfig(rest));
  }
  if (command === 'login') {
    return withNotice(cmdLogin(rest));
  }
  if (command === 'doctor') {
    return cmdDoctor(rest);
  }

  const handler = COMMANDS[command];
  if (!handler) {
    process.stderr.write(`Unknown command: ${command}\n`);
    return EXIT_ERROR;
  }

  // Only the global flags are needed here, to resolve the client before the
  // command's own (fuller) parseArgs call runs on the same argv.
  const { values } = parseArgs({ args: rest, options: GLOBAL_OPTION_SPEC, allowPositionals: true, strict: false });
  const client = resolveClient(globalFlagsFrom(values));

  try {
    return await withNotice(handler(client, rest));
  } catch (err) {
    if (err instanceof SnoopScanError) {
      process.stderr.write(`${err.code}: ${err.message.replace(`${err.code}: `, '')}\n`);
      return err.isTargetError ? EXIT_TARGET_ERROR : EXIT_ERROR;
    }
    process.stderr.write(`${(err as Error).message}\n`);
    return EXIT_ERROR;
  }
}

// Only when this file IS the entrypoint (the installed `snoopscan` bin), not
// when a test imports `cmdSearch` or anything else from it — an ESM module
// runs its top-level code on import regardless of what was imported, so
// without this guard every test file pulling from cli.ts would also launch
// the real CLI and call process.exit on the test runner. `argv[1]` is
// relative (however the shell invoked it) and unencoded; comparing it
// directly against `import.meta.url` (always absolute, always
// percent-encoded) fails for any path with a space in it — pathToFileURL
// normalizes both the same way.
// And through the REAL path: npm installs the bin as a symlink
// (node_modules/.bin/snoopscan -> ../snoopscan/dist/cli.js), and
// import.meta.url is the file behind it. Comparing against the symlink's own
// path never matched, so `npx snoopscan` and every global install exited 0
// having done nothing at all (found 22 Sep 2026, broken since the guard went in).
if (isEntrypoint()) {
  main().then((code) => process.exit(code));
}

function isEntrypoint(): boolean {
  const invoked = process.argv[1];
  if (!invoked) return false;
  try {
    return import.meta.url === pathToFileURL(realpathSync(invoked)).href;
  } catch {
    return import.meta.url === pathToFileURL(invoked).href;
  }
}
