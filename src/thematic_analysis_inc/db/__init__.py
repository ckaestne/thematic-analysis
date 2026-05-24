"""DB layer for the Stage 1 pipeline.

Stage-1 tables are owned by SQLModel (see :mod:`db.models`); all
Stage-1 helpers take and return SQLModel objects.
"""

from __future__ import annotations

from thematic_analysis_inc.db import (
    aggregation,
    cascades,
    codebook,
    coders,
    coding,
    documents,
    research_context,
    review,
    status,
    theme,
)
from thematic_analysis_inc.db.aggregation import (
    load_segment_and_codebook_for_aggregation,
)
from thematic_analysis_inc.db.codebook import (
    add_code_to_codebook,
    codebook_to_json_for_version,
    copy_codebook_membership,
    get_codebook,
    get_codebook_with_codes_and_research_context,
    insert_codebook_version,
    latest_codebook,
    list_codebooks,
    live_codes_for_batch,
    materialize_codebook_revision,
)
from thematic_analysis_inc.db.coders import (
    SYSTEM_AGGREGATOR_ID,
    SYSTEM_REVIEWER_ID,
    add_coder,
    get_coder,
    list_coders,
)
from thematic_analysis_inc.db.connection import (
    connect,
    init_db,
    now,
    session,
)
from thematic_analysis_inc.db.documents import (
    add_document,
    add_quote,
    count_segments,
    enqueue_segments,
    find_document_by_filename,
    get_segment,
    get_segment_with_codes,
    link_code_quote,
    list_documents,
    list_segments,
)
from thematic_analysis_inc.db.models import (
    Code,
    Codebook,
    CodebookCode,
    Coder,
    CodesDerived,
    CodesSupportingQuotes,
    CodingQueueEntry,
    Document,
    Quote,
    ResearchContext,
    Segment,
)
from thematic_analysis_inc.db.research_context import (
    RC_AGENT_ROLES,
    add_research_context_and_codebook_revision,
    clear_research_context,
    get_research_context,
    latest_research_context_version,
    list_research_context_versions,
    to_domain as research_context_to_domain,
)
from thematic_analysis_inc.db.status import (
    StatusCounts,
    status_counts,
)
from thematic_analysis_inc.db.theme import (
    add_theme_coding_job,
    get_theme,
    get_theme_coding_job,
    list_current_themes,
    list_theme_coding_jobs,
    list_themes_for_job,
    mark_theme_deleted,
    save_themes,
)


__all__ = [
    # submodules
    "aggregation",
    "cascades",
    "codebook",
    "coders",
    "coding",
    "documents",
    "research_context",
    "review",
    "status",
    "themes",
    # connection
    "connect",
    "init_db",
    "now",
    "session",
    # models
    "Code",
    "Codebook",
    "CodebookCode",
    "Coder",
    "CodesDerived",
    "CodesSupportingQuotes",
    "CodingQueueEntry",
    "Document",
    "Quote",
    "ResearchContext",
    "Segment",
    # research context
    "RC_AGENT_ROLES",
    "add_research_context_and_codebook_revision",
    "get_research_context",
    "latest_research_context_version",
    "list_research_context_versions",
    "clear_research_context",
    "research_context_to_domain",
    # aggregation
    "load_segment_and_codebook_for_aggregation",
    # codebook
    "latest_codebook",
    "get_codebook",
    "get_codebook_with_codes_and_research_context",
    "list_codebooks",
    "insert_codebook_version",
    "codebook_to_json_for_version",
    "add_code_to_codebook",
    "copy_codebook_membership",
    "live_codes_for_batch",
    "materialize_codebook_revision",
    # coders
    "SYSTEM_AGGREGATOR_ID",
    "SYSTEM_REVIEWER_ID",
    "add_coder",
    "get_coder",
    "list_coders",
    # documents / segments
    "add_document",
    "find_document_by_filename",
    "list_documents",
    "count_segments",
    "enqueue_segments",
    "get_segment",
    "get_segment_with_codes",
    "list_segments",
    "add_quote",
    "link_code_quote",
    # status
    "StatusCounts",
    "status_counts",
    # themes
    "add_theme_coding_job",
    "get_theme_coding_job",
    "list_theme_coding_jobs",
    "save_themes",
    "get_theme",
    "list_current_themes",
    "list_themes_for_job",
    "mark_theme_deleted",
]
