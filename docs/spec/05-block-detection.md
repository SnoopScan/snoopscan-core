# 05 — Block and Soft-Failure Detection

Implements principle P2: never trust HTTP 200.

## Why this is a first-class component

Modern anti-bot systems rarely return 403 any more. They return 200 with:

- A JavaScript challenge page
- A consent or cookie wall covering the content
- A login gate
- A "verify you are human" interstitial
- **Generated decoy content** — Cloudflare's AI Labyrinth serves plausible-looking generated pages to suspected crawlers, deliberately so that naive scrapers ingest them without noticing

That last one is the dangerous case for this build specifically. Content flows from here into an LLM pipeline and into a database. A scraper that trusts status codes will happily write a generated decoy article into the content store, and nobody notices until something downstream cites it.

Every fetch passes through this validator before being treated as success.

---

## 1. Interface

```python
class Verdict:
    ok: bool
    reason: str | None        # BLOCKED | TARGET_ERROR | ROBOTS_DENIED | SOFT_BLOCK | EMPTY
    signal: str | None        # specific detector that fired
    confidence: float         # 0-1
    details: dict

def validate(result: FetchResult, profile: DomainProfile,
             extraction: ExtractionResult | None) -> Verdict
```

Called twice in the pipeline:

1. **Post-fetch, pre-extraction** — cheap checks on status, headers, raw body
2. **Post-extraction** — checks needing extracted text and confidence score

Splitting them avoids running extraction on an obvious challenge page.

---

## 2. Layer 1 — status and headers

Runs first, cheapest.

**Hard block signals:**

| Signal | Verdict |
|---|---|
| 403 | `BLOCKED` |
| 429 | `BLOCKED`, honour `Retry-After`, raise domain politeness delay |
| 503 with challenge markers | `BLOCKED` |
| `cf-mitigated` header present | `BLOCKED`, signal `cloudflare_mitigated` |
| `x-datadome` / DataDome cookie set | `BLOCKED`, signal `datadome` |
| `server: ddos-guard` and no content | `BLOCKED` |
| Redirect to a known challenge host | `BLOCKED` |

**Not blocks:**

| Signal | Verdict |
|---|---|
| 404, 410 | `TARGET_ERROR` — do not escalate |
| 401 | `TARGET_ERROR` — auth required, out of scope |
| 5xx with no challenge markers, consistent across two tiers | `TARGET_ERROR` |
| 3xx to a normal page | Follow, not a block |

The block-versus-target-error distinction drives escalation. Misclassifying a 404 as a block sends the request up the ladder to tier 3 to fetch a page that does not exist — a hundred times the cost for the same nothing.

---

## 3. Layer 2 — body signatures

Pattern matching against the raw body, before extraction.

Maintain signatures in a **data file** (`detect/signatures.yaml`), not in code. WAF vendors change their pages; updating a YAML file should not need a deploy.

```yaml
- id: cf_challenge
  vendor: cloudflare
  any:
    - "Checking your browser before accessing"
    - "cdn-cgi/challenge-platform"
    - "challenges.cloudflare.com/turnstile"
  confidence: 0.95

- id: cf_js_challenge
  vendor: cloudflare
  all:
    - "_cf_chl_opt"
  confidence: 0.98

- id: datadome
  vendor: datadome
  any:
    - "geo.captcha-delivery.com"
    - "dd_cookie_test"
  confidence: 0.95

- id: perimeterx
  vendor: human
  any:
    - "_pxhd"
    - "px-captcha"
  confidence: 0.9

- id: consent_wall
  vendor: generic
  any:
    - "before you continue to"
    - "we and our partners use cookies"
  requires_low_content: true
  confidence: 0.7
```

`requires_low_content: true` means the signature only counts as a block when the extracted content is also below baseline — cookie notice text appears on plenty of pages that render fine.

When a signature identifies a vendor, write it to `domain_profiles.detected_waf`. Tier selection uses it: DataDome means go straight to Camoufox rather than wasting an attempt on Patchright.

---

## 4. Layer 3 — statistical soft-block detection

The important one. Catches challenges we have no signature for, and decoy content.

Uses `domain_profiles.avg_content_length` and `stdev_content_length`, maintained as a running aggregate over successful fetches of that domain.

**Rule:** if a fetch returns 200 and extracted content length is more than 3 standard deviations below the domain mean, and the domain has at least 20 recorded successes, flag `SOFT_BLOCK` with confidence scaled by the deviation.

Additional statistical checks:

