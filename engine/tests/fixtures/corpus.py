"""The extraction fixture corpus.

04-extraction.md section 9 makes this non-negotiable, and for a specific
reason: extraction regressions are SILENT. The output still looks like text,
so nothing fails and nobody notices until a consumer downstream is confidently
wrong about a page it never really read.

Every fixture pairs an HTML page with a REFERENCE extraction — the text a
careful human would say belongs in the output. Scoring is token-level F1
against that reference, which is what makes "did this change help?" answerable
instead of a matter of opinion.

Fixtures are hand-authored rather than saved from live sites. Third-party HTML
in a public AGPL repository is a copyright question we do not need, and every
page here uses example.com (constraint C3). What matters is that each one
reproduces a STRUCTURE that has actually broken extraction — several encode
bugs found on live runs, named in their notes.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from engine.core.models import PageType


@dataclass
class Fixture:
    name: str
    page_type: PageType
    html: str
    # The text that belongs in the output. Scored by token F1, so wording
    # matters but ordering and markdown syntax do not.
    reference: str
    # Content that must survive. A high F1 can still hide a lost heading.
    must_contain: tuple[str, ...] = ()
    # Content that must NOT survive — nav, cookie notices, recirculation.
    must_not_contain: tuple[str, ...] = ()
    min_words: int = 0
    min_tables: int = 0
    min_code_blocks: int = 0
    note: str = ""


_NAV = (
    "<nav><a href='/'>Home</a><a href='/products'>Products</a>"
    "<a href='/pricing'>Pricing</a><a href='/about'>About</a>"
    "<a href='/contact'>Contact</a></nav>"
)
_COOKIE = "<div class='cc'><p>We use cookies to improve your experience.</p></div>"
_FOOTER = (
    "<footer><p>Subscribe to our newsletter</p><a href='/privacy'>Privacy</a>"
    "<a href='/terms'>Terms</a><p>Related articles</p></footer>"
)


def _article() -> Fixture:
    body = (
        "<h1>Understanding Connection Pooling</h1>"
        "<p>A connection pool keeps a set of open database connections ready "
        "so that a request does not pay the cost of establishing one.</p>"
        "<p>The cost being avoided is real. A TLS handshake against a remote "
        "database can take longer than the query it precedes.</p>"
        "<h2>Sizing the pool</h2>"
        "<p>Pools are usually sized far too large. A pool bigger than the "
        "database can service adds queueing inside your application instead "
        "of inside the database, which is harder to observe.</p>"
        "<p>Start from the number of cores the database has, not from the "
        "number of workers you happen to run.</p>"
    )
    return Fixture(
        name="article_pooling",
        page_type=PageType.ARTICLE,
        html=(
            f"<!doctype html><html lang='en'><head><title>Understanding Connection "
            f"Pooling</title><meta property='og:type' content='article'>"
            f"</head><body>{_NAV}{_COOKIE}<article>{body}</article>{_FOOTER}</body></html>"
        ),
        reference=(
            "Understanding Connection Pooling A connection pool keeps a set of open "
            "database connections ready so that a request does not pay the cost of "
            "establishing one. The cost being avoided is real. A TLS handshake against "
            "a remote database can take longer than the query it precedes. Sizing the "
            "pool Pools are usually sized far too large. A pool bigger than the database "
            "can service adds queueing inside your application instead of inside the "
            "database, which is harder to observe. Start from the number of cores the "
            "database has, not from the number of workers you happen to run."
        ),
        must_contain=("Understanding Connection Pooling", "Sizing the pool"),
        must_not_contain=("We use cookies", "Subscribe to our newsletter"),
        min_words=90,
        note="Baseline article. Nav, cookie banner and footer must all go.",
    )


def _news_index() -> Fixture:
    """The ZDNet case: a homepage where every teaser is an <article>."""
    stories = [
        ("Database pooling revisited", "Why most connection pools are sized wrong."),
        ("The cost of a TLS handshake", "Measuring what a connection actually costs."),
        ("Queue depth as a signal", "What queue growth tells you before latency does."),
        ("Reading an execution plan", "A practical guide to query plans."),
        ("Indexes that are never used", "Finding and removing dead indexes."),
        ("Vacuum and bloat", "How table bloat accumulates and what to do."),
    ]
    cards = "".join(
        f"<article><h2>{title}</h2><p>{blurb}</p><a href='/story/{i}'>Read more</a></article>"
        for i, (title, blurb) in enumerate(stories)
    )
    return Fixture(
        name="listing_news_index",
        page_type=PageType.LISTING,
        html=(
            f"<!doctype html><html lang='en'><head><title>Tech News</title>"
            f"<meta property='og:type' content='article'></head><body>{_NAV}"
            f"<main>{cards}</main>{_FOOTER}</body></html>"
        ),
        reference=" ".join(f"{title} {blurb}" for title, blurb in stories),
        must_contain=tuple(title for title, _ in stories),
        must_not_contain=("We use cookies",),
        min_words=60,
        note=(
            "Regression: measured live, a news homepage with 46 <article> elements "
            "scored as a single article and yielded 355 chars of 125,317 available. "
            "One <article> is an article; many is an index of them."
        ),
    )


def _forum() -> Fixture:
    # Quotes differ per post on purpose. Four identical quoted lines would be
    # collapsed by the repeated-line sweep — correctly, since that is a
    # failed-extraction artefact — and the fixture would then measure the
    # sweep rather than the forum extractor.
    posts = [
        (
            "ana",
            "2026-08-01T09:00:00Z",
            "Opening the thread here.",
            "Has anyone measured the handshake cost directly?",
        ),
        (
            "bram",
            "2026-08-01T10:30:00Z",
            "Quoting ana about measurement.",
            "We did. It was around forty milliseconds cross-region.",
        ),
        (
            "carys",
            "2026-08-01T11:15:00Z",
            "Quoting bram on the cross-region figure.",
            "That matches ours. Pool size stopped mattering after.",
        ),
        (
            "dev",
            "2026-08-01T12:40:00Z",
            "Quoting carys about pool sizing.",
            "Worth noting the driver caches some of this already.",
        ),
    ]
    body = "".join(
        f"<div class='post'><span class='author'>{author}</span>"
        f"<time datetime='{when}'>{when}</time>"
        f"<div class='body'><blockquote>{quote}</blockquote>"
        f"<p>{text}</p></div></div>"
        for author, when, quote, text in posts
    )
    return Fixture(
        name="forum_thread",
        page_type=PageType.FORUM,
        html=(
            f"<!doctype html><html lang='en'><head><title>Handshake cost</title>"
            f"</head><body>{_NAV}<div class='thread'>{body}</div>{_FOOTER}</body></html>"
        ),
        # Timestamps belong in the reference: the spec asks for one section per
        # post with author AND timestamp as the heading, so an extractor that
        # emits them is right and the reference must credit it.
        reference=" ".join(
            f"{author} {when} {quote} {text}" for author, when, quote, text in posts
        ),
        must_contain=tuple(author for author, _, _, _ in posts)
        + tuple(text[:30] for _, _, _, text in posts),
        must_not_contain=("We use cookies",),
        min_words=40,
        note="Per-post attribution and quote nesting must survive.",
    )


def _product() -> Fixture:
    return Fixture(
        name="product_keyboard",
        page_type=PageType.PRODUCT,
        html=(
            "<!doctype html><html lang='en'><head><title>Compact Keyboard</title>"
            '<script type="application/ld+json">'
            '{"@context":"https://schema.org","@type":"Product",'
            '"name":"Compact Keyboard","sku":"KB-88",'
            '"offers":{"@type":"Offer","price":"129.99","priceCurrency":"GBP",'
            '"availability":"https://schema.org/InStock"}}'
            "</script></head><body>"
            + _NAV
            + "<main><h1>Compact Keyboard</h1><p class='price'>£129.99</p>"
            "<p>An eighty-eight key mechanical keyboard with hot-swappable "
            "switches and an aluminium case, intended for long typing sessions.</p>"
            "<ul><li>Hot-swappable switches</li><li>USB-C</li>"
            "<li>Aluminium case</li></ul></main>" + _FOOTER + "</body></html>"
        ),
        # The labelled fields are credited deliberately. A bare "129.99" is not
        # a useful extraction of a product page — the label is what makes the
        # number mean something to whatever reads the markdown. This was a
        # correction to the reference, not a change made to raise the score:
        # the noise that WAS removed (a raw schema.org URL, a leaked footer)
        # was fixed in the extractor.
        reference=(
            "Product details name Compact Keyboard sku KB-88 price 129.99 currency GBP "
            "availability In stock Compact Keyboard 129.99 An eighty-eight key "
            "mechanical keyboard with hot-swappable switches and an aluminium case, "
            "intended for long typing sessions. Hot-swappable switches USB-C "
            "Aluminium case"
        ),
        must_contain=("Compact Keyboard", "129.99", "Hot-swappable", "In stock"),
        must_not_contain=("We use cookies", "schema.org/", "Related articles"),
        min_words=20,
        note=(
            "JSON-LD must be read; price and SKU come from markup, not prose. "
            "Regressions encoded here: schema.org enum URLs were emitted raw, and "
            "the footer leaked in because a short <main> fell through to <body>."
        ),
    )


def _docs() -> Fixture:
    return Fixture(
        name="docs_quickstart",
        page_type=PageType.DOCS,
        html=(
            "<!doctype html><html lang='en'><head><title>Quickstart</title></head>"
            "<body><nav class='sidebar'><ul><li>Guides<ul>"
            "<li><a href='/docs/start'>Quickstart</a></li>"
            "<li><a href='/docs/config'>Configuration</a></li></ul></li></ul></nav>"
            "<main><h1>Quickstart</h1>"
            "<p>Install the package and create a configuration file before "
            "starting the server for the first time.</p>"
            "<pre><code class='language-bash'>pip install example-package</code></pre>"
            "<h2>Configuration</h2>"
            "<p>Every value has a default suitable for local development, so "
            "nothing is required to start.</p>"
            "<pre><code class='language-python'>from example import Client\n\n"
            "client = Client()\nclient.run()</code></pre></main></body></html>"
        ),
        reference=(
            "Quickstart Install the package and create a configuration file before "
            "starting the server for the first time. pip install example-package "
            "Configuration Every value has a default suitable for local development, "
            "so nothing is required to start. from example import Client client = "
            "Client() client.run()"
        ),
        must_contain=("Quickstart", "pip install example-package", "client.run()"),
        min_words=40,
        min_code_blocks=2,
        note="Code blocks must survive as fenced blocks, language preserved.",
    )


def _table() -> Fixture:
    rows = [
        ("Small", "2 vCPU", "4 GB", "£20"),
        ("Medium", "4 vCPU", "8 GB", "£40"),
        ("Large", "8 vCPU", "16 GB", "£80"),
        ("Extra large", "16 vCPU", "32 GB", "£160"),
    ]
    body = "".join(
        f"<tr><td>{a}</td><td>{b}</td><td>{c}</td><td>{d}</td></tr>" for a, b, c, d in rows
    )
    return Fixture(
        name="table_pricing",
        page_type=PageType.TABLE,
        html=(
            "<!doctype html><html lang='en'><head><title>Plans</title></head><body>"
            "<main><table><tr><th>Plan</th><th>CPU</th><th>Memory</th><th>Price</th></tr>"
            f"{body}</table></main></body></html>"
        ),
        reference=("Plan CPU Memory Price " + " ".join(" ".join(row) for row in rows)),
        must_contain=("Extra large", "16 vCPU", "£160"),
        min_words=20,
        min_tables=1,
        note="Structure must survive as a markdown table, not be flattened to prose.",
    )


def _directory() -> Fixture:
    """The alternativeto case: prose-heavy page that reads as a listing."""
    items = "".join(f"<div class='row'><a href='/t/{i}'>Tool {i}</a></div>" for i in range(14))
    prose = "".join(
        f"<p>Section {i} explains a distinct aspect of choosing between the "
        f"tools on this page, in enough specific words to be genuine editorial "
        f"content rather than filler.</p>"
        for i in range(10)
    )
    return Fixture(
        name="directory_with_prose",
        page_type=PageType.LISTING,
        html=(
            "<!doctype html><html lang='en'><head><title>Comparing tools</title></head>"
            f"<body>{_NAV}<aside>{items}</aside><main><h1>Comparing tools</h1>"
            f"{prose}</main>{_FOOTER}</body></html>"
        ),
        reference=(
            "Comparing tools "
            + " ".join(
                f"Section {i} explains a distinct aspect of choosing between the tools "
                f"on this page, in enough specific words to be genuine editorial "
                f"content rather than filler."
                for i in range(10)
            )
        ),
        must_contain=("Comparing tools", "Section 9 explains"),
        must_not_contain=("We use cookies",),
        min_words=150,
        note=(
            "Regression: a directory page classified (correctly) as a listing dropped "
            "from 606 words to 86 because the structured path is weaker here. Correct "
            "routing is no comfort if it produces a worse result."
        ),
    )


def _injected_script() -> Fixture:
    """The Bot Fight Mode case: real content plus an anti-bot script."""
    paragraphs = "".join(
        f"<p>Paragraph {i} of a genuine article that a reader wants, with enough "
        f"distinct wording to count as real editorial content.</p>"
        for i in range(12)
    )
    return Fixture(
        name="article_with_injected_script",
        page_type=PageType.ARTICLE,
        html=(
            "<!doctype html><html lang='en'><head><title>Genuine Article</title></head>"
            f"<body>{_NAV}<article><h1>Genuine Article</h1>{paragraphs}</article>"
            '<script defer src="/cdn-cgi/challenge-platform/scripts/jsd/main.js">'
            f"</script>{_FOOTER}</body></html>"
        ),
        reference=(
            "Genuine Article "
            + " ".join(
                f"Paragraph {i} of a genuine article that a reader wants, with enough "
                f"distinct wording to count as real editorial content."
                for i in range(12)
            )
        ),
        must_contain=("Genuine Article", "Paragraph 11"),
        must_not_contain=("challenge-platform", "cdn-cgi"),
        min_words=100,
        note=(
            "Regression: Cloudflare Bot Fight Mode injects its fingerprint script into "
            "ordinary pages. Treated as a challenge signal it discards real content."
        ),
    )


def all_fixtures() -> list[Fixture]:
    return [
        _article(),
        _news_index(),
        _forum(),
        _product(),
        _docs(),
        _table(),
        _directory(),
        _injected_script(),
    ]


def by_type() -> dict[PageType, list[Fixture]]:
    grouped: dict[PageType, list[Fixture]] = {}
    for fixture in all_fixtures():
        grouped.setdefault(fixture.page_type, []).append(fixture)
    return grouped


__all__ = ["Fixture", "all_fixtures", "by_type"]

_ = field
