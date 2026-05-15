"""Unit tests for the pure-logic pieces of segmenter_llm (no network)."""

from thematic_analysis_inc.segmenter_llm import (
    LLMSegment,
    _extract_json,
    _merge_short,
    _number_lines,
    _parse_boundaries,
    _slice_segments,
)
from thematic_analysis.loaders import DataSegment


def test_number_lines_format():
    numbered, lines = _number_lines("alpha\nbeta\ngamma")
    assert lines == ["alpha", "beta", "gamma"]
    assert numbered.splitlines()[0] == "0001 | alpha"
    assert numbered.splitlines()[2] == "0003 | gamma"


def test_extract_json_strips_fences():
    raw = "```json\n{\"segments\": [{\"start_line\": 1, \"title\": \"x\"}]}\n```"
    assert _extract_json(raw) == {
        "segments": [{"start_line": 1, "title": "x"}]
    }


def test_extract_json_tolerates_prose():
    raw = 'Here you go:\n{"segments": [{"start_line": 1, "title": "x"}]}\nDone.'
    assert _extract_json(raw)["segments"][0]["start_line"] == 1


def test_parse_boundaries_prepends_one():
    payload = {"segments": [{"start_line": 5, "title": "B"}]}
    out = _parse_boundaries(payload, total_lines=10)
    assert out[0].start_line == 1
    assert out[1].start_line == 5


def test_parse_boundaries_drops_out_of_range_and_dedupes():
    payload = {
        "segments": [
            {"start_line": 1, "title": "A"},
            {"start_line": 1, "title": "A-dup"},
            {"start_line": 99, "title": "OOR"},
            {"start_line": 4, "title": "B"},
        ]
    }
    out = _parse_boundaries(payload, total_lines=10)
    assert [b.start_line for b in out] == [1, 4]


def test_slice_segments_covers_all_lines():
    lines = [f"line{i}" for i in range(1, 11)]
    boundaries = [
        LLMSegment(start_line=1, title="A"),
        LLMSegment(start_line=4, title="B"),
        LLMSegment(start_line=8, title="C"),
    ]
    segs = _slice_segments(boundaries, lines, doc_id="doc")
    assert [s.segment_id for s in segs] == ["doc_l1", "doc_l4", "doc_l8"]
    assert segs[0].text == "line1\nline2\nline3"
    assert segs[1].text == "line4\nline5\nline6\nline7"
    assert segs[2].text == "line8\nline9\nline10"


def test_merge_short_collapses_under_floor_into_neighbor():
    big = " ".join(["w"] * 60)
    small = "tiny"
    segs = [
        DataSegment(segment_id="a", text=small),  # under floor, merge fwd
        DataSegment(segment_id="b", text=big),
        DataSegment(segment_id="c", text=small),  # under floor at tail, merge back
    ]
    merged = _merge_short(segs, min_words=50)
    assert len(merged) == 1
    assert merged[0].segment_id == "a"
    assert "tiny" in merged[0].text and "w w" in merged[0].text


def test_merge_short_keeps_already_large():
    big = " ".join(["w"] * 60)
    segs = [
        DataSegment(segment_id="a", text=big),
        DataSegment(segment_id="b", text=big),
    ]
    merged = _merge_short(segs, min_words=50)
    assert len(merged) == 2