| Check | Fires when | Meaning |
|---|---|---|
| Near-empty | Extracted text < 50 words, raw HTML > 10KB | Content not rendered or gated |
| Link-only | Link count high, prose word count near zero | Nav page served instead of content |
| Uniform-length | Multiple URLs on a domain return near-identical lengths | Same challenge page served for all |
| Title mismatch | Page title matches a known challenge title set | Interstitial |
| Confidence floor | `extractionConfidence` < 0.2 on a 200 | Extraction found nothing meaningful |

The uniform-length check is the strongest decoy detector available cheaply. When a crawl of 50 distinct URLs returns 50 bodies within a few percent of the same length, we are being served a template, not content.

### Cold start

For a domain with fewer than 20 recorded successes there is no baseline. Fall back to absolute thresholds (word count < 50, or `extractionConfidence` < 0.2) and lower confidence in the verdict. Do not block a first-ever fetch on statistics that do not exist yet.

---

## 5. Layer 4 — content plausibility

Cheap heuristics for generated decoy content. None conclusive alone; combine.

- **Entropy** — generated filler tends to sit in a narrow band of lexical diversity. Compute type-token ratio; flag extreme values in either direction
- **Boilerplate density** — decoys often lack the incidental markers of real pages: no outbound links to third parties, no images, no dates, no author
- **Structural monotony** — every paragraph within a few words of the same length
- **Topic drift** — content bears no relation to the URL path or the anchor text that led here. Cheap version: token overlap between URL slug and extracted title/headings
- **Missing expected elements** — a product URL with no price anywhere, a forum URL with no timestamps

Combine into a plausibility score. Below threshold, flag `SOFT_BLOCK` with signal `implausible_content` and **do not write to the content store**. Log it for review.

Set the threshold conservatively at first and tune with real data. False positives here mean discarding good content; measure before tightening.

---

## 6. Honeypot avoidance

Not detection of blocks, but avoidance of traps. Applied at link-extraction time in the crawl frontier.

Skip links that are:

- `display:none`, `visibility:hidden`, or positioned off-screen via inline style or a class known to hide
- Zero-size or 1x1 anchors
- `rel="nofollow"` **combined with** hidden styling (nofollow alone is not a honeypot signal — it is common on legitimate links)
- Matching known trap path patterns for the detected WAF

Cloudflare's AI Labyrinth specifically embeds hidden nofollow links to its generated pages. Following them is how a crawler identifies itself as a bot and gets its whole session downgraded. **This check is cheap and prevents a self-inflicted wound.**

Record skipped links in `frontier` with `status='skipped'` and `skip_reason` rather than silently dropping them, so the behaviour is auditable.

---

## 7. Verdict combination

Multiple detectors may fire. Combine:

```
if any hard signal (status/header) fires:
    → BLOCKED, confidence = max(signal confidences)

elif body signature fires with confidence >= 0.9:
    → BLOCKED

elif body signature fires 0.7-0.9 AND statistical check also fires:
    → BLOCKED

elif statistical check fires with confidence >= 0.8:
    → SOFT_BLOCK

elif plausibility below threshold:
    → SOFT_BLOCK, signal implausible_content

else:
    → ok
```

`SOFT_BLOCK` triggers escalation the same as `BLOCKED` but is recorded distinctly, because soft blocks are where tuning is needed and mixing them into one counter hides the signal.

Always record `block_signals` on the `pages` row even when the verdict is ok. Near-miss data is what lets thresholds be tuned later without re-crawling.

---

## 8. Monitoring and alerting

Detection quality degrades silently. Watch:

| Metric | Alert |
|---|---|
| Block rate by domain | Sudden rise = target deployed new protection |
| Block rate by tier | Rise at tier 1 = fingerprint went stale |
| Soft-block rate overall | Rise = either new protection or a detector regression |
| Verdict mix shift | Served → challenge shift is the earliest warning available |
| Plausibility rejections | Spike = possible decoy campaign, or a broken threshold |
| Escalation depth | Rising average = paying more per page, investigate |

The verdict-mix shift is the leading indicator. A target that starts challenging 20% of requests it previously served is telling you something changed before your success rate visibly drops.

Emit all of these as Prometheus metrics. Dashboard them. Review weekly.

---

## 9. Testing

- **Fixture suite** — saved HTML for each challenge type per vendor, plus known-good pages. Assert correct verdict on each. Add a fixture every time a new challenge type is encountered in the wild
- **Statistical checks** — synthetic: build a domain profile with a known mean and stdev, assert the threshold fires at the right deviation and not before
- **Cold start** — assert a domain with no history does not produce statistical blocks
- **Honeypot** — fixtures with hidden links, assert they are skipped and logged with a reason
- **False positive guard** — a set of legitimately short pages (a stub article, a small docs page) that must **not** be flagged. Detection that flags real content is worse than detection that misses a block

The false-positive set is as important as the true-positive set. It is the thing that stops threshold tuning from quietly breaking the crawler.
