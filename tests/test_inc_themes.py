"""End-to-end tests for Stage 2 (ThemeCodingJob + ThemeCoderAgent worker).

The agent's LLM call is replaced with a stub factory so these stay
offline and deterministic. They exercise: schema, the
``run_theme_coding_job`` worker, and the ``current themes`` query
(deleted / derived filtering).
"""

from __future__ import annotations

from pathlib import Path

from thematic_analysis_inc import workers
from thematic_analysis_inc import db as store
from thematic_analysis_inc.db.connection import session
from thematic_analysis_inc.db.models import (
    Code,
    Quote,
    SOURCE_AGGREGATOR,
    SOURCE_JOB,
    SOURCE_MANUAL,
    Theme,
    ThemesDerived,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _seed_codebook_with_codes(n_codes: int = 3) -> int:
    """Add a coder, a document with one segment, and `n_codes` reviewer
    codes pinned to the latest codebook revision so the codebook has
    real `code_id`s for the agent to reference. Returns the codebook
    version used."""
    store.add_coder("alice")
    doc = store.add_document("doc.md")
    rows = [(None, "text 0", 0, 0, 0)]
    segs = store.enqueue_segments(doc, rows)
    seg_id = segs[0].segment_id
    version = store.latest_codebook().version

    with session() as s:
        for i in range(n_codes):
            q = Quote(segment_id=seg_id, text=f"quote {i}")
            s.add(q)
            s.commit()
            s.refresh(q)
            c = Code(
                segment_id=seg_id,
                coder_id=-1,                       # reviewer
                codebook_used_id=version,
                code=f"code-{i}",
                description=f"desc {i}",
            )
            c.supporting_quotes = [q]
            s.add(c)
            s.commit()
            s.refresh(c)
            store.add_code_to_codebook(
                codebook=store.get_codebook(version), code=c,
            )
    return version


def _stub_agent_factory(themes_payload):
    """Return a factory that builds an agent whose develop_themes()
    returns transient Themes built from `themes_payload`.

    Each entry: {"title": str, "code_indices": [int, ...], "quote_indices": [int, ...]}
    Indices refer to ``codebook.codes`` and the supporting_quotes of the
    picked codes, respectively.
    """

    class _StubAgent:
        def develop_themes(self, codebook, prompt):
            out: list[Theme] = []
            for spec in themes_payload:
                codes = [codebook.codes[i] for i in spec["code_indices"]]
                allowed_q = {
                    q.quote_id: q
                    for c in codes
                    for q in (c.supporting_quotes or [])
                }
                quotes = [
                    allowed_q[qid] for qid in spec.get("quote_ids", [])
                    if qid in allowed_q
                ]
                out.append(
                    Theme(
                        codebook_used_id=codebook.version,
                        source="",                  # worker sets it
                        title=spec["title"],
                        description=spec.get("description", ""),
                        rationale=spec.get("rationale", ""),
                        codes=codes,
                        supporting_quotes=quotes,
                    )
                )
            return out

    return lambda: _StubAgent()


# ---------------------------------------------------------------------------
# Schema sanity
# ---------------------------------------------------------------------------


def test_stage2_tables_created(tmp_path: Path) -> None:
    store.init_db(tmp_path / "x.sqlite")
    from sqlalchemy import inspect

    from thematic_analysis_inc.db.connection import get_engine

    eng = get_engine()
    tabs = set(inspect(eng).get_table_names())
    assert {
        "theme_coding_job", "theme", "theme_code",
        "theme_supporting_quote", "themes_derived",
    } <= tabs


# ---------------------------------------------------------------------------
# Worker: run_theme_coding_job
# ---------------------------------------------------------------------------


def test_run_theme_coding_job_persists_themes_with_provenance(
    tmp_path: Path,
) -> None:
    store.init_db(tmp_path / "x.sqlite")
    version = _seed_codebook_with_codes(n_codes=3)

    job = store.add_theme_coding_job(
        codebook_used_id=version,
        prompt="research focus: X. persona: phenomenologist.",
    )
    assert job.id is not None
    assert job.codebook_used_id == version

    payload = [
        {
            "title": "Theme A",
            "description": "d", "rationale": "r",
            "code_indices": [0, 1],
            "quote_ids": [],
        },
        {
            "title": "Theme B",
            "description": "", "rationale": "",
            "code_indices": [1, 2],
            "quote_ids": [],
        },
    ]
    themes = workers.run_theme_coding_job(
        job, agent_factory=_stub_agent_factory(payload),
    )
    assert [t.title for t in themes] == ["Theme A", "Theme B"]
    for t in themes:
        assert t.source == SOURCE_JOB
        assert t.theme_coding_job_id == job.id
        assert t.codebook_used_id == version
        assert t.theme_id is not None
        assert len(t.codes) == 2


def test_run_theme_coding_job_missing_codebook_raises(tmp_path: Path) -> None:
    store.init_db(tmp_path / "x.sqlite")
    import pytest

    job = store.add_theme_coding_job(
        codebook_used_id=999, prompt="…",   # 999 doesn't exist
    )
    with pytest.raises(ValueError, match="codebook version 999"):
        workers.run_theme_coding_job(
            job, agent_factory=_stub_agent_factory([]),
        )


def test_run_theme_coding_job_empty_result_persists_nothing(
    tmp_path: Path,
) -> None:
    store.init_db(tmp_path / "x.sqlite")
    version = _seed_codebook_with_codes(n_codes=2)
    job = store.add_theme_coding_job(codebook_used_id=version, prompt="x")
    themes = workers.run_theme_coding_job(
        job, agent_factory=_stub_agent_factory([]),
    )
    assert themes == []
    assert store.list_themes_for_job(job.id) == []


# ---------------------------------------------------------------------------
# Current-themes query: deleted + derived filtering
# ---------------------------------------------------------------------------


def test_list_current_themes_excludes_deleted_and_derivation_sources(
    tmp_path: Path,
) -> None:
    store.init_db(tmp_path / "x.sqlite")
    version = _seed_codebook_with_codes(n_codes=3)

    # Seed three themes: one job theme A, one job theme B, and one
    # aggregator theme C derived from both A and B. C should appear in
    # "current"; A and B should drop out (as derivation sources).
    job = store.add_theme_coding_job(codebook_used_id=version, prompt="…")
    initial = workers.run_theme_coding_job(
        job,
        agent_factory=_stub_agent_factory(
            [
                {
                    "title": "A", "description": "", "rationale": "",
                    "code_indices": [0, 1], "quote_ids": [],
                },
                {
                    "title": "B", "description": "", "rationale": "",
                    "code_indices": [1, 2], "quote_ids": [],
                },
            ]
        ),
    )
    a, b = initial

    # Manually create an aggregator theme C derived from A and B.
    c = Theme(
        codebook_used_id=None,
        source=SOURCE_AGGREGATOR,
        title="C",
        codes=list(a.codes) + [x for x in b.codes if x not in a.codes],
    )
    with session() as s:
        s.add(c)
        s.commit()
        s.refresh(c)
        s.add(ThemesDerived(new_theme_id=c.theme_id, source_theme_id=a.theme_id))
        s.add(ThemesDerived(new_theme_id=c.theme_id, source_theme_id=b.theme_id))
        s.commit()

    current = store.list_current_themes()
    assert [t.title for t in current] == ["C"]

    # And a manual deleted theme stays excluded.
    manual = Theme(source=SOURCE_MANUAL, title="manual-dead", deleted=True)
    with session() as s:
        s.add(manual)
        s.commit()
    current = store.list_current_themes()
    assert [t.title for t in current] == ["C"]


def test_mark_theme_deleted_round_trip(tmp_path: Path) -> None:
    store.init_db(tmp_path / "x.sqlite")
    version = _seed_codebook_with_codes(n_codes=2)
    job = store.add_theme_coding_job(codebook_used_id=version, prompt="…")
    [t] = workers.run_theme_coding_job(
        job,
        agent_factory=_stub_agent_factory(
            [{
                "title": "T", "description": "", "rationale": "",
                "code_indices": [0, 1], "quote_ids": [],
            }]
        ),
    )
    assert [x.title for x in store.list_current_themes()] == ["T"]
    assert store.mark_theme_deleted(t.theme_id) is True
    assert store.list_current_themes() == []
    assert store.mark_theme_deleted(999_999) is False
