# 13 — AI Visibility

Measuring whether a brand is cited when someone asks an AI engine a question.

Status: **brief**, not yet a frozen contract. Written 7 Sep 2026 against the
vendors' own current documentation, read with the engine.

## Design position

**This is mostly not a scraping job, and pretending otherwise would build the
wrong thing.** Three of the four engines that matter publish an official API
that returns the citations alongside the answer:

| Engine | How citations come back | Official? |
|---|---|---|
| ChatGPT | Responses API + `web_search` tool → `url_citation` annotations | Yes |
| Perplexity | Sonar API → `search_results`; plus a standalone Search API | Yes |
| Gemini | Grounding with Google Search → `groundingMetadata`, inline `annotations` | Yes |
| Google AI Overviews | **No official API.** SerpApi exposes `engine=google_ai_overview`, reached with a `page_token` off the SERP | No |

So the work splits three ways, and only the third is ours-by-nature:

1. **API orchestration** — ask N prompts against M engines on a schedule, parse
   each vendor's citation shape into one row format.
2. **The AI Overview path** — the only one needing SERP work. Our search ladder
   already talks to Serper, SearXNG, DuckDuckGo and ScrapingDog, but the Serper
   adapter currently reads `organic` only. Whether Serper returns an AI Overview
   block at all is **unverified** — check before assuming, and be ready to add a
   provider that does.
3. **Everything around it** — scheduling, history, diffing, cost accounting.
   `monitor` already does this shape, and it should be reused rather than
   rebuilt.

The engine's real contribution here is 2 and 3. That is worth saying out loud,
because an "AI visibility" feature framed as scraping would send someone to
drive a browser at chatgpt.com for data an API hands over for free.

---

## 1. The honesty rail

**The API surface is not the consumer UI.** Asking the OpenAI Responses API with
`web_search` enabled is not the same act as a person typing into chatgpt.com:
different retrieval, different model routing, different personalisation, no
memory. The numbers correlate. They are not the same number.

Any product built on this must say what it measured, on the page and in the
report. "Cited in 40% of ChatGPT answers" is a claim about a specific API, with
a specific model, on a specific date. Store all three against every row so the
claim can be reconstructed later, and so a model update that moves every score
is visible as a cause rather than read as a client's decline.

Two more that follow from the same rule:

- **Answers are non-deterministic.** The same prompt twice gives different text
  and sometimes different citations. A single run is not a measurement. Sample
  each prompt N times per period (N ≥ 3) and report a rate with its spread, not
  a number with a decimal point.
- **Never report a score the client cannot act on.** A citation share means
  nothing without which URL was cited and what the answer said. The row is the
  product; the percentage is a summary of it.

---

## 2. Scope

**In:** ChatGPT, Perplexity, Gemini, Google AI Overviews. Prompt sets per
client. Citation capture with source URL, position and the surrounding claim.
Share-of-voice against named competitors. History, so movement is visible.

**Out, for now:** Copilot, Grok, Claude, DeepSeek. Add one only when a paying
client asks, and add it as a provider behind the same interface.

**Never:** driving a logged-in consumer UI with somebody's account to fake being
a real user. It breaks the terms of every one of these services, the account
that gets banned is ours, and the data is not better enough to be worth it. If a
competitor's numbers look impossible, this is usually why.

---

## 3. Shape

One endpoint, following the ladder pattern already used by search — a provider
interface with one adapter per engine, so a vendor changing its citation format
is a small edit in one file.

```
POST /v1/visibility
  { "brand": "…", "domain": "…", "competitors": ["…"],
    "prompts": ["…"], "engines": ["chatgpt","perplexity","gemini","ai_overview"],
    "samples": 3 }
```

Returns one row per (prompt × engine × sample): the answer text, every citation
with URL and position, whether the brand's domain appeared, which competitors
appeared, plus the model id and timestamp from §1.

Persist rows and let `monitor` schedule the repeat. Do not build a second
scheduler.

Cost is per API call and it multiplies: prompts × engines × samples × frequency.
50 prompts × 4 engines × 3 samples daily is 600 calls a day per client. Put the
real figure in `cost` on every response the way the other endpoints do, and set
a per-key ceiling before this is exposed to anyone.

---

## 4. Build order

1. **Perplexity first.** Cleanest citation format, cheapest calls, fastest way to
   prove the row shape is right.
2. **ChatGPT and Gemini.** Both official, both a straight adapter once the row
   shape is settled.
3. **AI Overviews last** — it is the only one with real unknowns. Verify Serper
   before writing anything against it.
4. **Then the scheduling and history layer**, reusing `monitor`.

Stop after 1 and look at the rows before building 2. If the row shape is wrong,
it is wrong for all four.

---

## 5. Open questions

- Does Serper return an AI Overview block, or do we need SerpApi as a second
  provider for that one path? **Unverified — this is the first thing to check.**
- Sampling: is N=3 enough to separate a real change from ordinary variance? Run
  one prompt 20 times and measure the spread before fixing N.
- Retention: answer text is bulky. How long before rows are pruned to citations
  only?
- Does this belong in the MCP server too, or is it a REST-only endpoint?
