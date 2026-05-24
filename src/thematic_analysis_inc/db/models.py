"""SQLModel data/object schema for the thematic-analysis pipeline.

# Design

This module is the single source of truth for the persistent shape of
the pipeline. Every concept the user works with is one of these
classes; relationships are explicit Python attributes (not raw ids).

The conceptual model:

- **Document** — a source file the analyst uploads. Documents are metadata
  only (filename, when added). The raw bytes are NOT stored; the analyst
  is responsible for keeping the source file around if needed.
- **Segment** — a contiguous span of lines from a Document, with its text
  content. Segments are the unit of coding work.
- **Code** — one labelled annotation. The same table holds Stage-A coder
  output (`coder_id ≥ 1`), aggregator output (`coder_id = 0`), and
  reviewer output (`coder_id = -1`). Each code records *which* Codebook
  revision was "used" when it was authored; the matching research
  context is reachable through `code.codebook_used.research_context`,
  so the run is fully reproducible.
- **Quote** — a span of supporting text inside a Segment. Codes link to
  the quotes that justify them through `codes_supporting_quotes`.
- **Codebook** — one revision of the curated codebook. There is
  conceptually a single codebook, but each curated update inserts a new
  row; the latest row is the "current" codebook. A separately-named
  link table (`codebook_code`) holds which Codes belong to which
  revision. Each codebook revision pins the `ResearchContext` revision
  it was authored against — changing the research context creates a
  fresh codebook revision in response, so downstream consumers only
  need to track the codebook version.
- **Coder** — an agent (or human) that produces codes. Two ids are
  reserved system rows: `0` = aggregator, `-1` = reviewer. User-created
  coders start at id 1.
- **ResearchContext** — one revision of the prose + per-role prompts
  driving the LLM agents. Each `add_research_context_and_codebook_revision` inserts a new row
  and triggers a new `Codebook` revision pinned to it; the latest
  codebook's `research_context` is the "current" context.
- **CodingQueueEntry** — one (Segment, Coder) assignment. Status is
  derived from `claimed_at` / `finished_at` / `error` — there is no
  status column.
- **CodesDerived** — provenance graph. Each row is one edge: "this code
  derives from that code", tagged as aggregation (`A`) or review (`R`).
  For review edges, the decision (`A`/`M`/`U` for ADD/MERGE/UPDATE) is
  recorded. SKIP review is unrepresented (no edge).

Conventions:

- FK *columns* end in `_id`; the corresponding relationship attribute
  has the conceptual name (e.g. `code.codebook_used_id` is the int,
  `code.codebook_used` is the related `Codebook` object).
- Tables matching their Python class name in lowercase don't need an
  explicit `__tablename__`. Multi-word classes (`ResearchContext`,
  `CodingQueueEntry`, …) carry an explicit snake_case table name.
- Link tables live at the bottom of this file — they're pure structural
  glue, not concepts. SQLAlchemy resolves `secondary="..."` references
  to them lazily, so the entity classes don't need to know about them.
"""

from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import CheckConstraint, Index, UniqueConstraint
from sqlmodel import Field, Relationship, SQLModel


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


# Single-character enum values used on `codes_derived`. Defined as
# constants so callers don't sprinkle string literals.
DERIVATION_AGGREGATION = "A"
DERIVATION_REVIEW = "R"

DECISION_ADD = "A"
DECISION_MERGE = "M"
DECISION_MERGE_AND_RENAME = "U"

# Reserved `Code.code` label used as a sentinel for "this coder ran and
# produced no codes" (or "this aggregation merged to nothing"). Persisted
# so we can distinguish "not yet processed" from "processed, no result"
# without inspecting the queue separately. The empty string is chosen so
# that no real code label collides with it.
SENTINEL_CODE_LABEL = ""


def is_sentinel_code(code: "Code") -> bool:
    return code.code == SENTINEL_CODE_LABEL


# ===========================================================================
# Entities
# ===========================================================================


class Document(SQLModel, table=True):
    """A source file. Metadata only — no raw bytes are stored.

    Each Document is segmented (offline) into a set of `Segment` rows;
    those are what the pipeline operates on.
    """

    # Counter; populated on insert. Optional only to satisfy SQLModel's
    # autoincrement pattern; after persist it is always set.
    document_id: Optional[int] = Field(default=None, primary_key=True)
    filename: str = Field(index=True)
    created_at: datetime = Field(default_factory=_utcnow)

    segments: list["Segment"] = Relationship(back_populates="document")


