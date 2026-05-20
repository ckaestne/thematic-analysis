"""Code Aggregator agent for merging codes from multiple coders."""

import json
import time
from dataclasses import dataclass

from thematic_analysis.agents.base import AgentConfig, BaseAgent
from thematic_analysis.agents.json_utils import extract_response_json
from thematic_analysis.codebook import Quote
from thematic_analysis_inc.db.models import Code as DBCode


@dataclass
class AggregatorConfig(AgentConfig):
    """Configuration for the Code Aggregator agent."""

    max_quotes_per_code: int = 10


@dataclass
class MergedCode:
    """A code that may combine multiple similar codes."""

    code: str
    original_codes: list[str]
    quotes: list[Quote]
    merge_rationale: str = ""


@dataclass
class AggregationResult:
    """Result of aggregating codes from multiple coders."""

    merged_codes: list[MergedCode]
    retained_codes: list[MergedCode]  # Codes kept separate (different concepts)

    def to_dict(self) -> dict:
        """Convert to dictionary format."""
        return {
            "merged_codes": [
                {
                    "code": mc.code,
                    "original_codes": mc.original_codes,
                    "quotes": [
                        {"quote_id": q.quote_id, "text": q.text} for q in mc.quotes
                    ],
                    "merge_rationale": mc.merge_rationale,
                }
                for mc in self.merged_codes
            ],
            "retained_codes": [
                {
                    "code": rc.code,
                    "original_codes": rc.original_codes,
                    "quotes": [
                        {"quote_id": q.quote_id, "text": q.text} for q in rc.quotes
                    ],
                }
                for rc in self.retained_codes
            ],
        }

    def to_json(self) -> str:
        """Convert to structured JSON format."""
        return json.dumps(self.to_dict(), indent=2)

    def all_codes(self) -> list[MergedCode]:
        """Return all codes (merged and retained)."""
        return self.merged_codes + self.retained_codes


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
    and organizes the results with top-K most relevant quotes.
    """

    def __init__(self, config: AggregatorConfig | None = None):
        super().__init__(config or AggregatorConfig())
        self.aggregator_config: AggregatorConfig = self.config  # type: ignore
        # Debug fields populated by the most recent `aggregate()` call so
        # callers (e.g. the `test-aggregate` CLI) can inspect what the
        # agent did without re-implementing prompt building or the LLM
        # call.
        self.last_payload: dict | None = None
        self.last_system_prompt: str = ""
        self.last_user_prompt: str = ""
        self.last_raw_response: str = ""
        self.last_elapsed: float = 0.0
        self.last_attempts: int = 0

    def get_system_prompt(self) -> str:
        return AGGREGATOR_SYSTEM_PROMPT

    def _build_prompt_payload(
        self, coder_codes: list[list[DBCode]]
    ) -> tuple[dict, dict[int, DBCode]]:
        """Assign sequential ids and build the JSON payload for the LLM.

        Returns:
            payload: dict to be json-dumped as the user message.
            code_index: assigned code id -> DBCode row.
        """
        code_index: dict[int, DBCode] = {}

        def _quote_texts(code: DBCode) -> list[str]:
            return [sq.text for sq in (code.supporting_quotes or [])]

        def _code_payload(code: DBCode) -> dict:
            cid = len(code_index) + 1
            code_index[cid] = code
            return {"id": cid, "label": code.code, "quotes": _quote_texts(code)}

        coders_payload = [
            {
                "coder_id": codes[0].coder_id,
                "codes": [_code_payload(code) for code in codes],
            }
            for codes in coder_codes
            if codes
        ]

        payload = {"coders": coders_payload}
        return payload, code_index

    def _coverage_errors(
        self, data: dict, code_index: dict[int, DBCode]
    ) -> list[str]:
        """Check that every input code id appears in exactly one of
        ``merge_groups[*].original_code_ids`` or ``retain_code_ids``.
        Returns a list of human-readable error strings; empty if valid."""
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
            errors.append(
                f"code ids assigned more than once: {duplicates}"
            )
        if unknown:
            errors.append(
                f"unknown code ids (not in the input): {unknown}"
            )
        return errors

    def _parse_response(
        self,
        response: str,
        code_index: dict[int, DBCode],
    ) -> AggregationResult | None:
        """Parse the LLM response into an AggregationResult."""
        data = extract_response_json(response)
        if data is None:
            return None

        max_quotes = self.aggregator_config.max_quotes_per_code
        merged_codes: list[MergedCode] = []
        retained_codes: list[MergedCode] = []
        consumed_ids: set[int] = set()

        for group in data.get("merge_groups", []):
            label = group.get("merged_code", "")
            orig_ids = [int(i) for i in group.get("original_code_ids", [])]
            rationale = group.get("rationale", "")

            original_codes: list[str] = []
            quotes: list[Quote] = []
            for cid in orig_ids:
                src = code_index.get(cid)
                if src is None:
                    continue
                consumed_ids.add(cid)
                original_codes.append(src.code)
                quotes.extend(src.supporting_quotes or [])

            if not original_codes:
                continue
            merged_codes.append(
                MergedCode(
                    code=label,
                    original_codes=original_codes,
                    quotes=quotes[:max_quotes],
                    merge_rationale=rationale,
                )
            )

        for cid in data.get("retain_code_ids", []):
            cid = int(cid)
            src = code_index.get(cid)
            if src is None or cid in consumed_ids:
                continue
            consumed_ids.add(cid)
            quotes = (src.supporting_quotes or [])[:max_quotes]
            retained_codes.append(
                MergedCode(
                    code=src.code,
                    original_codes=[src.code],
                    quotes=quotes,
                )
            )

        return AggregationResult(
            merged_codes=merged_codes,
            retained_codes=retained_codes,
        )

    def aggregate(self, coder_codes: list[list[DBCode]]) -> AggregationResult:
        """Aggregate codes from multiple coders for one segment.

        Args:
            coder_codes: One inner list of ``Code`` rows per coder, all for
                the same segment. Each ``Code`` is expected to carry its
                ``supporting_quotes``.
        """
        payload, code_index = self._build_prompt_payload(coder_codes)

        system_prompt = self.get_system_prompt()
        user_prompt = json.dumps(payload, indent=2) if code_index else ""
        self.last_payload = payload
        self.last_system_prompt = system_prompt
        self.last_user_prompt = user_prompt
        self.last_raw_response = ""
        self.last_elapsed = 0.0
        self.last_attempts = 0

        if not code_index:
            return AggregationResult(merged_codes=[], retained_codes=[])

        max_attempts = 3
        current_prompt = user_prompt
        response = ""
        t0 = time.monotonic()
        for attempt in range(max_attempts):
            self.last_attempts = attempt + 1
            response = self._call_llm(
                system_prompt,
                current_prompt,
                response_format=AGGREGATOR_RESPONSE_SCHEMA,
            )
            data = extract_response_json(response)
            if data is None:
                # parse failure is handled by _parse_response below; no
                # point asking for a coverage fix when we can't even parse.
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
        self.last_elapsed = time.monotonic() - t0
        self.last_raw_response = response

        result = self._parse_response(response, code_index)

        if result is None:
            max_quotes = self.aggregator_config.max_quotes_per_code
            retained = [
                MergedCode(
                    code=c.code,
                    original_codes=[c.code],
                    quotes=(c.supporting_quotes or [])[:max_quotes],
                )
                for c in code_index.values()
            ]
            return AggregationResult(merged_codes=[], retained_codes=retained)

        return result
