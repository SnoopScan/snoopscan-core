import { defineConfig } from 'tsup';

export default defineConfig([
  {
    entry: { index: 'src/index.ts' },
    format: ['esm', 'cjs'],
    dts: true,
    sourcemap: true,
    clean: true,
    // Browser-safe: no Node built-ins are imported from client.ts/errors.ts/
    // types.ts, only config.ts (CLI-only) touches node:fs/node:os/node:path.
    platform: 'neutral',
  },
  {
    // The CLI is Node-only (reads files, writes the config file) and ships
    // as a single executable entry — no dual format needed for a bin script.
    // The shebang already in src/cli.ts is preserved by tsup automatically,
    // which is also what makes the built output chmod +x.
    entry: { cli: 'src/cli.ts' },
    format: ['esm'],
    sourcemap: true,
    platform: 'node',
  },
]);
