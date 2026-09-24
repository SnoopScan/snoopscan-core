# snoopscan

Python client for SnoopScan, the web scraping API for AI agents.

MIT licensed. The server is AGPL-3.0; a client library must not be, or every
application that imports it inherits the copyleft.

```bash
pip install snoopscan
snoopscan login        # opens your browser: sign in or sign up (free, no card), approve, key saved
snoopscan doctor       # checks the install, the key and the API, and says how to fix anything
```

Needs Python 3.10+ (a Mac's built-in Python is 3.9: use `uvx snoopscan login`, which brings its own).
If `snoopscan` is not found after installing, run it as `python3 -m snoopscan`.

```python
import os
from snoopscan import SnoopScan

# `snoopscan config get api_key` prints the key login saved; put it in your environment.
snoop = SnoopScan(api_key=os.environ["SNOOPSCAN_API_KEY"])

page = snoop.scrape("https://example.com/")
print(page.markdown)

for link in snoop.map("https://example.com/", limit=100):
    print(link.url)

job = snoop.crawl_and_wait("https://example.com/", limit=50)
print(job.completed, "of", job.total)
```

## Templates

Structured fields without writing a schema. A template names a kind of page —
`product`, `article`, `jobPosting`, `localBusiness`, `event`, `recipe` — and its
fields are the schema.org names sites already publish, so they usually come
straight from the page's own markup, with no model call.

```python
rows = snoop.extract(["https://shop.example/product/123"], template="product")
print(rows[0]["data"])  # {"name": ..., "price": ..., "currency": ..., ...}

for t in snoop.templates():  # what this deployment offers
    print(t["name"], t["fields"])
```

Give a `schema` **or** a `template`, never both — a template *is* a schema.
Passing both, or neither, raises before the request is sent.

## Polling a crawl yourself

`crawl_and_wait` does this for you. When you want control over the wait:

```python
import time

job = snoop.crawl("https://example.com/", limit=500)
while not (job := snoop.crawl_status(job.id)).finished:  # completed, failed or cancelled
    time.sleep(5)
```

## Platforms

A site that publishes its data as JSON is asked, not crawled:

```python
catalogue = snoop.products(
    "https://store.example.com"
)  # Shopify, WooCommerce, Squarespace, Magento
for p in catalogue["products"]:
    print(p["title"], p["price"], p["currency"], p["available"])

posts = snoop.posts(
    "https://blog.example.com"
)  # WordPress, Substack, Squarespace, Discourse; else the feed
print(posts["source"], len(posts["posts"]))
```

Every page's `metadata.platform` says what built it, and a `scrape()` of a
Shopify, WooCommerce or Amazon product page carries `product` beside the
markdown. Amazon shows the honest client no price — pass `tier="browser"`.

## Monitors

```python
m = snoop.create_monitor(
    "Pricing",
    ["https://example.com/pricing"],
    intervalMinutes=60,
    webhook="https://hooks.example.com/snoop",
)
check = snoop.run_monitor(m["id"])  # a check now: {"counts": {...}, "pages": [...]}
snoop.monitor_checks(m["id"])  # recent checks
snoop.delete_monitor(m["id"])
```

Each page in a check is `same`, `changed` (with a git diff), `new` or `error`.
The webhook `monitor.check.completed` fires only when a check has something to
say.

## Company & domain

Two lookups that answer questions a single page can't:

```python
lead = snoop.company("acme.com")  # firmographics + contacts, from the site itself
print(lead["company"]["name"], lead["company"]["headcount"])
for email in lead["contacts"]["emails"]:
    print(email["email"], email["role"], email["onDomain"])

info = snoop.domain("acme.com")  # registration, DNS, backlinks — not a page fetch
print(info["registration"]["registrar"], info["dns"]["mx"])
print(info["backlinks"]["referringDomains"], "domains link here, per our own crawl graph")
```

`company()` takes `contacts=False` to skip contact discovery and return only
firmographics. `domain()`'s three lookups are each opt-out —
`registration=False`, `dns=False`, `backlinks=False` — since a caller asking
about a domain usually wants all of it, not a form to fill in.

## Base URL

Defaults to `http://localhost:8099`, the engine's own dev port. Point it
elsewhere with the `SNOOP_BASE_URL` environment variable, or per client:

```python
snoop = SnoopScan(api_key=os.environ["SNOOPSCAN_API_KEY"], base_url="https://api.example.com")
```

## Errors

Every failure raises `SnoopScanError` carrying the API's machine-readable
code, so callers can branch on what actually happened:

```python
from snoopscan import SnoopScanError

try:
    page = snoop.scrape(url)
except SnoopScanError as exc:
    if exc.is_blocked:
        ...  # BLOCKED — the target refused us; retrying as-is will not help
    elif exc.code == "FETCH_FAILED":
        ...  # the target could not be reached; retrying may help
    elif exc.code == "INVALID_REQUEST":
        ...  # our request was wrong; fix it, do not retry
```

## Cost

Every response carries what it cost to produce — the tier that answered, every
tier attempted, proxy bytes, browser milliseconds, and whether it came from
cache. A cache hit reports the accounting of the fetch that filled it, so
`cost.tier` is never null on a page that was really fetched once.

## Development

From a checkout of the engine repo:

```bash
uv pip install -e sdk/python --python .venv/bin/python
```
