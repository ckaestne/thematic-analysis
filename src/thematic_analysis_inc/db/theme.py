"""Theme + ThemeCodingJob CRUD, backed by SQLModel.

Themes come from one of three sources (see ``models.SOURCE_*``); the
"current" theme set excludes deleted themes and any theme that has
been folded into a newer one via ``themes_derived``.
"""

from __future__ import annotations

from sqlalchemy.orm import selectinload
from sqlmodel import select

from thematic_analysis_inc.db.connection import session
from thematic_analysis_inc.db.models import (
    SENTINEL_RUNNING_THEME_TITLE,
    SENTINEL_THEME_TITLE,
    SOURCE_JOB,
    SOURCE_MANUAL,
    Code,
    Document,
    Quote,
    Segment,
    Theme,
    ThemeCodingJob,
    ThemesDerived,
)

_SENTINEL_TITLES = (SENTINEL_THEME_TITLE, SENTINEL_RUNNING_THEME_TITLE)


# ---------------------------------------------------------------------------
# ThemeCodingJob
# ---------------------------------------------------------------------------


def add_theme_coding_job(
    codebook_used_id: int, prompt: str
) -> ThemeCodingJob:
    """Insert a new job and return the persisted (detached) row."""
    job = ThemeCodingJob(
        codebook_used_id=codebook_used_id, prompt=prompt,
    )
    with session() as s:
        s.add(job)
        s.commit()
        s.refresh(job)
        s.expunge(job)
        return job


def get_theme_coding_job(job_id: int) -> ThemeCodingJob | None:
    with session() as s:
        job = s.get(ThemeCodingJob, job_id)
        if job is not None:
            s.expunge(job)
        return job


def list_theme_coding_jobs() -> list[ThemeCodingJob]:
    with session() as s:
        rows = list(
            s.exec(
                select(ThemeCodingJob).order_by(ThemeCodingJob.id)
            ).all()
        )
        for r in rows:
            s.expunge(r)
        return rows


def mark_theme_job_running(job: ThemeCodingJob) -> Theme:
    """Insert a running-sentinel Theme row for ``job``. Returns the
    persisted (detached) sentinel so callers can clear it by id.

    Idempotent-ish: if a running sentinel already exists for this job
    the existing one is returned and no new row is written, so a stuck
    sentinel from a crashed previous run still blocks duplicates."""
    with session() as s:
        existing = s.exec(
            select(Theme).where(
                Theme.theme_coding_job_id == job.id,
                Theme.title == SENTINEL_RUNNING_THEME_TITLE,
            )
        ).first()
        if existing is not None:
            s.expunge(existing)
            return existing
        sentinel = Theme(
            codebook_used_id=job.codebook_used_id,
            source=SOURCE_JOB,
            theme_coding_job_id=job.id,
            title=SENTINEL_RUNNING_THEME_TITLE,
        )
        s.add(sentinel)
        s.commit()
        s.refresh(sentinel)
        s.expunge(sentinel)
        return sentinel


def clear_theme_job_running(job_id: int) -> bool:
    """Delete the running-sentinel row(s) for ``job_id``. Returns True
    if anything was removed. Safe to call when none exists."""
    with session() as s:
        rows = list(
            s.exec(
                select(Theme).where(
                    Theme.theme_coding_job_id == job_id,
                    Theme.title == SENTINEL_RUNNING_THEME_TITLE,
                )
            ).all()
        )
        if not rows:
            return False
        for r in rows:
            s.delete(r)
        s.commit()
        return True


def list_unrun_theme_coding_jobs() -> list[ThemeCodingJob]:
    """Jobs that have no `Theme` rows attributed to them — neither real
    nor the sentinel saved by the worker when the agent returned
    nothing. These are the jobs ``create-themes`` needs to run."""
    with session() as s:
        run_ids = select(Theme.theme_coding_job_id).where(
            Theme.theme_coding_job_id.is_not(None)  # type: ignore[union-attr]
        )
        rows = list(
            s.exec(
                select(ThemeCodingJob)
                .where(~ThemeCodingJob.id.in_(run_ids))  # type: ignore[union-attr]
                .order_by(ThemeCodingJob.id)
            ).all()
        )
        for r in rows:
            s.expunge(r)
        return rows


# ---------------------------------------------------------------------------
# Theme
# ---------------------------------------------------------------------------


def save_themes(themes: list[Theme]) -> list[Theme]:
    """Persist a batch of transient themes (with their link rows) in one
    transaction. Each theme must already have its ``source`` set and,
    where applicable, ``theme_coding_job_id``; the worker is responsible
    for that. Returns the persisted (detached) rows."""
    if not themes:
        return []
    with session() as s:
        for t in themes:
            s.add(t)
        s.commit()
        for t in themes:
            s.refresh(t)
            # Touch relationships so callers can read them after expunge.
            _ = list(t.codes)
            _ = list(t.supporting_quotes)
        s.expunge_all()
        return themes


