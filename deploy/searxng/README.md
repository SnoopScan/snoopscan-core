# SearXNG — the free rung

`/v1/search` leads with this. It is not optional infrastructure: with it gone the
ladder falls through to the **paid** rung, so search does not break — it silently
starts costing 10 credits a query instead of 2. That is a worse failure than an
outage because nothing looks wrong.

## Running it

`docker compose up searxng` brings it up with the rest of the stack. Two values are
substituted into `settings.yml` at start from the environment:

| Variable | What it is |
|---|---|
| `SEARXNG_SECRET` | Any long random string. `openssl rand -hex 32`. |
| `SEARXNG_PROXY_URL` | The residential proxy URL its upstream queries leave through. |

Then point the engine at it: `ENGINE_SEARXNG_URL=http://searxng:8888` inside compose,
or `http://127.0.0.1:8888` when running it directly.

## Production, as actually deployed (17 Sep 2026)

The box has no Docker, so SearXNG runs natively. Until this was installed the
ladder on production was `searxng,duckduckgo` with `ENGINE_SEARXNG_URL` empty —
one rung — and two dropped DuckDuckGo connections tripped its breaker for three
minutes, which on a one-rung ladder is a full `/v1/search` outage.

| Piece | Where |
|---|---|
| Code | `/srv/searxng/src` (git clone, depth 1) |
| Python | `/srv/searxng/venv` — `python3.11 -m venv` (needs the `python3.11-venv` apt package) |
| Settings | `/srv/searxng/settings.yml`, mode 600, owned by `searxng` |
| Service | `searxng.service`, user `searxng`, `SEARXNG_SETTINGS_PATH` set in the unit |
| Engine | `ENGINE_SEARXNG_URL=http://127.0.0.1:8888` |

Two deliberate differences from `settings.yml` in this repo:

- **`bind_address: 127.0.0.1`.** The engine is its only client; nothing outside the box
  should be able to query it.
- **No `outgoing.proxies`.** Every query fans out to several upstream engines, and at
  residential bandwidth prices that costs more than a 2-credit search earns. Measured
  direct from the box: 32 results for a test query, answered by Brave and Google CSE.
  Add the proxy back only if those start returning nothing.

`systemctl status searxng` for health; `journalctl -u searxng` for its log.

## Checking it

The engine's `search_health` canary asks every rung a question with a known answer
every fifteen minutes and logs `search_ladder_thinned` when one goes quiet. To check
by hand:

    curl -s 'http://127.0.0.1:8888/search?q=wikipedia&format=json' | head -c 200

A response that is HTML rather than JSON means `search.formats` lost its `json` entry —
that is the single most common way this breaks, and it is silent.

## Local development without Docker

The free rung on a developer Mac. Everything lives in `~/.snoop/searxng` — never in
`/tmp`, which macOS empties on reboot and which took the whole install with it once.

    D=~/.snoop/searxng; mkdir -p $D && cd $D
    git clone --depth 1 https://github.com/searxng/searxng.git src
    /opt/homebrew/bin/python3.14 -m venv venv        # NOT `python3`: that is Xcode's 3.9 on this Mac
    venv/bin/pip install -U pip && venv/bin/pip install -r src/requirements.txt
    cp deploy/searxng/settings.yml $D/settings.yml   # from this repo; json format is what the engine needs
    $D/start.sh                                      # runs `python -m searx.webapp` from src (no editable install — its isolated build cannot import msgspec)

`start.sh` is in `~/.snoop/searxng`; after a reboot it is the only command needed.
Check: `curl -s 'http://127.0.0.1:8888/search?q=wikipedia&format=json' | head -c 200`.
