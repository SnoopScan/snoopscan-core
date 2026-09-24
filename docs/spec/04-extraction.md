# 04 — Extraction Layer

Implements principle P3. This is where the engine earns its keep.

## The problem being solved

Heuristic extractors are built on one assumption: the page contains a single article, and everything else is boilerplate. That assumption holds for news and blogs and breaks everywhere else.

Published benchmark results across page types (2025-2026):

| Page type | Heuristic extractor F1 | Notes |
|---|---|---|
| Article | 0.82 - 0.86 | Solved |
| Documentation | ~0.80 | Mostly solved |
| Forum / thread | ~0.55 | Multi-author, quote nesting, pagination |
| Product page | ~0.68 | Price, availability, variants are not prose |
| Listing / collection | ~0.72 | Many items, no single "main content" |
| Tables | ~0.55 | Structure destroyed by text extraction |

The competitive complaint about existing scraping APIs is not that they fail to fetch. It is that the markdown they return is padded with nav, cookie notices and trending widgets, and that structure is flattened into prose. Both are extraction failures.

**Route by page type. Do not use one extractor for everything.**

---

## 1. Pipeline

```
raw HTML
   │
   ├─▶ 1. Pre-clean          strip script/style/noscript/svg, comments
   │
   ├─▶ 2. Selector filter    apply includeTags / excludeTags to the DOM
   │
   ├─▶ 3. Classify           determine pageType
   │
   ├─▶ 4. Route
   │        ├── article / docs      ──▶ heuristic path
   │        └── forum / product /
   │            listing / table      ──▶ structured path
   │
   ├─▶ 5. Convert            → markdown, preserving structure
   │
   ├─▶ 6. Post-clean         boilerplate sweep, whitespace normalisation
   │
   └─▶ 7. Score              extractionConfidence
```

Steps 2 and 6 are separate on purpose. Selector filtering is caller intent applied to the source DOM; boilerplate sweep is our own cleanup on the result.

---

## 2. Page classification

Cheap, deterministic, runs before extraction. No LLM.

Signals:

| Signal | Indicates |
|---|---|
| `<article>` present, one `<h1>`, high text-to-link ratio | `article` |
| Repeated sibling blocks with matching class structure, each containing author + timestamp | `forum` |
| Schema.org `Product` / `Offer` microdata or JSON-LD | `product` |
| Repeated card structures with links and images, low prose density | `listing` |
| Nav sidebar with nested link tree, code blocks, breadcrumbs | `docs` |
| `<table>` with `<th>` and >3 rows occupying most of the content area | `table`-dominant |

Implementation: rule-based scorer, each rule contributing weight. Highest score wins, ties resolve to `unknown`. `unknown` takes the heuristic path.

JSON-LD and microdata are the strongest signals and cheapest to read. **Parse structured markup first** — many sites hand you exactly what you want in `<script type="application/ld+json">` and everyone ignores it.

Store the result in `pages.page_type`. It also feeds `domain_profiles.typical_page_type`, which lets us skip classification on repeat visits to uniform sites.

---

## 3. Heuristic path (articles, docs, unknown)

**`trafilatura >= 1.8.0`** (Apache-2.0 from that version; earlier releases are GPLv3+ and forbidden by C2 — pin it).

```python
trafilatura.extract(
    html,
    output_format="markdown",
    include_links=True,
    include_tables=True,
    include_images=False,
    favor_precision=True,      # when onlyMainContent
    with_metadata=True,
)
```

`favor_precision=True` when `onlyMainContent` is set; `favor_recall=True` when it is false.

Fallback chain — if trafilatura returns under 50 words on a page whose raw text exceeds 500 words, it has failed. Retry with `favor_recall`, then fall back to a readability-style extractor. Record which path produced the output in `pages.extraction_path`.

trafilatura's metadata extraction (title, author, date, language) is good. Use it rather than writing our own, but cross-check `title` against `<title>` and Open Graph tags and prefer the longest sensible value.

---

## 4. Structured path (forum, product, listing, table)

Where we beat the competition.

### 4a. Structure-preserving conversion

Do not flatten. Convert with structure intact:

- **Tables** → markdown tables, preserving header rows and column alignment. Where a table is too complex for markdown (merged cells, nested), emit it as an HTML block inside the markdown rather than destroying it
- **Code blocks** → fenced blocks with language inferred from class attributes (`language-python`, `highlight-js`)
- **Lists** → real markdown lists, nesting preserved
- **Definition lists** → bold term + indented definition
- **Forum threads** → one section per post, with author and timestamp as a heading, quote nesting preserved as blockquotes

Use `html-to-markdown` (MIT) as the conversion base and extend it. Do not write a converter from scratch — edge cases in HTML are endless.

### 4b. Repeated-block detection (forums, listings)

For `forum` and `listing`, find the repeating unit rather than guessing at "main content":

1. Walk the DOM, hash each element's structural signature (tag path + class set, ignoring text)
2. Find the signature with the highest count where each instance contains meaningful text
3. That is the repeating unit — a post, a product card, a listing row
4. Extract each instance separately, emit as a sequence

