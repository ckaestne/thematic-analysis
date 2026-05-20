---
name: agent-design
description: How to design and modify agents in this project (Coder, Aggregator, Reviewer, ThemeCoder, ThemeAggregator). Covers the rule that agents take and return SQLModel objects directly, navigate relationships instead of carrying parallel data, never touch the DB, and expose simple methods keyed on domain inputs. Trigger when adding a new agent role, changing an agent's public method, introducing a new dataclass that mirrors a model row, or wiring an agent into the worker layer.
---

# Designing agents in this project

Agents are the LLM-driven components in `src/thematic_analysis/agents/`. This
note captures the design rules they share. Apply them to every agent, both
when writing a new one and when changing an existing one.

## 1. Speak in model objects, not parallel abstractions

The DB schema in `src/thematic_analysis_inc/db/models.py` (SQLModel) is the
source of truth for what a `Code`, `Quote`, `Segment`, `Codebook`, `Theme`,
etc. looks like. **Do not introduce dataclasses that mirror those rows.**

Bad — a parallel structure that has to be re-mapped back to the model rows
at every boundary:

```python
@dataclass
class MergedCode:
    code: str
    original_codes: list[str]    # back to which Code rows? text match?!
    quotes: list[Quote]
    merge_rationale: str
```

Good — produce real `Code` instances directly. The caller can write them.

```python
def aggregate(self, segment: Segment, codebook: Codebook) -> list[Code]:
    ...
    new_code = Code(
        segment_id=segment.segment_id,
        coder_id=SYSTEM_AGGREGATOR_ID,
        codebook_used_id=codebook.version,
        code=label,
        rationale=rationale,
    )
    new_code.supporting_quotes = [...]                    # existing rows
    new_code.derivation_sources = [CodesDerived(...)]     # link-rows
    return [new_code, ...existing retained Codes...]
```

If you find yourself writing a `*Result` / `Merged*` / `*Output` dataclass
that holds fields already present on a model row, stop and use the model.

## 2. Navigate relationships; never query the DB

Agents read inputs through SQLModel relationship attributes
(`segment.codes`, `code.supporting_quotes`, `code.codebook_used`,
`theme.codes`, …). They **must not** import `session()` or call any
`db_*` helper.

This works because the worker holds an open session around the call and
eager-loads the relevant relationships (`selectinload`) before invoking
the agent. Within that session the agent can traverse the graph freely.

Two important consequences:

- The agent ignores any object identity it can't reach via the segment
  (or whatever root it's given). If it needs a filter, it filters the
  relationship — e.g. `c.coder_id >= 1 and c.codebook_used is codebook`.
- The agent never decides which rows it gets — the worker chooses what to
  eager-load. If an agent needs a new relationship, add the
  `selectinload` in the worker, not a query inside the agent.

## 3. Return model objects; let the worker persist them

Agents return either:

- **New model instances** with `code_id is None` (or equivalent PK
  unset), with relationship attributes wired up to existing rows in the
  same session.
- **Existing model instances** unchanged when the agent's decision is
  "keep as-is" (so no new object is created).

The worker recognises both — `s.add` is a no-op for already-persistent
rows and creates the new ones. Cascades (`cascade="all, delete-orphan"`
on `Code.derivation_sources`, `secondary="codes_supporting_quotes"` for
`Code.supporting_quotes`) take care of the link/edge tables.

**Never create new `Quote` rows from text inside an agent.** Quotes are
authoritative; reuse the `Quote` instances reachable from the input.

## 4. The agent owns its sentinels

Where a sentinel row encodes "this agent produced nothing" (e.g. an
aggregator `Code` with `code == ""`), the *agent* emits it. The worker
should not have to invent a sentinel after the fact. Pattern from
`CodeAggregatorAgent._aggregate_one`:

- No inputs at all → return `[]`. (Nothing to say about this combination.)
- All inputs are sentinels → return `[sentinel]`. (Propagate emptiness.)
- Otherwise → run the LLM and return real results.

The worker writes whatever the agent returns; sentinel handling stays
inside the agent.

## 5. Short-circuit deterministic cases

When the answer doesn't actually need an LLM, skip it. Example: the
aggregator detects "all input codes come from a single coder" and copies
the codes over under the aggregator's `coder_id` with one provenance
edge each — no merge to do, no prompt to write, no token spend.

Reset the debug fields (`last_payload`, `last_user_prompt`,
`last_raw_response`, `last_elapsed`, `last_attempts`) on these paths so
inspectors see "no LLM call".

## 6. Public methods are keyed on the domain inputs

The simplest possible signature is the right one. For each agent, the
public method takes the domain object(s) it operates on and returns
model objects:

```python
class CoderAgent:        def code_segment(self, segment: Segment) -> list[Code]: ...
class CodeAggregatorAgent: def aggregate(self, segment: Segment, codebook: Codebook | None = None) -> list[Code]: ...
class ReviewerAgent:     def review_code(self, code: str, quotes: list[Quote]) -> ReviewResult: ...
                         def process_aggregation_result(self, codes: list[Code]) -> list[ReviewResult]: ...
```

Conventions:

- Optional grouping arg: when an agent can operate over either a slice
  (one codebook) or the whole (all codebooks on the segment), accept a
  default-`None` second arg and dispatch internally with a list
  comprehension. The "all" form is just a `for` over the slices.
- Helper functions that take un-grouped raw inputs (e.g. `coder_codes:
  list[list[Code]]`) are fine *internal* helpers — they leak the
  internal partitioning, so don't expose them.
- The agent stores debug state on `self.last_*` for CLI inspection;
  these are fields, not return values.

## 6a. Identity belongs on the constructor, not in a config blob

If an agent's behaviour or output depends on *who* is running it (the
`Coder` row, a `ThemeCoder`, etc.), accept that row as a constructor
argument. Don't smuggle it in through `AgentConfig.identity` or any
other generic config slot — the agent should hold the actual ORM row,
not a flattened copy of one field.

