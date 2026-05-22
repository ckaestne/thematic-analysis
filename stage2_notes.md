# Stage 2 Notes

Notes from a conversation reviewing how Phase 2 (theme development) works in
the Thematic-LM paper vs. this repo's implementation.

## How Phase 2 works

### Paper view (§3.1, Fig. 2)

Once Stage 1 has produced a finalized adaptive codebook (codes + supporting
quotes), Stage 2 develops higher-level themes:

1. **Theme coders** — multiple LLM agents, each with a distinct *identity
   perspective*, independently read the **whole codebook** (codes, quotes,
   quote IDs) and synthesize *overarching themes* that capture deeper
   patterns. Each theme has a name, description, and the most relevant
   supporting quotes. The paper compresses the codebook with LLMLingua to
   manage tokens.
2. **Theme aggregator** — merges similar themes across coders into a final,
   deduplicated theme set in JSON.

Stage 2 mirrors Stage 1's *coder → aggregator* structure, but the input is the
codebook (not raw segments) and the output is themes (not codes). There is no
reviewer in Stage 2.

### Implementation in this repo

- `src/thematic_analysis/agents/theme_coder.py` — `ThemeCoder.develop_themes()`
  reads a `DomainCodebook` and returns a `ThemeResult` (themes with
  name/description/codes/quotes).
- `src/thematic_analysis/agents/theme_aggregator.py` — merges multiple
  `ThemeResult`s into one final set, recording `original_themes` and a
  `merge_rationale`.
- `src/thematic_analysis_inc/workers.py` — `theme_code_one()` runs one theme
  coder against a pinned codebook version; a separate aggregation step
  combines all runs.
- `src/thematic_analysis_inc/db/theme.py` — tables `theme_coders`,
  `theme_coder_runs`, `theme_aggregations`, `theme_aggregation_inputs`. Runs
  are keyed on `(theme_coder_id, codebook_version)`.
- CLI: `add-theme-coder`, `list-theme-coders`,
  `generate-themes [--codebook-version N] [--workers K]`, `theme-status`,
  `export-themes`, `export-themes-html`.
- The research context (`set-research-context`) is shared with Stage 2 and
  anchors theme coders to the research question.

End state: a single consolidated `themes.json` (or HTML report) tied to a
specific codebook version.

## What theme coders read from the codebook

### Current implementation

The theme coder gets a compact view — **not** the full codebook. For each
codebook entry, the prompt includes:

- the **code name**
- the **number of quotes** attached to that code
- up to **3 sample quotes**, each truncated to 80 chars

See `_format_codes_section()` in `src/thematic_analysis/agents/theme_coder.py`:

```
- **{code_name}** ({n} quotes): "quote1..." | "quote2..." | "quote3..."
```

Notably absent:
- Code descriptions/definitions are not included (only the code label).
- Most quotes are dropped — only 3 per code, each clipped to 80 chars.
- No quote IDs in the prompt (re-attached afterwards by
  `_collect_quotes_for_codes` once the LLM picks codes per theme).
- No segment/document context beyond the quote text.

There's a `_compress_codebook()` helper that returns just comma-separated code
names, but `develop_themes()` doesn't currently use it.

### What the paper says

§3.1 "Coder Agents" (theme development part):

> "the theme coders are given a complete version of the codebook from the
> coding stage. The codebook is compressed with LLMLingua [22, 23] to reduce
> token costs. The coder agents then analyse the codes and associated quotes
> holistically to identify overarching themes..."

Appendix B (theme coder prompt):

> "You are a coder in the thematic analysis of social media data. Your job is
> to develop themes from codes and their corresponding quotes from the data.
> **When given the codebook in JSON with codes and quotes**, identify themes
> which reflect deeper meanings of the data. For each theme, write one
> sentence to describe what the theme talks about. Keep top {K} most relevant
> quotes; each theme has no more than ten quotes..."

§3.1 "Reviewer Agent" (on codebook contents):

> "This codebook stores previous codes, their corresponding quotes, and quote
> IDs in JSON format. Each entry in the codebook is a code, and its
> associated quotes are nested below each code along with their quote IDs."

So per the paper, the theme coder receives the **complete codebook in JSON**
— codes + all associated quotes (with quote IDs) — then **compressed via
LLMLingua**. Code *descriptions* are not mentioned as part of the codebook
(entries are code labels + nested quotes + quote IDs).

**Difference vs. this repo:** the paper passes the full codebook (all quotes
per code, with IDs, LLMLingua-compressed); this implementation passes only 3
sample quotes per code, truncated to 80 chars, with no IDs and no LLMLingua
compression. Quote IDs are reattached only after the LLM picks codes per
theme.

## What LLMLingua does