def get_theme(theme_id: int) -> Theme | None:
    """Fetch a theme with its codes + quotes eager-loaded."""
    with session() as s:
        t = s.exec(
            select(Theme)
            .where(Theme.theme_id == theme_id)
            .options(
                selectinload(Theme.codes).selectinload(  # type: ignore[arg-type]
                    Code.supporting_quotes
                ),
                selectinload(Theme.supporting_quotes)  # type: ignore[arg-type]
                .selectinload(Quote.segment)
                .selectinload(Segment.document),
            )
        ).first()
        if t is None:
            return None
        s.expunge_all()
        return t


def list_current_themes() -> list[Theme]:
    """Themes that are not deleted and not folded into a derived theme.
    Sentinel themes (empty title — "the job ran but produced nothing")
    are filtered out; they're worker-only book-keeping, not results."""
    with session() as s:
        src_ids = select(ThemesDerived.source_theme_id)
        rows = list(
            s.exec(
                select(Theme)
                .where(
                    Theme.deleted == False,                  # noqa: E712
                    Theme.title.not_in(_SENTINEL_TITLES),  # type: ignore[union-attr]
                    ~Theme.theme_id.in_(src_ids),
                )
                .order_by(Theme.theme_id)
                .options(
                    selectinload(Theme.codes).selectinload(   # type: ignore[arg-type]
                        Code.supporting_quotes
                    ),
                    selectinload(Theme.supporting_quotes)   # type: ignore[arg-type]
                    .selectinload(Quote.segment)
                    .selectinload(Segment.document),
                )
            ).all()
        )
        s.expunge_all()
        return rows


def list_themes_for_job(
    job_id: int, *, include_sentinel: bool = False
) -> list[Theme]:
    """All themes attributed to a job, ordered by id. By default
    excludes the sentinel row the worker writes when the agent returned
    no themes; pass ``include_sentinel=True`` to see it (e.g. to detect
    "this job already ran")."""
    with session() as s:
        stmt = (
            select(Theme)
            .where(
                Theme.source == SOURCE_JOB,
                Theme.theme_coding_job_id == job_id,
            )
            .order_by(Theme.theme_id)
            .options(
                selectinload(Theme.codes).selectinload(   # type: ignore[arg-type]
                    Code.supporting_quotes
                ),
                selectinload(Theme.supporting_quotes)   # type: ignore[arg-type]
                .selectinload(Quote.segment)
                .selectinload(Segment.document),
            )
        )
        if not include_sentinel:
            stmt = stmt.where(Theme.title.not_in(_SENTINEL_TITLES))  # type: ignore[union-attr]
        rows = list(s.exec(stmt).all())
        s.expunge_all()
        return rows


def mark_theme_deleted(theme_id: int, deleted: bool = True) -> bool:
    """Set the ``deleted`` flag. Returns ``True`` if the row existed."""
    with session() as s:
        t = s.get(Theme, theme_id)
        if t is None:
            return False
        t.deleted = deleted
        s.add(t)
        s.commit()
        return True


def add_manual_theme(
    title: str, description: str = "", rationale: str = ""
) -> Theme:
    """Insert a hand-curated theme (``source='manual'``) with no codes,
    quotes, codebook pin, or job link. Returns the persisted row."""
    t = Theme(
        source=SOURCE_MANUAL,
        title=title,
        description=description,
        rationale=rationale,
    )
    with session() as s:
        s.add(t)
        s.commit()
        s.refresh(t)
        # Touch the collection relationships so the caller can read them
        # after expunge — they're empty for a manual theme but the
        # payload helpers still call len() on them.
        _ = list(t.codes)
        _ = list(t.supporting_quotes)
        s.expunge(t)
        return t


def list_themes_derived_into(theme_id: int) -> list[Theme]:
    """Themes that have ``theme_id`` as one of their ``derived_from``
    sources — i.e. forward lineage. Order: ascending theme id."""
    with session() as s:
        new_ids = list(
            s.exec(
                select(ThemesDerived.new_theme_id).where(
                    ThemesDerived.source_theme_id == theme_id
                )
            ).all()
        )
        if not new_ids:
            return []
        rows = list(
            s.exec(
                select(Theme)
                .where(Theme.theme_id.in_(new_ids))  # type: ignore[union-attr]
                .order_by(Theme.theme_id)
            ).all()
        )
        for r in rows:
            s.expunge(r)
        return rows


def list_themes_derived_from(theme_id: int) -> list[Theme]:
    """Themes that ``theme_id`` was derived from — backward lineage.
    Order: ascending source theme id."""
    with session() as s:
        src_ids = list(
            s.exec(
                select(ThemesDerived.source_theme_id).where(
                    ThemesDerived.new_theme_id == theme_id
                )
            ).all()
        )
        if not src_ids:
            return []
        rows = list(
            s.exec(
                select(Theme)
                .where(Theme.theme_id.in_(src_ids))  # type: ignore[union-attr]
                .order_by(Theme.theme_id)
            ).all()
        )
        for r in rows:
            s.expunge(r)
        return rows
