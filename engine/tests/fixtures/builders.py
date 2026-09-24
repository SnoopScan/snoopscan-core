"""Fixture HTML, one builder per page type.

Written by hand rather than saved from live sites so the repo carries no
third-party content and no identifying data (constraint C3). Every domain here
is example.com or .invalid.

The fixture suite is the accumulated memory of everything that has broken: add
a builder (or a case to one) every time a real-world extraction is found to be
wrong.
"""

from __future__ import annotations

_NAV = (
    "<nav><a href='/'>Home</a><a href='/news'>News</a><a href='/about'>About</a>"
    "<a href='/contact'>Contact</a></nav>"
)
_COOKIE = "<div class='consent'><p>We use cookies to improve your experience.</p></div>"
_FOOTER = (
    "<footer><p>Subscribe to our newsletter</p>"
    "<a href='/privacy'>Privacy</a><a href='/terms'>Terms</a></footer>"
)


def article_html() -> str:
    paragraphs = "".join(
        f"<p>This is genuine body paragraph {i} of the article, written with enough "
        f"words that the extractor treats it as real prose rather than a stray "
        f"fragment of page furniture.</p>"
        for i in range(1, 6)
    )
    return f"""<!doctype html>
<html lang="en"><head>
<title>How Extraction Actually Works</title>
<meta name="description" content="A description of the extraction pipeline.">
<meta property="og:type" content="article">
<meta name="author" content="Staff Writer">
<meta property="article:published_time" content="2026-08-01T09:00:00Z">
</head><body>
{_NAV}{_COOKIE}
<article>
<h1>How Extraction Actually Works</h1>
{paragraphs}
<h2>A second section</h2>
<p>A further paragraph of real content beneath a subheading, so heading
structure is present in the source and can be checked in the output.</p>
</article>
{_FOOTER}
</body></html>"""


def forum_html() -> str:
    posts = "".join(
        f"""<div class="post">
  <span class="author">author{i}</span>
  <time datetime="2026-08-0{i}T10:00:00Z">Aug {i}, 2026</time>
  <div class="post-body">
    <blockquote>Quoting an earlier reply in the thread here.</blockquote>
    <p>This is the body of post {i}, containing enough words to register as a
    genuine repeating unit of content rather than page scaffolding.</p>
  </div>
</div>"""
        for i in range(1, 6)
    )
    return f"""<!doctype html>
<html lang="en"><head><title>Thread: extraction quality</title></head><body>
{_NAV}
<div class="thread">{posts}</div>
{_FOOTER}
</body></html>"""


def listing_html() -> str:
    cards = "".join(
        f"""<div class="card">
  <img src="/img/{i}.png" alt="">
  <h3 class="title">Product {i}</h3>
  <p class="desc">A short blurb describing product {i} in a sentence.</p>
  <a class="link" href="/p/{i}">View product</a>
</div>"""
        for i in range(1, 9)
    )
    return f"""<!doctype html>
<html lang="en"><head><title>All products</title></head><body>
{_NAV}
<div class="grid">{cards}</div>
{_FOOTER}
</body></html>"""


def product_html() -> str:
    return """<!doctype html>
<html lang="en"><head><title>Mechanical Keyboard</title>
<script type="application/ld+json">
{
  "@context": "https://schema.org",
  "@type": "Product",
  "name": "Mechanical Keyboard",
  "description": "A compact mechanical keyboard.",
  "sku": "KB-001",
  "offers": {
    "@type": "Offer",
    "price": "129.99",
    "priceCurrency": "GBP",
    "availability": "https://schema.org/InStock"
  }
}
</script></head><body>
<nav><a href='/'>Home</a><a href='/shop'>Shop</a></nav>
<main>
<h1>Mechanical Keyboard</h1>
<p class="price">£129.99</p>
<p>A compact mechanical keyboard with hot-swappable switches, suitable for
long typing sessions and available in several layouts.</p>
<ul><li>Hot-swappable switches</li><li>USB-C</li><li>Aluminium case</li></ul>
</main>
</body></html>"""


def table_html() -> str:
    rows = "".join(
        f"<tr><td>Widget {chr(64 + i)}</td><td>{i * 25}</td><td>In stock</td></tr>"
        for i in range(1, 8)
    )
    return f"""<!doctype html>
<html lang="en"><head><title>Product data</title></head><body>
<main><table>
<tr><th>Product</th><th>Units</th><th>Status</th></tr>
{rows}
</table></main>
</body></html>"""


def docs_html() -> str:
    return """<!doctype html>
<html lang="en"><head><title>Getting started</title></head><body>
<nav class="sidebar"><ul>
  <li>Guides<ul><li><a href="/docs/start">Getting started</a></li>
                <li><a href="/docs/config">Configuration</a></li></ul></li>
  <li>Reference<ul><li><a href="/docs/api">API</a></li></ul></li>
</ul></nav>
<nav aria-label="Breadcrumb"><a href="/docs">Docs</a> / Getting started</nav>
<main>
<h1>Getting started</h1>
<p>Install the package and run the initial setup command to create a local
configuration file before starting the server for the first time.</p>
<pre><code class="language-bash">pip install example-package</code></pre>
<h2>Configuration</h2>
<p>Configuration is read from the environment. Every value has a default
suitable for local development, so nothing is required to start.</p>
<pre><code class="language-python">from example import Client

client = Client()
client.run()</code></pre>
</main>
</body></html>"""


def challenge_html(vendor: str = "cloudflare") -> str:
    """A challenge page served with HTTP 200 — the case that matters."""
    if vendor == "cloudflare":
        return """<!doctype html><html><head><title>Just a moment...</title></head>
<body><script>window._cf_chl_opt={cvId:"3"};</script>
<div>Checking your browser before accessing the site.</div>
<div id="cf-content">Enable JavaScript and cookies to continue</div></body></html>"""
    return """<!doctype html><html><head><title>Access denied</title></head>
<body><script src="https://geo.captcha-delivery.com/captcha/"></script>
<div>Please verify you are a human</div></body></html>"""


def short_but_real_html() -> str:
    """A legitimately short page. The false-positive guard: detection that
    flags real content is worse than detection that misses a block."""
    return """<!doctype html>
<html lang="en"><head><title>Release note</title></head><body>
<article><h1>Version 2.1 released</h1>
<p>This release fixes a caching bug and updates two dependencies. No action is
required for existing installations.</p></article>
</body></html>"""