This turns a forum from "0.55 F1 mush" into a clean list of posts. It is the single highest-value piece of work in this spec.

Same technique gives listings: each card becomes an entry with title, link, and description.

### 4c. Schema-constrained extraction (`/v1/extract`, `json` format)

When the caller supplies a JSON Schema:

1. Extract candidate content via the paths above
2. Pull structured markup (JSON-LD, microdata, Open Graph) — often answers the schema directly with no model call
3. If the schema is not satisfied, send the cleaned content plus the schema to an LLM with a strict instruction to emit only conforming JSON
4. **Validate the output against the schema before returning.** Non-conforming output is an error, not a passthrough
5. On validation failure, retry once with the validation errors fed back. Then fail

Step 4 is the difference between structured extraction and a model producing plausible text. A price field that returns `"about £40"` instead of `40.0` has failed, and returning it as success poisons whatever consumes it.

Cost control: steps 2 and 3 are ordered deliberately. Structured markup is free. Only call the model when markup does not answer the schema.

---

## 5. Extraction confidence

Every extraction returns `extractionConfidence` in 0-1. Consumers treat < 0.5 as suspect.

Components:

| Factor | Weight | Measure |
|---|---|---|
| Text ratio | 0.25 | extracted chars / raw text chars. Very low or ~1.0 both suspicious |
| Length vs baseline | 0.25 | Distance from `domain_profiles.avg_content_length` in standard deviations |
| Structure retained | 0.20 | Headings, lists, tables present where the source had them |
| Boilerplate absence | 0.20 | No matches against the boilerplate phrase set (cookie/consent/subscribe/nav labels) |
| Metadata completeness | 0.10 | Title, language, and where applicable author/date present |

Ratio near 1.0 means we extracted everything including nav — precision failure. Ratio under 0.05 means we extracted almost nothing — recall failure. Both score low.

This score is also an input to block detection (`05-block-detection.md`): a page that fetched with status 200 but scores 0.1 is very likely a challenge or consent page rather than a genuine extraction failure.

---

## 6. Boilerplate sweep

Post-extraction pass removing what survived:

- Cookie and consent phrases (maintain a phrase list, multilingual)
- "Subscribe to our newsletter", "Sign up for updates" blocks
- Social share button label runs
- "Related articles" / "You may also like" trailing lists, when they appear after the main content ends
- Navigation label runs — three or more consecutive short link-only lines
- Repeated identical lines (a common artifact of failed extraction)

Applied conservatively. Removing genuine content is worse than leaving a stray cookie line. Every rule needs a fixture test proving it does not eat real text.

Maintain the phrase list in a data file, not in code, so it can be extended without a deploy.

---

## 7. Optional model-based extraction

For the hard page types, an SLM-based extractor in the MinerU-HTML style (Apache-2.0, weights included) substantially outperforms heuristics — roughly 0.82 overall ROUGE against ~0.64 for heuristics, with much stronger table and code fidelity.

**Not in v0 or v1.** It needs GPU inference, which changes the deployment profile and cost model entirely.

Design for it now by keeping the extraction router pluggable: adding a model-backed extractor must be registering a new handler for a page type, not a refactor. When volume of hard-type pages justifies GPU cost, it drops in.

**Explicitly excluded:** ReaderLM-v2 is CC-BY-NC-4.0, non-commercial only. Forbidden by C2. Do not benchmark against it, do not use it, do not add it as an optional dependency.

---

## 8. Change tracking

When `changeTracking` format is requested:

1. Normalise extracted text (collapse whitespace, lowercase, strip punctuation runs)
2. SHA-256 → `content_hash`
3. Look up latest `page_versions` row for this `normalized_hash`
4. No prior row → `changeStatus: "new"`, insert
5. Hash differs → `changeStatus: "changed"`, insert, compute diff
6. Hash matches → `changeStatus: "same"`, no insert

Diff modes:
- `git-diff` — unified diff of the two markdown bodies. Cheap, no model call
- `json` — semantic summary of what changed. Requires a model call, priced accordingly

Normalisation before hashing matters. Without it, a timestamp or view counter in the page makes every fetch look changed.

---

## 9. Testing

**Fixture-driven, and this is non-negotiable.** Extraction regressions are silent — output still looks like text.

- Maintain `tests/fixtures/` with saved HTML for at least 10 pages per page type, plus expected extraction output
- Every fixture asserts: word count within tolerance, required content present, forbidden boilerplate absent, structure preserved (table count, code block count, heading count)
- Any extraction change runs the full fixture suite. A drop in any fixture's score fails the build
- Add a fixture every time a real-world extraction is found to be wrong. The suite is the accumulated memory of everything that has broken

Track aggregate F1 across the fixture set as a single headline number in CI output. It should only ever go up.

For the classifier: separate fixture set, assert page type is correctly identified. Misclassification routes to the wrong extractor and is invisible unless tested directly.
