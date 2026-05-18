"""SQL-bearing layer for the incremental Stage 1 + Stage 2 pipeline.

All raw SQL lives below this package. Callers should import sub-modules
(e.g. `from thematic_analysis_inc.db import coding, aggregation`) or
the package-level convenience functions re-exported here.
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
from thematic_analysis_inc.db.codebook import (
    CodebookEntry,
    CodebookVersion,
    codebook_to_json_for_version,
    get_codebook_codes,
    get_codebook_version,
    insert_codebook_version,
    latest_codebook_version,
    list_codebook_versions,
)
from thematic_analysis_inc.db.coders import (
    SYSTEM_AGGREGATOR_ID,
    SYSTEM_REVIEWER_ID,
    Coder,
    add_coder,
    get_coder,
    get_coder_by_name,
    list_coders,
)
from thematic_analysis_inc.db.connection import connect, init_db, now
from thematic_analysis_inc.db.documents import (
    EnqueueResult,
    Segment,
    add_document,
    add_quote,
    enqueue_segments,
    find_document_id_by_filename,
    get_segment,
    link_code_quote,
)
from thematic_analysis_inc.db.research_context import (
    clear_research_context,
    get_research_context,
    set_research_context,
)
from thematic_analysis_inc.db.schema import create_schema
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
from thematic_analysis_inc.db.status import (
    Stage2StatusCounts,
    StatusCounts,
    stage2_status_counts,
    status_counts,
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
    # research context
    "set_research_context",
    "get_research_context",
    "clear_research_context",
    # codebook
    "CodebookEntry",
    "CodebookVersion",
    "latest_codebook_version",
    "get_codebook_version",
    "list_codebook_versions",
    "insert_codebook_version",
    "get_codebook_codes",
    "codebook_to_json_for_version",
    # coders
    "Coder",
    "SYSTEM_AGGREGATOR_ID",
    "SYSTEM_REVIEWER_ID",
    "add_coder",
    "get_coder",
    "get_coder_by_name",
    "list_coders",
    # documents/segments
    "Segment",
    "EnqueueResult",
    "add_document",
    "find_document_id_by_filename",
    "enqueue_segments",
    "get_segment",
    "add_quote",
    "link_code_quote",
    # status
    "StatusCounts",
    "Stage2StatusCounts",
    "status_counts",
    "stage2_status_counts",
    # schema
    "create_schema",
    # theme (Stage 2) — backwards-compatible re-exports
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
