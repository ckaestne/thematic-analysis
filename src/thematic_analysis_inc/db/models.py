"""SQLModel data/object schema for the thematic-analysis pipeline.

This module defines every table as a typed SQLModel class with explicit
relationships, so callers work with `Document`/`Segment`/`Code`/`Quote`
objects (and navigate `code.segment`, `code.coder`, `code.codebook_version`,
`code.research_context_version`, `code.supporting_quotes` …) rather than
ids and raw rows.

It deliberately mirrors `data-schema.md`. The differences from the raw
DDL there are:

- FK *columns* use the SQLAlchemy convention `<rel>_id` (e.g.
  `codebook_version_id` rather than the historical `codebook_version`).
  The relationship attribute (e.g. `code.codebook_version`) returns the
  object. The column rename only affects the persisted column name and
  the ORM API; the conceptual schema is unchanged.
- Timestamps are `datetime` on the Python side; SQLAlchemy persists them
  as DATETIME columns and round-trips them through real `datetime`
  objects (no more ISO-string handling at call sites).

This module *defines* the schema. Wiring it through the rest of the
codebase (engine setup, repositories, workers, web layer) is a separate
step — for now the existing raw-SQL `db/` modules continue to operate.
"""

from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import CheckConstraint, Index, UniqueConstraint
from sqlmodel import Field, Relationship, SQLModel


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


# ---------------------------------------------------------------------------
# Link tables (no extra columns)
# ---------------------------------------------------------------------------


class CodesSupportingQuotes(SQLModel, table=True):
    """n:m link between `codes` and `quotes`."""

    __tablename__ = "codes_supporting_quotes"

    code_id: int = Field(
        foreign_key="codes.code_id", primary_key=True
    )
    quote_id: int = Field(
        foreign_key="quotes.quote_id", primary_key=True
    )


class CodebookMembership(SQLModel, table=True):
    """Which reviewer codes belong to which codebook version.

    Table name is the historical `codebook`; class name disambiguates it
    from the conceptual "codebook" idea.
    """

    __tablename__ = "codebook"

    codebook_version_id: int = Field(
        foreign_key="codebook_versions.version", primary_key=True
    )
    code_id: int = Field(
        foreign_key="codes.code_id", primary_key=True
    )

    __table_args__ = (
        Index("idx_codebook_version", "codebook_version_id"),
    )


class ThemeAggregationInput(SQLModel, table=True):
    """Which theme_coder_runs contributed to a theme_aggregation."""

    __tablename__ = "theme_aggregation_inputs"

    theme_aggregation_id: int = Field(
        foreign_key="theme_aggregations.id", primary_key=True
    )
    theme_coder_run_id: int = Field(
        foreign_key="theme_coder_runs.id", primary_key=True
    )


# Single-character enum values. Defined as constants so callers don't
# sprinkle string literals across the codebase.
DERIVATION_AGGREGATION = "A"
DERIVATION_REVIEW = "R"

DECISION_ADD = "A"
DECISION_MERGE = "M"
DECISION_UPDATE = "U"


# ---------------------------------------------------------------------------
# Core entities
# ---------------------------------------------------------------------------