```python
class CoderAgent(BaseAgent):
    def __init__(
        self,
        coder: Coder,
        codebook: Codebook,
        config: CoderConfig | None = None,
    ):
        ...
```

That way the agent can:

- stamp `coder_id` on every `Code` it returns (see §6b), and
- read other Coder columns (identity, role, future fields) without the
  worker remembering to wire each one through `config=`.

The same rule applies to the `Codebook`: pass the ORM row in the
constructor (the agent is bound to one revision for its lifetime), and
read the codebook's research context off
`codebook.research_context`. Don't accept a separate `research_context`
argument — the research context is pinned to a codebook revision, so
"which codebook?" already answers "which research context?". A
duplicate `research_context=` parameter just creates a way for the two
to disagree.

## 6b. Stamp the assignment keys; don't let the worker rebuild the row

Every `Code` an agent returns must already have its identifying foreign
keys set: `segment_id`, `coder_id`, and `codebook_used_id`. Real codes
and sentinels alike — the sentinel goes through the same construction
path. The worker's persistence helper is then a flat `s.add(c)` per
returned row, not a "rebuild the Code from the assignment" loop:

```python
# in the agent
def _new_code(self, segment: Segment, code: str, description: str) -> Code:
    return Code(
        segment_id=segment.segment_id,
        coder_id=self.coder.coder_id,
        codebook_used_id=self.codebook.version,
        code=code,
        description=description,
    )

# in the persistence helper
for c in codes:
    c.supporting_quotes = [
        s.merge(q) if q.quote_id is not None else q
        for q in (c.supporting_quotes or [])
    ]
    s.add(c)
s.commit()
```

Why this matters: if persistence "fixes up" missing fields, the agent
can return half-built rows (no segment_id, no coder_id) and tests pass
locally — but the agent then doesn't actually own its output. Every
field on the Code is the agent's choice; the worker just writes what
it's handed.

## 7. Model changes follow the agent's needs

If wiring an agent's output as a model graph requires a model tweak,
make the model tweak. The aggregator refactor turned
`Code.derivation_sources` from `viewonly=True` into a writable
relationship with `cascade="all, delete-orphan"` so the agent can write
`new_code.derivation_sources = [CodesDerived(...)]` and have the
provenance edges cascade in. That kind of edit belongs in
`db/models.py` alongside the agent change, not in a workaround.

## 8. Worker contract

Mirror this in the worker that calls the agent:

```python
with session() as s:
    seg = s.exec(
        select(Segment)
        .options(selectinload(Segment.codes).selectinload(Code.supporting_quotes))
        .where(Segment.segment_id == sid)
    ).one()
    codebook = s.get(Codebook, codebook_version)
    result_codes = agent.aggregate(seg, codebook)
    for c in result_codes:
        if c.code_id is None:
            s.add(c)
    s.commit()
```

The worker:
- holds the session,
- eager-loads everything the agent will traverse,
- adds only the new rows the agent returned,
- relies on cascades for link/edge tables.

## Checklist before opening a PR that touches an agent

- [ ] No new `*Result` / `Merged*` dataclass that duplicates a model row.
- [ ] No `session()` / `db_*.something()` calls inside the agent module.
- [ ] No `Quote(text=...)` construction from quote *text* inside an agent.
- [ ] Public method takes domain objects (Segment / Codebook / Code /
      Theme), returns model objects.
- [ ] Identity-bearing ORM rows (`Coder`, `ThemeCoder`, …) are
      constructor arguments, not flattened into an `AgentConfig` field.
- [ ] No separate `research_context=` argument when the agent has a
      `Codebook` — read it off `codebook.research_context`.
- [ ] Returned `Code`s carry `segment_id` / `coder_id` /
      `codebook_used_id` already set; the worker just `s.add`s them.
- [ ] Sentinel cases handled inside the agent.
- [ ] Deterministic cases short-circuit the LLM.
- [ ] Worker uses one session with `selectinload` for everything the
      agent reads; no detach/re-attach gymnastics.
- [ ] If a relationship needed to be writable for the agent's output,
      the model change is in the same PR.