LLMLingua is a prompt-compression technique from Microsoft Research (Jiang et
al., EMNLP 2023; LongLLMLingua, ACL 2024). It shrinks a long prompt into a
shorter one that preserves the information an LLM needs, so you pay for fewer
input tokens (and fit more into context) without a big quality loss.

How it works:

1. **Budget allocation** — given a target compression ratio (e.g. 2×–20×), it
   decides how aggressively to compress each part of the prompt
   (instructions, demonstrations, question, context).
2. **Coarse-grained compression** — uses a small auxiliary LM (e.g. LLaMA-7B,
   GPT-2) to score demonstrations / chunks by perplexity and drops the least
   informative ones.
3. **Token-level compression** — the small LM computes the perplexity /
   self-information of each remaining token; tokens that are highly
   predictable (filler words, redundant punctuation, repeated structure) are
   deleted. High-surprisal tokens that actually constrain meaning are kept.
4. Output is shorter, often partly ungrammatical-looking, but large LLMs
   reconstruct meaning well enough to answer as if they had the full prompt.

**LongLLMLingua** extends this for long-context tasks (RAG, long documents):
question-aware re-ranking so the most useful content survives compression and
lands in positions the LLM attends to best.

Why Thematic-LM uses it: the full codebook (every code + every supporting
quote + quote IDs, in JSON) gets large on real datasets. LLMLingua trims the
JSON so the theme coder still sees "the whole codebook" semantically, at a
fraction of the token cost.

## Does LLMLingua work on a whole codebook JSON?

Mostly yes, with caveats — LLMLingua is text-in / text-out, so feeding it a
JSON string works, but JSON is a bit adversarial for it.

**What works well**

- Long natural-language fields (quote text, descriptions, rationales)
  compress nicely. That's where the savings come from.
- LongLLMLingua's question-aware re-ranking helps: pass the theme-coding
  instruction as the "question" and it preserves the most relevant
  quotes/codes.

**What can break**

- **Structural tokens** (`{`, `}`, `"`, `:`, `,`, key names) are highly
  predictable and get deleted. Output is no longer valid JSON. The downstream
  LLM usually still understands it, but you can't `json.loads` the
  compressed prompt.
- **Quote IDs** are exactly what LLMLingua most readily mangles (long, high-
  perplexity, look like noise). If you need cite-back IDs, protect them via
  the "force-tokens" list, or compress only quote-text fields.
- **Compression ratio matters.** Aggressive ratios (10–20×) drop content
  words and degrade theme quality. 2–4× is usually safe for structured data.
- **Per-field compression beats whole-blob compression.** Walk the JSON
  tree, compress only long string leaves (quote text, descriptions), leave
  keys/IDs/short labels alone, re-serialize. Keeps valid JSON, most of the
  savings.

**Practical recipe**

1. Build the full codebook JSON.
2. Either compress the whole blob with `force_tokens` protecting structure
   and IDs, or — recommended — compress only long string fields so structure
   stays intact.
3. Feed the compressed string to the theme coder.

## How important is compression with modern models?

Much less than when the paper was written (late 2024). For most realistic
codebooks today, you can skip compression entirely.

**Context windows are no longer the bottleneck.** Claude Sonnet/Opus 4.x and
GPT-4-class models handle 200K+ tokens; Gemini handles 1–2M. A codebook with
100 codes × 20 quotes × 30 tokens/quote ≈ 60K tokens — fits with room to
spare.

**Cost has dropped and caching helps more.** Input tokens are ~10–30× cheaper
than in 2023. Prompt caching (Anthropic, OpenAI, Gemini) bills a stable
codebook prefix at ~10% on subsequent calls — for parallel theme coders over
the same codebook, caching saves more than LLMLingua would, with zero
quality risk.

**Long-context recall has improved.** "Lost in the middle" effects that
motivated LongLLMLingua's re-ranking are much weaker on current frontier
models. They can actually use a 60K-token codebook holistically — exactly
what thematic analysis needs.

**Compression has real downsides** — mangled quote IDs, lost nuance in
quotes, extra dependency, extra failure mode, paying a small scorer model to
decide what a much smarter model sees. Bad tradeoff for a research pipeline
where theme quality is the point.

**When it would still matter**

- Truly huge corpora: thousands of codes, tens of thousands of quotes
  (500K+ tokens).
- Cheap/local small models with 8–32K windows (the paper's appendix uses
  LLaMA-3-8B).
- Tight per-call budgets at very high call volume.

**Recommendation for this repo:** drop the LLMLingua plan. Send the full
codebook JSON (codes + descriptions + all quotes with IDs) to the theme
coder, and rely on prompt caching for the multi-coder case. If a codebook
ever gets unwieldy, the first lever should be *sampling* quotes per code
(e.g. keep the N most representative) — simpler, preserves valid JSON and
IDs, easier to reason about.