class Segment(SQLModel, table=True):
    """A contiguous range of lines inside a Document, with its text.

    `line_from`/`line_to` mark the position in the source; `content` is
    the actual text the coder agent sees; `position` orders segments
    within a document.
    """

    segment_id: Optional[int] = Field(default=None, primary_key=True)
    document_id: int = Field(
        foreign_key="document.document_id", ondelete="CASCADE", index=True
    )
    title: Optional[str] = None
    content: str
    line_from: int
    line_to: int
    position: int

    document: Document = Relationship(back_populates="segments")
    codes: list["Code"] = Relationship(back_populates="segment")
    quotes: list["Quote"] = Relationship(back_populates="segment")


class ResearchContext(SQLModel, table=True):
    """One revision of the research context.

    Each `add_research_context_and_codebook_revision` inserts a new row; the latest is the
    "current" context. The `*_prompt` columns map to
    `thematic_analysis.research_context.AGENT_ROLES`; `NULL` means "no
    tailored prompt for this role — fall back to `description`".

    There are no back-relationships from a ResearchContext to its
    consumers — `codebook.research_context` is the canonical accessor.
    """

    __tablename__ = "research_context"

    research_context_version: Optional[int] = Field(
        default=None, primary_key=True
    )
    description: str = Field(default="")
    coder_prompt: Optional[str] = None
    coding_critic_prompt: Optional[str] = None
    reviewer_prompt: Optional[str] = None
    theme_coder_prompt: Optional[str] = None
    created_at: datetime = Field(default_factory=_utcnow)


class Codebook(SQLModel, table=True):
    """One revision of the curated codebook.

    Conceptually there is just one codebook, but each curated update
    inserts a new row, so we have history. The latest row is "the
    current codebook"; previous rows are predecessors via
    `parent_version`. Membership (which Codes belong to a revision)
    lives in the `codebook_code` link table.

    Each codebook revision pins the `ResearchContext` revision it was
    authored against. When the research context changes a new codebook
    revision is created in response, so everywhere else in the pipeline
    only needs to know the codebook version.
    """

    # The integer is a monotonically-increasing counter. The specific
    # value carries no meaning beyond "later versions have larger ids".
    version: Optional[int] = Field(default=None, primary_key=True)
    parent_version: Optional[int] = Field(
        default=None, foreign_key="codebook.version"
    )
    research_context_version: int = Field(
        foreign_key="research_context.research_context_version",
        index=True,
    )
    created_at: datetime = Field(default_factory=_utcnow)

    parent: Optional["Codebook"] = Relationship(
        back_populates="children",
        sa_relationship_kwargs={
            "remote_side": "Codebook.version",
            "foreign_keys": "Codebook.parent_version",
        },
    )
    children: list["Codebook"] = Relationship(
        back_populates="parent",
        sa_relationship_kwargs={
            "foreign_keys": "Codebook.parent_version",
        },
    )
    codes: list["Code"] = Relationship(
        sa_relationship_kwargs={"secondary": "codebook_code"},
    )
    research_context: ResearchContext = Relationship()


class Coder(SQLModel, table=True):
    """An agent (or human) that produces codes.

    Two reserved system rows are seeded on table creation:

    - `coder_id = 0` — aggregator
    - `coder_id = -1` — reviewer

    User-supplied coders get auto-assigned ids ≥ 1.
    """

    coder_id: int = Field(primary_key=True)
    identity: str
    created_at: datetime = Field(default_factory=_utcnow)


