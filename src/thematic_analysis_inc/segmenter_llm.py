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

from thematic_analysis.llm_config import (
    env_max_tokens,
    resolve_model,
    resolve_temperature,
)


# Built-in fallback when neither LLM_MODEL_SEGMENTER nor LLM_MODEL is set.
# Gemini Flash is plenty for boundary picking and cheap; users can override.
_SEGMENTER_DEFAULT_MODEL = "gemini/gemini-2.5-flash-lite"
_SEGMENTER_DEFAULT_TEMPERATURE = 0.0


_SYSTEM_PROMPT = (
    "You segment documents into topical sections for qualitative analysis. "
    "You receive a document with each line prefixed by its line number. "
    "Identify the line numbers where major topic shifts occur. "
    "Each segment must be at least paragraph-length (several sentences); "
    "prefer fewer, larger segments over many small ones. Shorter documents have 1-5 segments, longer documents can have 10 to 50 segments, rarely much more than that. "
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


_LONG_LINE_WORDS = 250
_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+(?=[A-Z\"'(\[])")


def _split_long_lines(
    text: str,
    max_words: int = _LONG_LINE_WORDS,
    chunk_words: int = 50,
) -> str:
    """Split any line over ``max_words`` into sentence-grouped chunks.

    Transcripts and similar dumps sometimes pack the whole body into a
    single physical line, which collapses the line-numbered segmentation
    prompt. Such lines are broken on sentence boundaries and then
    re-merged into ~``chunk_words``-word chunks so the LLM sees a
    sensible number of boundary candidates rather than hundreds of
    single-sentence lines.
    """
    out: list[str] = []
    for ln in text.splitlines():
        if len(ln.split()) <= max_words:
            out.append(ln)
            continue
        sentences = [s for s in _SENTENCE_SPLIT.split(ln) if s]
        buf: list[str] = []
        buf_words = 0
        for s in sentences:
            buf.append(s)
            buf_words += len(s.split())
            if buf_words >= chunk_words:
                out.append(" ".join(buf))
                buf = []
                buf_words = 0
        if buf:
            if out and buf_words < chunk_words:
                out[-1] = out[-1] + " " + " ".join(buf)
            else:
                out.append(" ".join(buf))
    return "\n".join(out)


def _number_lines(text: str) -> tuple[str, list[str]]:
    lines = _split_long_lines(text).splitlines()
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


@dataclass
class TitledSegment:
    segment_id: str
    text: str
    title: str


def _extract_response_text(response) -> str:
    content = getattr(getattr(response, "message", None), "content", None) or []
    parts: list[str] = []
    for part in content:
        if isinstance(part, TextContent):
            parts.append(part.text)
            continue
        text = getattr(part, "text", None)
        if isinstance(text, str):
            parts.append(text)
            continue
        if isinstance(part, str):
            parts.append(part)
    return "".join(parts)


def _slice_segments(
    boundaries: list[LLMSegment], lines: list[str], doc_id: str
) -> list[TitledSegment]:
    out: list[TitledSegment] = []
    for idx, b in enumerate(boundaries):
        start = b.start_line - 1
        end = boundaries[idx + 1].start_line - 1 if idx + 1 < len(boundaries) else len(lines)
        chunk = "\n".join(lines[start:end]).strip()
        if not chunk:
            continue
        seg_id = f"{doc_id}_l{b.start_line}"
        out.append(TitledSegment(segment_id=seg_id, text=chunk, title=b.title))
    return out


def _merge_short(
    segments: list[TitledSegment], min_words: int
) -> list[TitledSegment]:
    if len(segments) <= 1:
        return segments
    merged: list[TitledSegment] = []
    for seg in segments:
        if merged and len(merged[-1].text.split()) < min_words:
            prev = merged[-1]
            merged[-1] = TitledSegment(
                segment_id=prev.segment_id,
                text=prev.text + "\n\n" + seg.text,
                title=prev.title,
            )
        else:
            merged.append(seg)
    while len(merged) >= 2 and len(merged[-1].text.split()) < min_words:
        tail = merged.pop()
        prev = merged[-1]
        merged[-1] = TitledSegment(
            segment_id=prev.segment_id,
            text=prev.text + "\n\n" + tail.text,
            title=prev.title,
        )
    return merged


def segment_by_llm(
    text: str,
    doc_id: str,
    model: str | None = None,
    min_words: int = 50,
) -> list[TitledSegment]:
    """Segment a document via LLM-chosen boundaries.

    Returns titled segments sliced from the original text. The model
    never emits segment text; it only picks boundary line numbers and
    titles.

    Model selection chain: explicit ``model`` arg > ``LLM_MODEL_SEGMENTER``
    > ``LLM_MODEL`` > built-in segmenter default (gemini-2.5-flash-lite).
    Temperature defaults to 0.0; ``LLM_MAX_TOKENS_SEGMENTER`` overrides
    max output tokens when set.
    """
    numbered, lines = _number_lines(text)
    effective_model = model or resolve_model(
        "segmenter", fallback=_SEGMENTER_DEFAULT_MODEL
    )
    temperature = resolve_temperature(
        "segmenter", fallback=_SEGMENTER_DEFAULT_TEMPERATURE
    )
    llm = LLM(usage_id="segmenter", model=effective_model, temperature=temperature)
    max_tokens = env_max_tokens("segmenter")
    if max_tokens is not None:
        llm.max_output_tokens = max_tokens
    last_error: Exception | None = None
    for _attempt in range(3):
        response = llm.completion(
            messages=[
                Message(role="system", content=[TextContent(text=_SYSTEM_PROMPT)]),
                Message(role="user", content=[TextContent(text=numbered)]),
            ],
            response_format=_RESPONSE_SCHEMA,
        )
        raw = _extract_response_text(response)
        try:
            payload = _extract_json(raw)
            boundaries = _parse_boundaries(payload, total_lines=len(lines))
            segments = _slice_segments(boundaries, lines, doc_id)
            return _merge_short(segments, min_words=min_words)
        except Exception as exc:  # noqa: BLE001
            last_error = exc

    detail = str(last_error) if last_error is not None else "unknown parse failure"
    raise ValueError(
        f"failed to parse segmentation JSON after 3 attempts: {detail}"
    )
