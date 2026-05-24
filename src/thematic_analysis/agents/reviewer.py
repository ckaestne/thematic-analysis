"""Reviewer agent: integrates an aggregator Code into the codebook.

The agent takes one aggregator ``Code`` and returns a fresh reviewer
``Code`` (PK ``None``, ``coder_id == -1``, ``codebook_used_id ==
parent.version``). The returned Code carries provenance through
``derivation_sources`` (``CodesDerived`` 'R' edges) so the full history of
every reviewer decision is reachable as a graph:

- ADD     → one edge, source=aggregator.
- MERGE   → two edges, sources=[aggregator, previous_target_reviewer_code].
            Label kept from target; quotes union of target and aggregator.
- UPDATE  → two edges, same as MERGE. Label renamed to a new label.

The agent never mutates existing rows. The previous target reviewer Code
stays in the ``code`` table — it just drops out of the next codebook
revision's membership when ``materialize_codebook_revision`` runs.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass

from thematic_analysis.agents.base import AgentConfig, BaseAgent
from thematic_analysis.agents.json_utils import extract_response_json
from thematic_analysis.prompts import join_system_prompt_sections
from thematic_analysis_inc.db import embeddings as db_embeddings
from thematic_analysis_inc.db.coders import SYSTEM_REVIEWER_ID
from thematic_analysis_inc.db.models import (
    Code,
    Codebook,
    CodesDerived,
    DECISION_ADD,
    DECISION_MERGE,
    DECISION_MERGE_AND_RENAME,
    DERIVATION_REVIEW,
)


@dataclass
class ReviewerConfig(AgentConfig):
    """Configuration for the Reviewer agent."""

    task: str = "reviewer"
    similarity_threshold: float = 0.4  # Threshold for considering codes similar
    top_k_similar: int = 25  # Number of similar codes to retrieve (paper §4)
    merge_threshold: float = 0.90  # Threshold for automatic merging
    max_quotes_per_code: int = 5  # Quotes shown per code in the prompt


REVIEWER_SYSTEM_PROMPT = """\
You are an expert qualitative researcher responsible for maintaining the codebook.
Your task is to review a new code against similar existing codes and decide
how it should be integrated.

## Input
The user message is a JSON object:
- `new_code`: an object with `code` (the new label) and `quotes` (the verbatim
  quote texts that support it).
- `similar_codes`: a list of existing codes that may overlap. Each entry has
  `code` (the existing label), 
  and `quotes` (the verbatim quote texts already associated with it).

## Decision Guidelines
- **add_new**: the code represents a genuinely new concept. No other fields
  needed.
- **merge**: the new code captures the same concept as an existing one. Set
  `target_code` to the existing code's label (it is kept as the merged label).
- **merge_and_rename**: same as merge, but neither the existing label nor the
  new label is a good fit — propose a more representative label that captures
  both. Set `target_code` to the existing code's label and `new_label` to your
  proposed name. Avoid labels that are simply an AND connection between two ,
  distinct concepts; in that case prefer to add new codes. 
  

## Output Format
Respond with a single JSON object:
- `decision`: one of "add_new", "merge", or "merge_and_rename"
- `target_code`: the existing code label being merged into (exact label from
  the input). `null` for add_new.
- `new_label`: the proposed replacement label for merge_and_rename. `null` for
  add_new and merge.