class Code(SQLModel, table=True):
    """One labelled annotation.

    Every code — Stage-A coder code, aggregator code, reviewer code —
    is one row here. The author is identified by `coder.coder_id`:

    - `coder_id ≥ 1` — Stage-A coder output
    - `coder_id = 0`  — aggregator output
    - `coder_id = -1` — reviewer output (the canonical codebook code)

    Every code records which Codebook revision was "used" at authoring
    time; the matching research context is reachable as
    `code.codebook_used.research_context`.
    """

    code_id: Optional[int] = Field(default=None, primary_key=True)
    segment_id: int = Field(foreign_key="segment.segment_id", ondelete="CASCADE", index=True)
    coder_id: int = Field(foreign_key="coder.coder_id", index=True)
    codebook_used_id: int = Field(
        foreign_key="codebook.version", index=True
    )
    code: str
    description: str = Field(default="")
    rationale: str = Field(default="")
    # Raw float32 bytes (np.ndarray.tobytes()). Populated only on
    # reviewer Codes (coder_id == -1) so similarity search can run
    # against the codebook without re-embedding every load.
    embedding: Optional[bytes] = Field(default=None)

    segment: Segment = Relationship(back_populates="codes")
    coder: Coder = Relationship()
    codebook_used: Codebook = Relationship()

    supporting_quotes: list["Quote"] = Relationship(
        back_populates="codes",
        sa_relationship_kwargs={"secondary": "codes_supporting_quotes"},
    )

    # Inbound provenance edges (codes_derived) — "what did this code
    # derive from?" Assignable; the worker writes a Code with its
    # `derivation_sources` list populated and the link rows cascade in.
    derivation_sources: list["CodesDerived"] = Relationship(
        back_populates="new_code",
        sa_relationship_kwargs={
            "primaryjoin": "Code.code_id == CodesDerived.new_code_id",
            "foreign_keys": "CodesDerived.new_code_id",
            "cascade": "all, delete-orphan",
        },
    )


class Quote(SQLModel, table=True):
    """A span of supporting text inside a Segment.

    `quote_id` is a counter. Quotes are linked to the Codes they
    support through `codes_supporting_quotes` (n:m).
    """

    quote_id: Optional[int] = Field(default=None, primary_key=True)
    segment_id: int = Field(foreign_key="segment.segment_id", ondelete="CASCADE", index=True)
    text: str

    segment: Segment = Relationship(back_populates="quotes")
    codes: list[Code] = Relationship(
        back_populates="supporting_quotes",
        sa_relationship_kwargs={"secondary": "codes_supporting_quotes"},
    )


def _normalize_quote_text(text: str) -> str:
    """Key used to detect near-duplicate quotes on the same segment.

    Collapses whitespace, lowercases, and strips punctuation so that
    "I felt supported." and "i felt supported" hash to the same key.
    """
    import re
    import string

    s = text.translate(str.maketrans("", "", string.punctuation))
    s = re.sub(r"\s+", " ", s).strip().lower()
    return s


def create_quote(segment: "Segment", text: str) -> Quote:
    """Return an existing Quote on ``segment`` with the same (or
    near-identical) text, or create a new transient one.

    All Quote creation in the pipeline should go through this so that
    different coders cannot produce duplicate Quote rows for the same
    span. Matching ignores surrounding whitespace, casing, and
    punctuation. The number of quotes per segment is small, so a linear
    scan is fine.
    """
    qt = text.strip()
    key = _normalize_quote_text(qt)
    for existing in segment.quotes or []:
        if _normalize_quote_text(existing.text) == key:
            return existing
    return Quote(text=qt, segment_id=segment.segment_id)


class CodingQueueEntry(SQLModel, table=True):
    """One (Segment, Coder, Codebook revision) assignment.

    The primary key includes the codebook revision so that, when it
    moves forward, the same (segment, coder) can be enqueued again as a
    separate work item. Re-enqueueing at the same revision is a no-op
    (idempotent). The research-context revision used by this work item
    is implied by `codebook_used.research_context`.

    Status is derived from `claimed_at` / `finished_at` / `error` (see
    `status` property below) — there is no status column.
    """

    __tablename__ = "coding_queue"

    segment_id: int = Field(
        foreign_key="segment.segment_id", ondelete="CASCADE", primary_key=True
    )
    coder_id: int = Field(
        foreign_key="coder.coder_id", ondelete="CASCADE", primary_key=True, index=True
    )
    codebook_used_id: int = Field(
        foreign_key="codebook.version", ondelete="CASCADE", primary_key=True
    )
    enqueued_at: datetime = Field(default_factory=_utcnow)
    claimed_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None
    error: Optional[str] = None

    segment: Segment = Relationship()
    coder: Coder = Relationship()
    codebook_used: Codebook = Relationship()

    @property
    def status(self) -> str:
        if self.error is not None:
            return "failed"
        if self.finished_at is not None:
            return "done"
        if self.claimed_at is not None:
            return "running"
        return "pending"


