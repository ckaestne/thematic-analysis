"""Code Aggregator agent for merging codes from multiple coders."""

import json
import time
from dataclasses import dataclass

from thematic_analysis.agents.base import AgentConfig, BaseAgent
from thematic_analysis.agents.json_utils import extract_response_json
from thematic_analysis_inc.db.coders import SYSTEM_AGGREGATOR_ID
from thematic_analysis_inc.db.models import (
    DERIVATION_AGGREGATION,
    SENTINEL_CODE_LABEL,
    Code,
    Codebook,
    CodesDerived,
    Segment,
    is_sentinel_code,
)


@dataclass
class AggregatorConfig(AgentConfig):
    """Configuration for the Code Aggregator agent."""

    max_quotes_per_code: int = 10


AGGREGATOR_SYSTEM_PROMPT = """\
You are an aggregator coder in the thematic analysis of social media data. \
Your job is to take the codes and corresponding quotes produced by several \
independent coders, merge codes from different coders that capture the same \
underlying concept, and retain codes that capture different concepts.

## Input
The user message is a JSON object with coders and their codes.
Each code has an `id`, a `label`, and a list of
  `quotes` (the verbatim quote texts that support it).

## Rules
1. Merge codes when they describe the same phenomenon and cover all the
   quotes from the original codes, even if from different angles or at
   different levels of specificity. Give the merged code a clear,
   representative label.
2. Keep codes separate when merging them would lose an analytical
   distinction.
3. Codes from the same coder already describe distinct concepts and
   do not need to be merged.
4. Every input code id must appear in exactly one of `merge_groups`
   (inside `original_code_ids`) or `retain_code_ids`.
5. Refer to codes by their integer `id` from the input. Do not echo
   labels or quote text back.

## Output
Respond with a single JSON object:
- `merge_groups`: list of merges. Each merge has:
  - `merged_code`: a new representative label (string)
  - `original_code_ids`: list of input code ids being merged (length >= 2)
  - `rationale`: a brief explanation
- `retain_code_ids`: list of input code ids kept as-is (not merged)
"""

AGGREGATOR_RESPONSE_SCHEMA = {
    "type": "json_schema",
    "json_schema": {
        "name": "code_aggregation",
        "strict": True,
        "schema": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "merge_groups": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {
                            "merged_code": {"type": "string"},
                            "original_code_ids": {
                                "type": "array",
                                "items": {"type": "integer"},
                            },
                            "rationale": {"type": "string"},
                        },
                        "required": [
                            "merged_code",
                            "original_code_ids",
                            "rationale",
                        ],
                    },
                },
                "retain_code_ids": {
                    "type": "array",
                    "items": {"type": "integer"},
                },
            },
            "required": ["merge_groups", "retain_code_ids"],
        },
    },
}


