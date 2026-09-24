/**
 * Where the CLI keeps its settings between shells. Same file, same format,
 * same resolution order as the Python CLI's `snoopscan config` — a flag
 * beats the environment beats this file — so configuring once works for
 * whichever language's CLI is installed, not one config per ecosystem.
 */

import { homedir } from 'node:os';
import { join, dirname } from 'node:path';
import { mkdirSync, readFileSync, writeFileSync, existsSync, chmodSync } from 'node:fs';

export const KNOWN = ['api_key', 'base_url'] as const;
export type KnownKey = (typeof KNOWN)[number];

export function configPath(): string {
  const root = process.env.XDG_CONFIG_HOME || join(homedir(), '.config');
  return join(root, 'snoopscan', 'config.toml');
}

/** Reads the flat `key = "value"` lines the Python CLI writes. Not a real
 * TOML parser — the file format here is deliberately that simple. */
export function load(): Partial<Record<KnownKey, string>> {
  const path = configPath();
  if (!existsSync(path)) return {};
  let text: string;
  try {
    text = readFileSync(path, 'utf-8');
  } catch {
    return {};
  }
  const out: Partial<Record<KnownKey, string>> = {};
  for (const line of text.split('\n')) {
    const match = line.match(/^(\w+)\s*=\s*"(.*)"\s*$/);
    if (!match) continue;
    const [, key, value] = match;
    if (key !== undefined && value !== undefined && (KNOWN as readonly string[]).includes(key)) {
      out[key as KnownKey] = value;
    }
  }
  return out;
}

/** Merge into the file, creating it 0600. The mode is set on write, not
 * after — a key briefly world-readable has already been readable. */
export function save(values: Partial<Record<KnownKey, string>>): string {
  const path = configPath();
  mkdirSync(dirname(path), { recursive: true });

  const merged = { ...load(), ...values };
  const lines = ['# snoopscan CLI settings. Written by `snoopscan config set`.'];
  for (const key of KNOWN) {
    if (merged[key] !== undefined) lines.push(`${key} = "${merged[key]}"`);
  }
  writeFileSync(path, lines.join('\n') + '\n', { mode: 0o600 });
  chmodSync(path, 0o600);
  return path;
}

/** Enough to tell two keys apart, not enough to use one. */
export function redact(secret: string): string {
  if (!secret) return '';
  if (secret.length <= 12) return '*'.repeat(secret.length);
  return `${secret.slice(0, 7)}…${secret.slice(-4)}`;
}
