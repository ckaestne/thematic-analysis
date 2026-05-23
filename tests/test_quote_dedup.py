"""Regression tests for Quote deduplication.

Two related guarantees:

1. ``save_codes_and_finish_assignment`` must dedup transient Quotes
   against existing ``Quote`` rows on the segment, AND must not leave
   orphan duplicates behind. Earlier versions did ``s.add(c)`` before
   resolving ``c.supporting_quotes`` against existing rows; the
   save-update cascade on the many-to-many pulled the agent's transient
   Quote objects into the session, and the later reassignment swapped
   them out of the relationship but they still flushed as orphan rows.

2. ``claim_next_assignment`` must not hand out two assignments on the
   same segment to two workers concurrently. Two coders coding the same
   segment in parallel would each load segment.quotes from before the
   other's commit and produce duplicate Quote rows for the same span.
"""

from __future__ import annotations

from pathlib import Path

from thematic_analysis_inc import db as store
from thematic_analysis_inc.db.connection import session
from thematic_analysis_inc.db.models import Code, Quote


def _seed(tmp_path: Path):
    store.init_db(tmp_path / "x.sqlite")
    coder = store.add_coder("a")
    doc = store.add_document("doc.md")
    segs = store.enqueue_segments(doc, [(None, "the cat sat on the mat", 0, 0, 0)])
    return coder, segs[0]


def _stub_code(label: str, quote_text: str, *, segment, coder, codebook):
    c = Code(
        segment_id=segment.segment_id,
        coder_id=coder.coder_id,
        codebook_used_id=codebook.version,
        code=label,
        description="",
    )
    c.supporting_quotes = [Quote(text=quote_text, segment_id=segment.segment_id)]
    return c


def _all_quotes_for_segment(segment_id: int) -> list[Quote]:
    with session() as s:
        from sqlmodel import select

        rows = list(s.exec(select(Quote).where(Quote.segment_id == segment_id)).all())
        for r in rows:
            s.expunge(r)
        return rows


def test_save_codes_dedupes_transient_quote_against_existing(tmp_path: Path) -> None:
    coder, seg = _seed(tmp_path)
    cb = store.latest_codebook()

    # First coder run inserts a Quote.
    store.coding.enqueue_pairs(((seg.segment_id, coder.coder_id),), codebook_version=cb.version)
    a1 = store.coding.claim_next_assignment(coder)
    assert a1 is not None
    store.coding.save_codes_and_finish_assignment(
        a1, [_stub_code("c1", "the cat sat", segment=seg, coder=coder, codebook=cb)]
    )

    quotes_after_first = _all_quotes_for_segment(seg.segment_id)
    assert len(quotes_after_first) == 1
    existing_qid = quotes_after_first[0].quote_id

    # A second run on a new codebook revision claims a fresh assignment for
    # the same segment. The agent re-emits a transient Quote with the same
    # text (normalized). Save must reuse the existing row, not insert a
    # second one — and must not leave an orphan transient Quote behind.
    cb2 = store.insert_codebook_version(parent=cb)
    store.coding.enqueue_pairs(
        ((seg.segment_id, coder.coder_id),), codebook_version=cb2.version
    )
    a2 = store.coding.claim_next_assignment(coder)
    assert a2 is not None
    fresh_seg = store.get_segment(seg.segment_id)
    store.coding.save_codes_and_finish_assignment(
        a2,
        [_stub_code("c2", "  The Cat Sat.", segment=fresh_seg, coder=coder, codebook=cb2)],
    )

    quotes_after_second = _all_quotes_for_segment(seg.segment_id)
    assert len(quotes_after_second) == 1, (
        f"expected 1 quote, got {[(q.quote_id, q.text) for q in quotes_after_second]}"
    )
    assert quotes_after_second[0].quote_id == existing_qid


def test_save_codes_no_orphan_when_two_codes_share_a_new_span(tmp_path: Path) -> None:
    """Two codes in the same assignment, both supported by the same new
    span — must produce exactly one Quote row, not two."""
    coder, seg = _seed(tmp_path)
    cb = store.latest_codebook()

    store.coding.enqueue_pairs(((seg.segment_id, coder.coder_id),), codebook_version=cb.version)
    assignment = store.coding.claim_next_assignment(coder)
    assert assignment is not None

    codes = [
        _stub_code("c1", "the cat sat", segment=seg, coder=coder, codebook=cb),
        _stub_code("c2", "the cat sat", segment=seg, coder=coder, codebook=cb),
    ]
    store.coding.save_codes_and_finish_assignment(assignment, codes)

    quotes = _all_quotes_for_segment(seg.segment_id)
    assert len(quotes) == 1, [(q.quote_id, q.text) for q in quotes]


def test_claim_next_assignment_serializes_by_segment(tmp_path: Path) -> None:
    """If one assignment on segment S is claimed (not finished), another
    pending assignment on S for a different coder must not be claimable."""
    store.init_db(tmp_path / "x.sqlite")
    coder_a = store.add_coder("a")
    coder_b = store.add_coder("b")
    doc = store.add_document("doc.md")
    seg = store.enqueue_segments(doc, [(None, "hello world", 0, 0, 0)])[0]
    cb = store.latest_codebook()

    store.coding.enqueue_pairs(
        ((seg.segment_id, coder_a.coder_id), (seg.segment_id, coder_b.coder_id)),
        codebook_version=cb.version,
    )

    first = store.coding.claim_next_assignment()
    assert first is not None
    # While `first` is in-flight, nothing else on this segment is claimable.
    second = store.coding.claim_next_assignment()
    assert second is None, (
        f"per-segment serialization broken: claimed second={second!r}"
    )

    # After finishing `first`, the other coder's assignment becomes claimable.
    store.coding.save_codes_and_finish_assignment(
        first,
        [
            _stub_code(
                "c",
                "hello",
                segment=store.get_segment(seg.segment_id),
                coder=store.get_coder(first.coder_id),
                codebook=cb,
            )
        ],
    )
    third = store.coding.claim_next_assignment()
    assert third is not None
    assert third.segment_id == seg.segment_id
    assert third.coder_id != first.coder_id


def test_claim_next_assignment_parallel_across_segments(tmp_path: Path) -> None:
    """Per-segment lock must NOT prevent claims on different segments."""
    store.init_db(tmp_path / "x.sqlite")
    coder = store.add_coder("a")
    doc = store.add_document("doc.md")
    segs = store.enqueue_segments(
        doc,
        [(None, f"seg {i}", 0, 0, i) for i in range(3)],
    )
    cb = store.latest_codebook()
    store.coding.enqueue_pairs(
        ((s.segment_id, coder.coder_id) for s in segs),
        codebook_version=cb.version,
    )

    claimed = []
    for _ in range(3):
        a = store.coding.claim_next_assignment(coder)
        assert a is not None
        claimed.append(a.segment_id)
    assert sorted(claimed) == sorted(s.segment_id for s in segs)