# ===========================================================================
# Stage 2 — themes
#
# A theme groups codebook codes (and representative quotes) into a
# higher-level pattern of meaning. Themes come from one of three sources:
#
#   - SOURCE_JOB         a `ThemeCodingJob` run (LLM theme coder)
#   - SOURCE_AGGREGATOR  output of a (future) automated merge step
#   - SOURCE_MANUAL      hand-curated by the user
#
# Provenance edges (`themes_derived`) capture which themes were folded
# into a newer one. The "current" theme set is then `Theme.deleted IS
# FALSE AND theme_id NOT IN (SELECT source_theme_id FROM themes_derived)`
# — no separate status flag.
# ===========================================================================


SOURCE_JOB = "job"
SOURCE_AGGREGATOR = "aggregator"
SOURCE_MANUAL = "manual"


class ThemeCodingJob(SQLModel, table=True):
    """One LLM theme-coding run over a single codebook revision.

    `prompt` carries the customisable part the user supplies for this
    job — research question, persona, any extra instructions. The agent
    splices it into the base system prompt at run time.
    """

    __tablename__ = "theme_coding_job"

    id: Optional[int] = Field(default=None, primary_key=True)
    codebook_used_id: int = Field(
        foreign_key="codebook.version", index=True
    )
    prompt: str
    created_at: datetime = Field(default_factory=_utcnow)

    codebook_used: Codebook = Relationship()
    themes: list["Theme"] = Relationship(back_populates="job")


class Theme(SQLModel, table=True):
    """One theme. See header comment for the source taxonomy.

    `codebook_used_id` is only populated for job-derived themes, where
    the agent knew exactly which codebook revision it was reading.
    Aggregator and manual themes leave it `NULL` and reach a codebook
    transitively through their `codes` (each `Code` pins its own
    `codebook_used`).
    """

    __tablename__ = "theme"

    theme_id: Optional[int] = Field(default=None, primary_key=True)
    codebook_used_id: Optional[int] = Field(
        default=None, foreign_key="codebook.version", index=True
    )

    source: str = Field(max_length=16, index=True)
    theme_coding_job_id: Optional[int] = Field(
        default=None, foreign_key="theme_coding_job.id", index=True
    )

    title: str
    description: str = Field(default="")
    rationale: str = Field(default="")
    deleted: bool = Field(default=False, index=True)
    created_at: datetime = Field(default_factory=_utcnow)

    job: Optional[ThemeCodingJob] = Relationship(back_populates="themes")
    codebook_used: Optional[Codebook] = Relationship()

    codes: list[Code] = Relationship(
        sa_relationship_kwargs={"secondary": "theme_code"},
    )
    supporting_quotes: list[Quote] = Relationship(
        sa_relationship_kwargs={"secondary": "theme_supporting_quote"},
    )

    derived_from: list["Theme"] = Relationship(
        sa_relationship_kwargs={
            "secondary": "themes_derived",
            "primaryjoin": "Theme.theme_id == ThemesDerived.new_theme_id",
            "secondaryjoin": "Theme.theme_id == ThemesDerived.source_theme_id",
        },
    )

    __table_args__ = (
        CheckConstraint(
            "source IN ('job','aggregator','manual')",
            name="ck_theme_source",
        ),
        CheckConstraint(
            "(source = 'job') = (theme_coding_job_id IS NOT NULL)",
            name="ck_theme_job_consistency",
        ),
    )


# ===========================================================================
# Link tables
#
# Pure structural glue between entities. Kept at the bottom because each
# row here is meaningless on its own — the meaningful concepts are above.
# SQLAlchemy resolves `secondary="<tablename>"` references to these
# lazily, so the entity classes don't need to import them.
# ===========================================================================


class CodebookCode(SQLModel, table=True):
    """n:m link from a Codebook revision to the Codes it contains."""

    __tablename__ = "codebook_code"

    codebook_version: int = Field(
        foreign_key="codebook.version", ondelete="CASCADE", primary_key=True
    )
    code_id: int = Field(
        foreign_key="code.code_id", ondelete="CASCADE", primary_key=True
    )


