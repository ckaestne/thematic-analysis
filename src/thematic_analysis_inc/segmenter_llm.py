"""LLM-driven topical segmentation.

Numbers each input line, asks the model to pick boundary line numbers, then
slices the *original* text by those numbers. The model never emits segment
text, so there's no risk of rewording or omission; the worst it can do is
choose poor boundaries, which the merge-pass below partially compensates
for by collapsing under-length segments into their neighbour.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

from openhands.sdk import LLM, Message, TextContent

from thematic_analysis.loaders import DataSegment


_SYSTEM_PROMPT = (
    "You segment documents into topical sections for qualitative analysis. "
    "You receive a document with each line prefixed by its line number. "
    "Identify the line numbers where major topic shifts occur. "
    "Each segment must be at least paragraph-length (several sentences); "
    "prefer fewer, larger segments over many small ones. "
    "A boundary marks a genuine shift in what is being discussed, not a "
    "minor turn within the same topic. "
    "The first segment must start at line 1. "
    "For each segment, give its start_line and a 3-8 word title describing its topic."
)


_RESPONSE_SCHEMA = {
    "type": "json_schema",
    "json_schema": {
        "name": "segmentation",
        "strict": True,
        "schema": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "segments": {
                    "type": "array",
                    "minItems": 1,
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {
                            "start_line": {"type": "integer", "minimum": 1},
                            "title": {"type": "string", "minLength": 1},
                        },
                        "required": ["start_line", "title"],
                    },
                }
            },
            "required": ["segments"],
        },
    },
}


@dataclass
class LLMSegment:
    start_line: int
    title: str


def _number_lines(text: str) -> tuple[str, list[str]]:
    lines = text.splitlines()
    width = max(4, len(str(len(lines))))
    numbered = "\n".join(f"{i + 1:0{width}d} | {ln}" for i, ln in enumerate(lines))
    return numbered, lines


def _extract_json(raw: str) -> dict:
    raw = raw.strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw, flags=re.MULTILINE)
    start = raw.find("{")
    end = raw.rfind("}")
    if start == -1 or end == -1:
        raise ValueError(f"no JSON object in response: {raw[:200]!r}")
    return json.loads(raw[start : end + 1])


def _parse_boundaries(payload: dict, total_lines: int) -> list[LLMSegment]:
    segs_raw = payload.get("segments")
    if not isinstance(segs_raw, list) or not segs_raw:
        raise ValueError(f"missing or empty 'segments' in payload: {payload!r}")

    parsed: list[LLMSegment] = []
    for entry in segs_raw:
        start = int(entry["start_line"])
        if start < 1 or start > total_lines:
            continue
        title = str(entry.get("title", "")).strip() or "(untitled)"
        parsed.append(LLMSegment(start_line=start, title=title))

    parsed.sort(key=lambda s: s.start_line)
    seen: set[int] = set()
    deduped: list[LLMSegment] = []
    for s in parsed:
        if s.start_line in seen:
            continue
        seen.add(s.start_line)
        deduped.append(s)

    if not deduped:
        raise ValueError("no valid boundaries after parsing")
    if deduped[0].start_line != 1:
        deduped.insert(0, LLMSegment(start_line=1, title=deduped[0].title))
    return deduped


def _slice_segments(
    boundaries: list[LLMSegment], lines: list[str], doc_id: str
) -> list[DataSegment]:
    out: list[DataSegment] = []
    for idx, b in enumerate(boundaries):
        start = b.start_line - 1
        end = boundaries[idx + 1].start_line - 1 if idx + 1 < len(boundaries) else len(lines)
        chunk = "\n".join(lines[start:end]).strip()
        if not chunk:
            continue
        seg_id = f"{doc_id}_l{b.start_line}"
        out.append(DataSegment(segment_id=seg_id, text=chunk))
    return out


def _merge_short(segments: list[DataSegment], min_words: int) -> list[DataSegment]:
    if len(segments) <= 1:
        return segments
    merged: list[DataSegment] = []
    for seg in segments:
        if merged and len(merged[-1].text.split()) < min_words:
            prev = merged[-1]
            merged[-1] = DataSegment(
                segment_id=prev.segment_id,
                text=prev.text + "\n\n" + seg.text,
            )
        else:
            merged.append(seg)
    while len(merged) >= 2 and len(merged[-1].text.split()) < min_words:
        tail = merged.pop()
        prev = merged[-1]
        merged[-1] = DataSegment(
            segment_id=prev.segment_id,
            text=prev.text + "\n\n" + tail.text,
        )
    return merged


def segment_by_llm(
    text: str,
    doc_id: str,
    model: str = "gemini/gemini-2.5-flash-lite",
    min_words: int = 50,
) -> tuple[list[DataSegment], list[LLMSegment]]:
    """Segment a document via LLM-chosen boundaries.

    Returns (segments, raw_boundaries) so callers can show the model's
    proposed titles alongside the resulting slices.
    """
    numbered, lines = _number_lines(text)
    llm = LLM(usage_id="segmenter", model=model, temperature=0.0)
    response = llm.completion(
        messages=[
            Message(role="system", content=[TextContent(text=_SYSTEM_PROMPT)]),
            Message(role="user", content=[TextContent(text=numbered)]),
        ],
        response_format=_RESPONSE_SCHEMA,
    )
    raw = "".join(
        part.text for part in response.message.content if isinstance(part, TextContent)
    )
    payload = _extract_json(raw)
    boundaries = _parse_boundaries(payload, total_lines=len(lines))
    segments = _slice_segments(boundaries, lines, doc_id)
    segments = _merge_short(segments, min_words=min_words)
    return segments, boundaries