class CodeAggregatorAgent(BaseAgent):
    """Agent that merges and organizes codes from multiple coders.

    The Code Aggregator takes code assignments from multiple coder agents,
    identifies codes with similar meanings, merges them when appropriate,
    and emits new Code objects (with quotes and provenance edges wired
    up in memory) for the worker to persist.
    """

    def __init__(self, config: AggregatorConfig | None = None):
        super().__init__(config or AggregatorConfig())
        self.aggregator_config: AggregatorConfig = self.config  # type: ignore
        # Debug fields populated by the most recent per-codebook
        # aggregation so callers (e.g. the `test-aggregate` CLI) can
        # inspect what the agent did without re-implementing prompt
        # building or the LLM call.
        self.last_payload: dict | None = None
        self.last_system_prompt: str = ""
        self.last_user_prompt: str = ""
        self.last_raw_response: str = ""
        self.last_elapsed: float = 0.0
        self.last_attempts: int = 0

    def get_system_prompt(self) -> str:
        return AGGREGATOR_SYSTEM_PROMPT

    def aggregate(
        self,
        segment: Segment,
        codebook: Codebook | None = None,
    ) -> list[Code]:
        """Aggregate codes for one segment.

        With ``codebook``, aggregate only that codebook revision. Without,
        group the segment's codes by ``codebook_used`` and aggregate each
        revision in turn (concatenated result).
        """
        if codebook is not None:
            return self._aggregate_one(segment, codebook)

        # SQLModel disables __hash__, so a set over Codebook objects raises
        # TypeError; key the dedup on the FK int instead (one entry per
        # codebook revision, identity preserved via the value).
        codebooks = {c.codebook_used_id: c.codebook_used for c in segment.codes}
        return [
            resulting_code
            for codebook in codebooks.values()
            for resulting_code in self._aggregate_one(segment, codebook)
        ]

    def _aggregate_one(
        self, segment: Segment, codebook: Codebook
    ) -> list[Code]:
        inputs = [
            c
            for c in segment.codes
            if c.coder_id >= 1 and c.codebook_used is codebook
        ]
        if not inputs:
            return []
        if all(is_sentinel_code(c) for c in inputs):
            return [self._sentinel(segment, codebook)]

        grouped: dict[int, list[Code]] = {}
        for c in inputs:
            if is_sentinel_code(c):
                continue
            grouped.setdefault(c.coder_id, []).append(c)
        coder_codes = [grouped[k] for k in sorted(grouped)]

        # Single-coder short-circuit: there is nothing to merge across
        # coders, so copy each code over under the aggregator's coder_id
        # without an LLM call. Quotes are reused; one provenance edge
        # links each new code back to its source.
        if len(coder_codes) == 1:
            self.last_payload = None
            self.last_user_prompt = ""
            self.last_raw_response = ""
            self.last_elapsed = 0.0
            self.last_attempts = 0
            return [
                self._copy_as_aggregator(segment, codebook, src)
                for src in coder_codes[0]
            ]

        payload, code_index = self._build_prompt_payload(coder_codes)
        system_prompt = self.get_system_prompt()
        user_prompt = json.dumps(payload, indent=2)
        self.last_payload = payload
        self.last_system_prompt = system_prompt
        self.last_user_prompt = user_prompt

        data, response, elapsed, attempts = self._call_with_retries(
            system_prompt, user_prompt, code_index
        )
        self.last_raw_response = response
        self.last_elapsed = elapsed
        self.last_attempts = attempts

        if data is None:
            # Parse failure: fall back to retaining every input code as-is.
            return [c for codes in coder_codes for c in codes]

        return self._build_codes(segment, codebook, data, code_index)

    def _build_prompt_payload(
        self, coder_codes: list[list[Code]]
    ) -> tuple[dict, dict[int, Code]]:
        code_index: dict[int, Code] = {}

        def _code_payload(code: Code) -> dict:
            cid = len(code_index) + 1
            code_index[cid] = code
            quotes = [q.text for q in (code.supporting_quotes or [])]
            return {"id": cid, "label": code.code, "quotes": quotes}

        coders_payload = [
            {
                "coder_id": codes[0].coder_id,
                "codes": [_code_payload(c) for c in codes],
            }
            for codes in coder_codes
            if codes
        ]
        return {"coders": coders_payload}, code_index

    def _coverage_errors(
        self, data: dict, code_index: dict[int, Code]
    ) -> list[str]:
        seen_counts: dict[int, int] = {}
        unknown: list[int] = []

        def _bump(raw):
            try:
                cid = int(raw)
            except (TypeError, ValueError):
                unknown.append(raw)
                return
            if cid not in code_index:
                unknown.append(cid)
                return
            seen_counts[cid] = seen_counts.get(cid, 0) + 1

        for group in data.get("merge_groups", []) or []:
            for raw in group.get("original_code_ids", []) or []:
                _bump(raw)
        for raw in data.get("retain_code_ids", []) or []:
            _bump(raw)

        missing = sorted(cid for cid in code_index if cid not in seen_counts)
        duplicates = sorted(cid for cid, n in seen_counts.items() if n > 1)

        errors: list[str] = []
        if missing:
            errors.append(
                f"missing code ids (not assigned to any merge or retain): {missing}"
            )
        if duplicates:
            errors.append(f"code ids assigned more than once: {duplicates}")
        if unknown:
            errors.append(f"unknown code ids (not in the input): {unknown}")
        return errors

    def _call_with_retries(
        self,
        system_prompt: str,
        user_prompt: str,
        code_index: dict[int, Code],
    ) -> tuple[dict | None, str, float, int]:
        max_attempts = 3
        current_prompt = user_prompt
        response = ""
        data: dict | None = None
        t0 = time.monotonic()
        attempts = 0
        for attempt in range(max_attempts):
            attempts = attempt + 1
            response = self._call_llm(
                system_prompt,
                current_prompt,
                response_format=AGGREGATOR_RESPONSE_SCHEMA,
            )
            data = extract_response_json(response)
            if data is None:
                break
            errors = self._coverage_errors(data, code_index)
            if not errors:
                break
            if attempt == max_attempts - 1:
                break
            err_text = "\n".join(f"- {e}" for e in errors)
            current_prompt = (
                f"{user_prompt}\n\n"
                f"Your previous response was:\n{response}\n\n"
                f"That response violated this rule: every input code id "
                f"must appear in exactly one of `merge_groups` "
                f"(inside `original_code_ids`) or `retain_code_ids`. "
                f"Problems found:\n{err_text}\n\n"
                f"Return a corrected JSON object that fixes these issues. "
                f"Use only the code ids from the original input."
            )
        return data, response, time.monotonic() - t0, attempts

    def _build_codes(
        self,
        segment: Segment,
        codebook: Codebook,
        data: dict,
        code_index: dict[int, Code],
    ) -> list[Code]:
        max_quotes = self.aggregator_config.max_quotes_per_code
        out: list[Code] = []
        consumed: set[int] = set()

        for group in data.get("merge_groups", []) or []:
            label = group.get("merged_code", "")
            orig_ids = [int(i) for i in group.get("original_code_ids", [])]
            rationale = group.get("rationale", "")

            sources: list[Code] = []
            quotes: list = []
            seen_qids: set[int] = set()
            for cid in orig_ids:
                src = code_index.get(cid)
                if src is None or cid in consumed:
                    continue
                consumed.add(cid)
                sources.append(src)
                for q in src.supporting_quotes or []:
                    if q.quote_id is None or q.quote_id in seen_qids:
                        continue
                    seen_qids.add(q.quote_id)
                    quotes.append(q)

            if not sources:
                continue
            new_code = Code(
                segment_id=segment.segment_id,
                coder_id=SYSTEM_AGGREGATOR_ID,
                codebook_used_id=codebook.version,
                code=label,
                description="",
                rationale=rationale,
            )
            new_code.supporting_quotes = quotes[:max_quotes]
            new_code.derivation_sources = [
                CodesDerived(
                    source_code=src,
                    derivation_type=DERIVATION_AGGREGATION,
                )
                for src in sources
            ]
            out.append(new_code)

        for raw in data.get("retain_code_ids", []) or []:
            cid = int(raw)
            src = code_index.get(cid)
            if src is None or cid in consumed:
                continue
            consumed.add(cid)
            out.append(src)

        return out

    def _sentinel(self, segment: Segment, codebook: Codebook) -> Code:
        return Code(
            segment_id=segment.segment_id,
            coder_id=SYSTEM_AGGREGATOR_ID,
            codebook_used_id=codebook.version,
            code=SENTINEL_CODE_LABEL,
        )

    def _copy_as_aggregator(
        self, segment: Segment, codebook: Codebook, src: Code
    ) -> Code:
        """Re-stamp ``src`` as an aggregator code with one provenance edge."""
        new_code = Code(
            segment_id=segment.segment_id,
            coder_id=SYSTEM_AGGREGATOR_ID,
            codebook_used_id=codebook.version,
            code=src.code,
            description=src.description,
            rationale=src.rationale,
        )
        new_code.supporting_quotes = list(src.supporting_quotes or [])[
            : self.aggregator_config.max_quotes_per_code
        ]
        new_code.derivation_sources = [
            CodesDerived(source_code=src, derivation_type=DERIVATION_AGGREGATION)
        ]
        return new_code