class CodesSupportingQuotes(SQLModel, table=True):
    """n:m link: which Quotes support which Codes."""

    __tablename__ = "codes_supporting_quotes"

    code_id: int = Field(
        foreign_key="code.code_id", ondelete="CASCADE", primary_key=True
    )
    quote_id: int = Field(
        foreign_key="quote.quote_id", ondelete="CASCADE", primary_key=True
    )


class CodesDerived(SQLModel, table=True):
    """Provenance edge: a new Code was derived from a source Code.

    `derivation_type` is `'A'` (aggregation) or `'R'` (review). For
    review edges, `decision` is one of `'A'`/`'M'`/`'U'` (ADD / MERGE /
    UPDATE). SKIP reviews leave no row.
    """

    __tablename__ = "codes_derived"

    new_code_id: int = Field(
        foreign_key="code.code_id", ondelete="CASCADE", primary_key=True
    )
    source_code_id: int = Field(
        foreign_key="code.code_id", ondelete="CASCADE", primary_key=True
    )
    derivation_type: str = Field(max_length=1)
    decision: Optional[str] = Field(default=None, max_length=1)
    rationale: Optional[str] = None

    new_code: Code = Relationship(
        back_populates="derivation_sources",
        sa_relationship_kwargs={
            "foreign_keys": "[CodesDerived.new_code_id]",
        },
    )
    source_code: Code = Relationship(
        sa_relationship_kwargs={
            "foreign_keys": "[CodesDerived.source_code_id]",
        },
    )

    __table_args__ = (
        CheckConstraint(
            "derivation_type IN ('A','R')", name="ck_derivation_type"
        ),
        CheckConstraint(
            "decision IS NULL OR decision IN ('A','M','U')",
            name="ck_decision",
        ),
        Index(
            "idx_codes_derived_source",
            "source_code_id", "derivation_type",
        ),
    )


class ThemeCode(SQLModel, table=True):
    """n:m link: which codebook codes a theme groups."""

    __tablename__ = "theme_code"

    theme_id: int = Field(
        foreign_key="theme.theme_id", primary_key=True
    )
    code_id: int = Field(
        foreign_key="code.code_id", primary_key=True
    )


class ThemeSupportingQuote(SQLModel, table=True):
    """n:m link: representative quotes the agent picked for a theme.

    Stored separately from the theme's codes (rather than derived from
    them) so the LLM's curatorial choice — which quotes best illustrate
    *this theme* — survives independently of which quotes happen to be
    attached to each constituent code.
    """

    __tablename__ = "theme_supporting_quote"

    theme_id: int = Field(
        foreign_key="theme.theme_id", primary_key=True
    )
    quote_id: int = Field(
        foreign_key="quote.quote_id", primary_key=True
    )


class ThemesDerived(SQLModel, table=True):
    """Provenance edge: `new_theme` was derived from `source_theme`.

    Used by the (future) aggregator to record merges. The "current"
    theme set excludes any theme that appears as a `source_theme_id`.
    """

    __tablename__ = "themes_derived"

    new_theme_id: int = Field(
        foreign_key="theme.theme_id", primary_key=True
    )
    source_theme_id: int = Field(
        foreign_key="theme.theme_id", primary_key=True
    )
    rationale: Optional[str] = None


# ---------------------------------------------------------------------------


__all__ = [
    # Entities
    "Document",
    "Segment",
    "ResearchContext",
    "Codebook",
    "Coder",
    "Code",
    "Quote",
    "CodingQueueEntry",
    "ThemeCodingJob",
    "Theme",
    # Link tables
    "CodebookCode",
    "CodesSupportingQuotes",
    "CodesDerived",
    "ThemeCode",
    "ThemeSupportingQuote",
    "ThemesDerived",
    # Enum-ish constants
    "DERIVATION_AGGREGATION",
    "DERIVATION_REVIEW",
    "DECISION_ADD",
    "DECISION_MERGE",
    "DECISION_MERGE_AND_RENAME",
    "SENTINEL_CODE_LABEL",
    "is_sentinel_code",
    "SOURCE_JOB",
    "SOURCE_AGGREGATOR",
    "SOURCE_MANUAL",
]