- `rationale`: brief explanation for the decision."""

REVIEWER_RESPONSE_SCHEMA = {
    "type": "json_schema",
    "json_schema": {
        "name": "code_review_decision",
        "strict": True,
        "schema": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "decision": {
                    "type": "string",
                    "enum": ["add_new", "merge", "merge_and_rename"],
                },
                "target_code": {"type": ["string", "null"]},
                "new_label": {"type": ["string", "null"]},
                "rationale": {"type": "string"},
            },
            "required": [
                "decision", "target_code", "new_label", "rationale",
            ],
        },
    },
}


class ReviewerAgent(BaseAgent):
    """Reviewer agent — see module docstring for the contract.

    Constructor args:

    - ``codebook``: the parent DB ``Codebook`` row (only ``.research_context``
      and ``.version`` are read). Detached is fine.
    - ``live_codes``: the effective codebook membership during this batch
      (``db.live_codes_for_batch(parent)``). Used as the similarity-search pool.
      The agent never reads ``codebook.codes`` directly so it doesn't matter
      whether new codes added earlier in the batch are visible via the
      relationship.
    - ``embedding_service``: instance the agent uses to embed new code labels.
    """

    def __init__(
        self,
        codebook: Codebook,
        live_codes: list[Code],
        embedding_service: db_embeddings.EmbeddingService,
        config: ReviewerConfig | None = None,
    ):
        super().__init__(config or ReviewerConfig())
        self.reviewer_config: ReviewerConfig = self.config  # type: ignore
        self.codebook = codebook
        self.live_codes = list(live_codes)
        self.embedding_service = embedding_service

        # Debug fields populated by the most recent ``review_code`` call.
        # Short-circuit paths (auto-merge, no-similar) leave the LLM-specific
        # fields empty.
        self.last_similar: list[tuple[Code, float]] = []
        self.last_payload: dict | None = None
        self.last_system_prompt: str = ""
        self.last_user_prompt: str = ""
        self.last_raw_response: str = ""
        self.last_elapsed: float = 0.0
        self.last_shortcut: str | None = None  # "auto_merge" | "no_similar" | None

    def get_system_prompt(self) -> str:
        # Render the research context the same way the domain dataclass would:
        # tailored ``reviewer_prompt`` if present, else the raw description
        # wrapped in a generic header. The agent reads the DB row directly
        # rather than depending on a domain conversion helper.
        rc = self.codebook.research_context
        research_section = ""
        if rc is not None:
            tailored = (getattr(rc, "reviewer_prompt", None) or "").strip()
            if tailored:
                research_section = tailored
            else:
                desc = (getattr(rc, "description", None) or "").strip()
                if desc:
                    research_section = f"## Research Context\n{desc}"
        return join_system_prompt_sections(
            REVIEWER_SYSTEM_PROMPT,
            research_context_instructions=research_section,
        )

    # ── construction helpers ────────────────────────────────────────────

    def _embed(self, text: str) -> bytes:
        return db_embeddings.encode(self.embedding_service.embed_single(text))

    def _new_reviewer_code(
        self,
        source_code_from_aggregator: Code,
        label: str,
        rationale: str,
        quotes: list,
        embedding: bytes,
        prev_target: Code | None,
        decision: str,
    ) -> Code:
        """Build a fresh reviewer Code with provenance edges wired up."""
        new_code = Code(
            segment_id=source_code_from_aggregator.segment_id,
            coder_id=SYSTEM_REVIEWER_ID,
            codebook_used_id=self.codebook.version,
            code=label,
            description="",
            rationale=rationale,
            embedding=embedding,
        )
        new_code.supporting_quotes = list(quotes)
        edges = [
            CodesDerived(
                source_code=source_code_from_aggregator,
                derivation_type=DERIVATION_REVIEW,
                decision=decision,
                rationale=rationale,
            )
        ]
        if prev_target is not None:
            edges.append(
                CodesDerived(
                    source_code=prev_target,
                    derivation_type=DERIVATION_REVIEW,
                    decision=decision,
                    rationale=rationale,
                )
            )
        new_code.derivation_sources = edges
        return new_code

    def _add(self, source_code_from_aggregator: Code, label: str, rationale: str) -> Code:
        return self._new_reviewer_code(
            source_code_from_aggregator=source_code_from_aggregator,
            label=label,
            rationale=rationale,
            quotes=list(source_code_from_aggregator.supporting_quotes or []),
            embedding=self._embed(label),
            prev_target=None,
            decision=DECISION_ADD,
        )

    def _merge_or_update(
        self,
        source_code_from_aggregator: Code,
        target: Code,
        label: str,
        rationale: str,
        decision: str,
    ) -> Code:
        quotes = self._union_quotes(target, source_code_from_aggregator)
        return self._new_reviewer_code(
            source_code_from_aggregator=source_code_from_aggregator,
            label=label,
            rationale=rationale,
            quotes=quotes,
            embedding=self._embed(label),
            prev_target=target,
            decision=decision,
        )

    @staticmethod
    def _union_quotes(target: Code, source_code_from_aggregator: Code) -> list:
        # SQLModel Quote rows are unhashable; key on PK in a dict so the
        # identity map's canonical instance is preserved.
        out: dict[int, object] = {}
        for q in (target.supporting_quotes or []):
            if q.quote_id is not None:
                out[q.quote_id] = q
        for q in (source_code_from_aggregator.supporting_quotes or []):
            if q.quote_id is not None and q.quote_id not in out:
                out[q.quote_id] = q
        return list(out.values())

    # ── prompt + parsing ────────────────────────────────────────────────

    def _build_payload(
        self,
        new_code_text: str,
        new_code_quotes: list,
        similar: list[tuple[Code, float]],
    ) -> dict:
        n = self.reviewer_config.max_quotes_per_code
        return {
            "new_code": {
                "code": new_code_text,
                "quotes": [q.text for q in new_code_quotes[:n]],
            },
            "similar_codes": [
                {
                    "code": entry.code,
                    "quotes": [q.text for q in (entry.supporting_quotes or [])[:n]],
                }
                for entry, score in similar
            ],
        }

    def _parse_response(
        self, response: str
    ) -> tuple[str, str | None, str | None, str]:
        """Return ``(decision_str, target_label, new_label, rationale)``.

        Falls back to ``("add_new", None, None, …)`` on parse failure.
        """
        data = extract_response_json(response)
        if data is None:
            return "add_new", None, None, "Could not parse response"
        decision_str = (data.get("decision") or "add_new").lower()
        if decision_str not in {"add_new", "merge", "merge_and_rename"}:
            decision_str = "add_new"
        return (
            decision_str,
            data.get("target_code"),
            data.get("new_label"),
            data.get("rationale", ""),
        )

    def _resolve_target(self, label: str | None) -> Code | None:
        if label is None:
            return None
        for c in self.live_codes:
            if c.code == label:
                return c
        return None

    # ── public API ──────────────────────────────────────────────────────

    def review_code(self, code: Code) -> Code:
        """Review one aggregator ``Code`` and return a fresh reviewer ``Code``.

        The returned Code's ``code_id`` is ``None``; the worker calls
        ``s.add`` and lets the cascade write the provenance edges and the
        quote links.
        """
        result = self._review_code_impl(code)
        assert result.coder_id == SYSTEM_REVIEWER_ID, (
            "reviewer must return a code with coder_id == SYSTEM_REVIEWER_ID; "
            f"got {result.coder_id}"
        )
        return result

    def _review_code_impl(self, code: Code) -> Code:
        self.last_similar = []
        self.last_payload = None
        self.last_system_prompt = ""
        self.last_user_prompt = ""
        self.last_raw_response = ""
        self.last_elapsed = 0.0
        self.last_shortcut = None

        query_emb = self.embedding_service.embed_single(code.code)
        similar = db_embeddings.find_similar(
            query_emb, self.live_codes, top_k=self.reviewer_config.top_k_similar
        )
        self.last_similar = similar

        # Auto-merge: top match similarity ≥ merge_threshold.
        if similar:
            top_entry, top_score = similar[0]
            if top_score >= self.reviewer_config.merge_threshold:
                self.last_shortcut = "auto_merge"
                return self._merge_or_update(
                    source_code_from_aggregator=code,
                    target=top_entry,
                    label=top_entry.code,
                    rationale=f"Automatic merge: {top_score:.2f} similarity",
                    decision=DECISION_MERGE,
                )

        # Below threshold: nothing to compare against → ADD.
        above = [
            (entry, score)
            for entry, score in similar
            if score >= self.reviewer_config.similarity_threshold
        ]
        if not above:
            self.last_shortcut = "no_similar"
            return self._add(
                source_code_from_aggregator=code,
                label=code.code,
                rationale="No similar codes found",
            )

        # Otherwise let the LLM decide.
        payload = self._build_payload(
            code.code, list(code.supporting_quotes or []), above
        )
        system_prompt = self.get_system_prompt()
        user_prompt = json.dumps(payload, indent=2)
        self.last_payload = payload
        self.last_system_prompt = system_prompt
        self.last_user_prompt = user_prompt

        t0 = time.monotonic()
        response = self._call_llm(
            system_prompt, user_prompt, response_format=REVIEWER_RESPONSE_SCHEMA
        )
        self.last_raw_response = response
        self.last_elapsed = time.monotonic() - t0

        decision_str, target_label, new_label, rationale = self._parse_response(
            response
        )
        target = self._resolve_target(target_label) if decision_str in {
            "merge",
            "merge_and_rename",
        } else None

        if decision_str == "merge" and target is not None:
            return self._merge_or_update(
                source_code_from_aggregator=code,
                target=target,
                label=target.code,
                rationale=rationale,
                decision=DECISION_MERGE,
            )
        if (
            decision_str == "merge_and_rename"
            and target is not None
            and new_label
        ):
            return self._merge_or_update(
                source_code_from_aggregator=code,
                target=target,
                label=new_label,
                rationale=rationale,
                decision=DECISION_MERGE_AND_RENAME,
            )
        # add_new, or merge/merge_and_rename with an unresolvable target or
        # (for rename) a missing new_label.
        return self._add(
            source_code_from_aggregator=code,
            label=code.code,
            rationale=rationale or "Add as new code",
        )
