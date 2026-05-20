"""DB layer for the Stage 1 + Stage 2 pipeline.

Stage-1 tables are owned by SQLModel (see :mod:`db.models`); all
Stage-1 helpers take and return SQLModel objects. Stage-2 (``theme_*``)
tables still use a raw-SQL DAL in :mod:`db.theme`.
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
    schema,
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
)
from thematic_analysis_inc.db.documents import (
    add_document,
    add_quote,
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
from thematic_analysis_inc.db.schema import create_schema
from thematic_analysis_inc.db.status import (
    Stage2StatusCounts,
    StatusCounts,
    stage2_status_counts,
    status_counts,
)
from thematic_analysis_inc.db.theme import (
    ThemeCoder,
    add_theme_coder,
    all_theme_coders_done,
    get_theme_coder,
    latest_theme_aggregation,
    list_theme_coders,
    record_theme_aggregation_failure,
    record_theme_coder_failure,
    record_theme_coder_result,
    remove_theme_coder,
    reset_unfinished_theme_aggregations,
    reset_unfinished_theme_coder_runs,
    start_theme_aggregation,
    start_theme_coder_run,
    theme_coders_to_run,
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
    "schema",
    "status",
    "theme",
    # connection
    "connect",
    "init_db",
    "now",
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
    "enqueue_segments",
    "get_segment",
    "get_segment_with_codes",
    "list_segments",
    "add_quote",
    "link_code_quote",
    # status
    "StatusCounts",
    "Stage2StatusCounts",
    "status_counts",
    "stage2_status_counts",
    # schema
    "create_schema",
    # theme (Stage 2)
    "ThemeCoder",
    "add_theme_coder",
    "remove_theme_coder",
    "get_theme_coder",
    "list_theme_coders",
    "theme_coders_to_run",
    "start_theme_coder_run",
    "record_theme_coder_result",
    "record_theme_coder_failure",
    "reset_unfinished_theme_coder_runs",
    "all_theme_coders_done",
    "start_theme_aggregation",
    "record_theme_aggregation_failure",
    "reset_unfinished_theme_aggregations",
    "latest_theme_aggregation",
]
