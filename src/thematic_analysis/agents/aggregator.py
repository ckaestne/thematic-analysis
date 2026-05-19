"""Code Aggregator agent for merging codes from multiple coders."""

import json
import re
from dataclasses import dataclass

from thematic_analysis.agents.base import AgentConfig, BaseAgent
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
The user message is a JSON object with two fields:
- `coders`: a list of coders. Each coder has a `coder_id` and a list of
  `codes`. Each code has an integer `id`, a `label`, and a list of
  `quote_ids` (the quotes that support it).
- `quotes`: a list of quotes. Each quote has an integer `id` and a `text`.

## Rules
1. Each code is attributed to exactly one coder. Codes produced by the
   **same coder** already represent distinct concepts in that coder's view
   and must **not** be merged with each other. Only consider merging codes
   that come from *different* coders.
2. Merge codes when they describe the same phenomenon, even if from
   different angles or at different levels of specificity. Give the
   merged code a clear, representative label.
3. Keep codes separate when merging them would lose an analytical
   distinction.
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

    def get_system_prompt(self) -> str:
        return AGGREGATOR_SYSTEM_PROMPT

    def _build_prompt_payload(
        self, coder_codes: list[list[DBCode]]
    ) -> tuple[dict, dict[int, DBCode], dict[int, Quote]]:
        """Assign sequential ids and build the JSON payload for the LLM.

        Returns:
            payload: dict to be json-dumped as the user message.
            code_index: assigned code id -> DBCode row.
            quote_index: assigned quote id -> Quote.
        """
        quote_id_by_text: dict[str, int] = {}
        quote_index: dict[int, Quote] = {}
        code_index: dict[int, DBCode] = {}
        coders_payload: list[dict] = []

        next_code_id = 1
        next_quote_id = 1

        for codes in coder_codes:
            if not codes:
                continue
            coder_id = codes[0].coder_id
            codes_payload: list[dict] = []
            for c in codes:
                cid = next_code_id
                next_code_id += 1
                code_index[cid] = c

                quote_ids: list[int] = []
                for sq in (c.supporting_quotes or []):
                    qid = quote_id_by_text.get(sq.text)
                    if qid is None:
                        qid = next_quote_id
                        next_quote_id += 1
                        quote_id_by_text[sq.text] = qid
                        quote_index[qid] = Quote(quote_id=str(qid), text=sq.text)
                    if qid not in quote_ids:
                        quote_ids.append(qid)

                codes_payload.append(
                    {"id": cid, "label": c.code, "quote_ids": quote_ids}
                )
            coders_payload.append({"coder_id": coder_id, "codes": codes_payload})

        quotes_payload = [
            {"id": qid, "text": quote_index[qid].text}
            for qid in sorted(quote_index)
        ]
        payload = {"coders": coders_payload, "quotes": quotes_payload}
        return payload, code_index, quote_index

    def _quotes_for_code(
        self, code: DBCode, quote_index: dict[int, Quote]
    ) -> list[Quote]:
        """Return assigned Quote objects (with the assigned ids) for a code."""
        # Look up by text so we reuse the assigned id from the payload.
        text_to_qid = {q.text: int(q.quote_id) for q in quote_index.values()}
        seen: set[int] = set()
        out: list[Quote] = []
        for sq in (code.supporting_quotes or []):
            qid = text_to_qid.get(sq.text)
            if qid is None or qid in seen:
                continue
            seen.add(qid)
            out.append(quote_index[qid])
        return out

    def _parse_response(
        self,
        response: str,
        code_index: dict[int, DBCode],
        quote_index: dict[int, Quote],
    ) -> AggregationResult | None:
        """Parse the LLM response into an AggregationResult."""
        json_match = re.search(r"```(?:json)?\s*(.*?)```", response, re.DOTALL)
        if json_match:
            json_str = json_match.group(1).strip()
        else:
            json_match = re.search(r"\{.*\}", response, re.DOTALL)
            if not json_match:
                return None
            json_str = json_match.group(0)

        try:
            data = json.loads(json_str)
        except json.JSONDecodeError:
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
            seen_qids: set[str] = set()
            for cid in orig_ids:
                src = code_index.get(cid)
                if src is None:
                    continue
                consumed_ids.add(cid)
                original_codes.append(src.code)
                for q in self._quotes_for_code(src, quote_index):
                    if q.quote_id in seen_qids:
                        continue
                    seen_qids.add(q.quote_id)
                    quotes.append(q)

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
            quotes = self._quotes_for_code(src, quote_index)
            retained_codes.append(
                MergedCode(
                    code=src.code,
                    original_codes=[src.code],
                    quotes=quotes[:max_quotes],
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
        payload, code_index, quote_index = self._build_prompt_payload(coder_codes)

        if not code_index:
            return AggregationResult(merged_codes=[], retained_codes=[])

        user_prompt = json.dumps(payload, indent=2)
        response = self._call_llm(
            self.get_system_prompt(),
            user_prompt,
            response_format=AGGREGATOR_RESPONSE_SCHEMA,
        )
        result = self._parse_response(response, code_index, quote_index)

        if result is None:
            max_quotes = self.aggregator_config.max_quotes_per_code
            retained = [
                MergedCode(
                    code=c.code,
                    original_codes=[c.code],
                    quotes=self._quotes_for_code(c, quote_index)[:max_quotes],
                )
                for c in code_index.values()
            ]
            return AggregationResult(merged_codes=[], retained_codes=retained)

        return result