class ResearchContext(SQLModel, table=True):
    """Append-only history of research-context revisions.

    Each `set_research_context` insert creates a new row with a fresh
    `research_context_version`. The five `*_prompt` columns map to
    `thematic_analysis.research_context.AGENT_ROLES`; NULL means "no
    tailored prompt — fall back to `description`".
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
    theme_aggregator_prompt: Optional[str] = None
    created_at: datetime = Field(default_factory=_utcnow)

    codes: list["Code"] = Relationship(back_populates="research_context")
    coding_queue_entries: list["CodingQueueEntry"] = Relationship(
        back_populates="research_context"
    )
    theme_coder_runs: list["ThemeCoderRun"] = Relationship(
        back_populates="research_context"
    )
    theme_aggregations: list["ThemeAggregation"] = Relationship(
        back_populates="research_context"
    )


class CodebookVersion(SQLModel, table=True):
    """One revision of the curated codebook.

    Membership (which reviewer codes are part of this version) lives in
    `codebook`; this table only carries metadata + the parent link.
    """

    __tablename__ = "codebook_versions"

    version: Optional[int] = Field(default=None, primary_key=True)
    parent_version_id: Optional[int] = Field(
        default=None, foreign_key="codebook_versions.version"
    )
    created_at: datetime = Field(default_factory=_utcnow)
    created_by: str

    parent: Optional["CodebookVersion"] = Relationship(
        sa_relationship_kwargs={
            "remote_side": "CodebookVersion.version",
            "foreign_keys": "CodebookVersion.parent_version_id",
        }
    )

    codes_authored: list["Code"] = Relationship(
        back_populates="codebook_version"
    )
    coding_queue_entries: list["CodingQueueEntry"] = Relationship(
        back_populates="codebook_version"
    )
    member_codes: list["Code"] = Relationship(
        link_model=CodebookMembership,
        sa_relationship_kwargs={
            "primaryjoin": (
                "CodebookVersion.version == "
                "CodebookMembership.codebook_version_id"
            ),
            "secondaryjoin": "CodebookMembership.code_id == Code.code_id",
        },
    )


class Coder(SQLModel, table=True):
    """A coder. Two rows are reserved for the system:

    - `coder_id = 0` — aggregator
    - `coder_id = -1` — reviewer

    User-supplied coders get auto-assigned ids ≥ 1 (the helper computes
    `MAX(coder_id) + 1`).
    """

    __tablename__ = "coders"

    coder_id: int = Field(primary_key=True)
    identity: str
    created_at: datetime = Field(default_factory=_utcnow)

    codes: list["Code"] = Relationship(back_populates="coder")
    coding_queue_entries: list["CodingQueueEntry"] = Relationship(
        back_populates="coder"
    )


class Document(SQLModel, table=True):
    __tablename__ = "documents"

    document_id: Optional[int] = Field(default=None, primary_key=True)
    filename: str = Field(index=True)
    content: bytes
    created_at: datetime = Field(default_factory=_utcnow)

    segments: list["Segment"] = Relationship(back_populates="document")


class Segment(SQLModel, table=True):
    __tablename__ = "segments"

    segment_id: Optional[int] = Field(default=None, primary_key=True)
    document_id: Optional[int] = Field(
        default=None,
        foreign_key="documents.document_id",
        index=True,
    )
    content: str
    line_from: Optional[int] = None
    line_to: Optional[int] = None
    position: Optional[int] = None

    document: Optional[Document] = Relationship(back_populates="segments")
    codes: list["Code"] = Relationship(back_populates="segment")
    quotes: list["Quote"] = Relationship(back_populates="segment")
    coding_queue_entries: list["CodingQueueEntry"] = Relationship(
        back_populates="segment"
    )


class Code(SQLModel, table=True):
    """Every code (Stage-A coder code, aggregator code, reviewer code).

    The author is identified by `coder.coder_id`:

    - `coder_id ≥ 1` — Stage-A coder output
    - `coder_id = 0` — aggregator output
    - `coder_id = -1` — reviewer output (the canonical codebook code)

    Reviewer codes have `segment_id = NULL` (a reviewer code isn't tied
    to a single segment).
    """

    __tablename__ = "codes"

    code_id: Optional[int] = Field(default=None, primary_key=True)

    segment_id: Optional[int] = Field(
        default=None, foreign_key="segments.segment_id", index=True
    )
    coder_id: int = Field(foreign_key="coders.coder_id", index=True)
    codebook_version_id: Optional[int] = Field(
        default=None,
        foreign_key="codebook_versions.version",
        index=True,
    )
    research_context_version_id: Optional[int] = Field(
        default=None,
        foreign_key="research_context.research_context_version",
    )

    code: str
    description: str = Field(default="")
    rationale: str = Field(default="")

    segment: Optional[Segment] = Relationship(back_populates="codes")
    coder: Coder = Relationship(back_populates="codes")
    codebook_version: Optional[CodebookVersion] = Relationship(
        back_populates="codes_authored"
    )
    research_context: Optional[ResearchContext] = Relationship(
        back_populates="codes"
    )

    supporting_quotes: list["Quote"] = Relationship(
        back_populates="codes",
        link_model=CodesSupportingQuotes,
    )

    member_of_codebook_versions: list[CodebookVersion] = Relationship(
        back_populates="member_codes",
        link_model=CodebookMembership,
        sa_relationship_kwargs={
            "primaryjoin": "Code.code_id == CodebookMembership.code_id",
            "secondaryjoin": (
                "CodebookMembership.codebook_version_id == "
                "CodebookVersion.version"
            ),
        },
    )

    # Provenance edges (`codes_derived`). Both directions are exposed so
    # callers can ask "what did this code derive from?" and "what did
    # this code lead to?" without writing SQL.
    derivation_sources: list["CodesDerived"] = Relationship(
        sa_relationship_kwargs={
            "primaryjoin": "Code.code_id == CodesDerived.new_code_id",
            "foreign_keys": "CodesDerived.new_code_id",
            "viewonly": True,
        }
    )
    derivation_targets: list["CodesDerived"] = Relationship(
        sa_relationship_kwargs={
            "primaryjoin": "Code.code_id == CodesDerived.source_code_id",
            "foreign_keys": "CodesDerived.source_code_id",
            "viewonly": True,
        }
    )


class Quote(SQLModel, table=True):
    __tablename__ = "quotes"

    quote_id: Optional[int] = Field(default=None, primary_key=True)
    segment_id: Optional[int] = Field(
        default=None, foreign_key="segments.segment_id", index=True
    )
    text: str

    segment: Optional[Segment] = Relationship(back_populates="quotes")
    codes: list[Code] = Relationship(
        back_populates="supporting_quotes",
        link_model=CodesSupportingQuotes,
    )


class CodesDerived(SQLModel, table=True):
    """Provenance edge: `new_code` was derived from `source_code`.

    Carries which kind of derivation (aggregation vs review), and — for
    review edges — the decision and rationale. Defined after `Code` so
    its type-annotated relationships resolve directly.
    """

    __tablename__ = "codes_derived"

    new_code_id: int = Field(
        foreign_key="codes.code_id", primary_key=True
    )
    source_code_id: int = Field(
        foreign_key="codes.code_id", primary_key=True
    )
    # 'A' = aggregation, 'R' = review.
    derivation_type: str = Field(max_length=1)
    # NULL for aggregation edges; 'A' (ADD) / 'M' (MERGE) / 'U' (UPDATE)
    # for review edges. SKIP is unrepresented.
    decision: Optional[str] = Field(default=None, max_length=1)
    rationale: Optional[str] = None

    new_code: Code = Relationship(
        sa_relationship_kwargs={
            "foreign_keys": "[CodesDerived.new_code_id]",
        }
    )
    source_code: Code = Relationship(
        sa_relationship_kwargs={
            "foreign_keys": "[CodesDerived.source_code_id]",
        }
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
            "idx_codes_derived_source", "source_code_id", "derivation_type"
        ),
    )


class CodingQueueEntry(SQLModel, table=True):
    """One row per (segment, real coder) assignment.

    Replaces the old `coder_runs` table. Status is *derived*:

    | condition                                  | status     |
    |--------------------------------------------|------------|
    | `error IS NOT NULL`                        | failed     |
    | `finished_at IS NOT NULL`                  | done       |
    | `claimed_at IS NOT NULL`                   | running    |
    | otherwise                                  | pending    |
    """

    __tablename__ = "coding_queue"

    segment_id: int = Field(
        foreign_key="segments.segment_id", primary_key=True
    )
    coder_id: int = Field(
        foreign_key="coders.coder_id", primary_key=True, index=True
    )
    codebook_version_id: int = Field(
        foreign_key="codebook_versions.version"
    )
    research_context_version_id: Optional[int] = Field(
        default=None,
        foreign_key="research_context.research_context_version",
    )
    claimed_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None
    error: Optional[str] = None

    segment: Segment = Relationship(back_populates="coding_queue_entries")
    coder: Coder = Relationship(back_populates="coding_queue_entries")
    codebook_version: CodebookVersion = Relationship(
        back_populates="coding_queue_entries"
    )
    research_context: Optional[ResearchContext] = Relationship(
        back_populates="coding_queue_entries"
    )

    @property
    def status(self) -> str:
        if self.error is not None:
            return "failed"
        if self.finished_at is not None:
            return "done"
        if self.claimed_at is not None:
            return "running"
        return "pending"


# ---------------------------------------------------------------------------
# Stage 2 — theme tables
# ---------------------------------------------------------------------------


class ThemeCoder(SQLModel, table=True):
    __tablename__ = "theme_coders"

    theme_coder_id: str = Field(primary_key=True)
    identity: str
    created_at: datetime = Field(default_factory=_utcnow)

    runs: list["ThemeCoderRun"] = Relationship(
        back_populates="theme_coder"
    )


class ThemeCoderRun(SQLModel, table=True):
    """One run per (theme_coder, codebook_version)."""

    __tablename__ = "theme_coder_runs"

    id: Optional[int] = Field(default=None, primary_key=True)
    theme_coder_id: str = Field(foreign_key="theme_coders.theme_coder_id")
    codebook_version_id: int = Field(
        foreign_key="codebook_versions.version",
        index=True,
    )
    research_context_version_id: Optional[int] = Field(
        default=None,
        foreign_key="research_context.research_context_version",
    )
    status: str = Field(default="running", index=True)
    claimed_at: datetime = Field(default_factory=_utcnow)
    finished_at: Optional[datetime] = None
    result_json: Optional[str] = None
    raw_response: Optional[str] = None
    error: Optional[str] = None

    theme_coder: ThemeCoder = Relationship(back_populates="runs")
    codebook_version: CodebookVersion = Relationship()
    research_context: Optional[ResearchContext] = Relationship(
        back_populates="theme_coder_runs"
    )

    contributed_to: list["ThemeAggregation"] = Relationship(
        back_populates="input_runs",
        link_model=ThemeAggregationInput,
    )

    __table_args__ = (
        UniqueConstraint(
            "theme_coder_id",
            "codebook_version_id",
            name="uq_theme_coder_runs",
        ),
    )


class ThemeAggregation(SQLModel, table=True):
    """One aggregation per codebook_version (UNIQUE)."""

    __tablename__ = "theme_aggregations"

    id: Optional[int] = Field(default=None, primary_key=True)
    codebook_version_id: int = Field(
        foreign_key="codebook_versions.version",
        unique=True,
    )
    research_context_version_id: Optional[int] = Field(
        default=None,
        foreign_key="research_context.research_context_version",
    )
    status: str = Field(default="running", index=True)
    created_at: datetime = Field(default_factory=_utcnow)
    finished_at: Optional[datetime] = None
    result_json: Optional[str] = None
    error: Optional[str] = None

    codebook_version: CodebookVersion = Relationship()
    research_context: Optional[ResearchContext] = Relationship(
        back_populates="theme_aggregations"
    )

    input_runs: list[ThemeCoderRun] = Relationship(
        back_populates="contributed_to",
        link_model=ThemeAggregationInput,
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


__all__ = [
    # Link tables
    "CodesSupportingQuotes",
    "CodebookMembership",
    "CodesDerived",
    "ThemeAggregationInput",
    # Core entities
    "ResearchContext",
    "CodebookVersion",
    "Coder",
    "Document",
    "Segment",
    "Code",
    "Quote",
    "CodingQueueEntry",
    # Stage 2
    "ThemeCoder",
    "ThemeCoderRun",
    "ThemeAggregation",
    # Enum-ish constants
    "DERIVATION_AGGREGATION",
    "DERIVATION_REVIEW",
    "DECISION_ADD",
    "DECISION_MERGE",
    "DECISION_UPDATE",
]
