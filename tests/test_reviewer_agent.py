"""Tests for ReviewerAgent."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from thematic_analysis.agents.reviewer import (
    REVIEWER_RESPONSE_SCHEMA,
    ReviewerAgent,
    ReviewerConfig,
)
from thematic_analysis_inc import db as store
from thematic_analysis_inc.db import embeddings as db_embeddings
from thematic_analysis_inc.db.aggregation import save_aggregator_codes
from thematic_analysis_inc.db.coders import SYSTEM_AGGREGATOR_ID, SYSTEM_REVIEWER_ID
from thematic_analysis_inc.db.models import (
    Code,
    CodesDerived,
    DECISION_ADD,
    DECISION_MERGE,
    DECISION_UPDATE,
    DERIVATION_REVIEW,
)
from thematic_analysis_inc.db.review import save_reviewer_decision


@pytest.fixture
def db_setup(tmp_path: Path):
    """Initialise an empty DB and return the connection."""
    return store.init_db(tmp_path / "x.sqlite")


@pytest.fixture
def embedding_service():
    return db_embeddings.EmbeddingService(use_mock=True)


def _seed_segment(content: str = "seg") -> int:
    doc = store.add_document("doc.md")
    segs = store.enqueue_segments(doc, [(None, content, 0, 5, 0)])
    return segs[0].segment_id


def _add_agg_code(*, segment_id: int, code: str, version: int, quotes: list[str]) -> Code:
    seg = store.get_segment(segment_id)
    quote_rows = [store.add_quote(seg, t) for t in quotes]
    c = Code(
        segment_id=segment_id,
        coder_id=SYSTEM_AGGREGATOR_ID,
        codebook_used_id=version,
        code=code,
    )
    c.supporting_quotes = quote_rows
    return save_aggregator_codes([c])[0]


def _seed_reviewer_code(
    *, segment_id: int, code: str, version: int, quotes: list[str],
    embedding_service: db_embeddings.EmbeddingService,
    source_code_from_aggregator: Code,
) -> Code:
    """Write a reviewer Code via save_reviewer_decision (no finalize)."""
    seg = store.get_segment(segment_id)
    qrows = [store.add_quote(seg, t) for t in quotes]
    c = Code(
        segment_id=segment_id,
        coder_id=SYSTEM_REVIEWER_ID,
        codebook_used_id=version,
        code=code,
        embedding=db_embeddings.encode(embedding_service.embed_single(code)),
    )
    c.supporting_quotes = qrows
    c.derivation_sources = [
        CodesDerived(
            source_code=source_code_from_aggregator,
            derivation_type=DERIVATION_REVIEW,
            decision=DECISION_ADD,
        )
    ]
    return save_reviewer_decision(c)


def _load_parent_with_one_reviewer_code(
    embedding_service: db_embeddings.EmbeddingService,
    label: str,
) -> tuple[Code, Code]:
    """Set up: aggregator code + finalized codebook containing one reviewer
    code with ``label`` and a single quote. Returns (parent_codebook,
    target_reviewer_code, next_aggregator_code_to_review)."""
    seg_id = _seed_segment()
    agg1 = _add_agg_code(
        segment_id=seg_id, code=label, version=1, quotes=[f"q-{label}"]
    )
    reviewer_code = _seed_reviewer_code(
        segment_id=seg_id,
        code=label,
        version=1,
        quotes=[f"q-{label}"],
        embedding_service=embedding_service,
        source_code_from_aggregator=agg1,
    )
    new_cb = store.materialize_codebook_revision(
        store.get_codebook_with_codes_and_research_context(1)
    )
    assert new_cb is not None
    parent = store.get_codebook_with_codes_and_research_context(new_cb.version)
    return parent, reviewer_code


class TestReviewerConfig:
    def test_defaults(self):
        c = ReviewerConfig()
        assert c.similarity_threshold == 0.75
        assert c.top_k_similar == 10
        assert c.merge_threshold == 0.90


class TestBuildPayload:
    def test_payload_carries_new_code_quotes_and_similars(
        self, db_setup, embedding_service
    ):
        parent, target = _load_parent_with_one_reviewer_code(
            embedding_service, "emotional support"
        )
        agg = _add_agg_code(
            segment_id=target.segment_id,
            code="peer comfort",
            version=parent.version,
            quotes=["feeling supported"],
        )
        agent = ReviewerAgent(
            codebook=parent,
            live_codes=list(parent.codes),
            embedding_service=embedding_service,
        )
        payload = agent._build_payload(
            agg.code,
            list(agg.supporting_quotes),
            [(target, 0.85)],
        )
        assert payload["new_code"]["code"] == "peer comfort"
        assert payload["new_code"]["quotes"] == ["feeling supported"]
        assert payload["similar_codes"][0]["code"] == "emotional support"
        assert payload["similar_codes"][0]["similarity"] == 0.85


class TestSystemPrompt:
    def test_system_prompt_mentions_decisions(self, db_setup, embedding_service):
        parent = store.get_codebook_with_codes_and_research_context(1)
        agent = ReviewerAgent(
            codebook=parent,
            live_codes=[],
            embedding_service=embedding_service,
        )
        prompt = agent.get_system_prompt()
        assert "merge" in prompt
        assert "update" in prompt
        assert "add_new" in prompt
        assert "skip" not in prompt  # SKIP is gone


class TestReviewCodeDecisions:
    def test_no_similar_returns_add(self, db_setup, embedding_service):
        """Empty codebook → result is a fresh reviewer Code with one ADD edge."""
        parent = store.get_codebook_with_codes_and_research_context(1)
        seg_id = _seed_segment()
        agg = _add_agg_code(
            segment_id=seg_id, code="new concept", version=1, quotes=["q"]
        )
        agent = ReviewerAgent(
            codebook=parent,
            live_codes=[],
            embedding_service=embedding_service,
        )
        result = agent.review_code(agg)
        assert result.code_id is None
        assert result.coder_id == SYSTEM_REVIEWER_ID
        assert result.codebook_used_id == parent.version
        assert result.code == "new concept"
        edges = list(result.derivation_sources)
        assert len(edges) == 1
        assert edges[0].decision == DECISION_ADD
        assert edges[0].source_code is agg
        assert result.embedding is not None

    def test_auto_merge_above_threshold(self, db_setup, embedding_service):
        """A very high similarity short-circuits to MERGE with no LLM call."""
        parent, target = _load_parent_with_one_reviewer_code(
            embedding_service, "emotional support"
        )
        agg = _add_agg_code(
            segment_id=target.segment_id,
            code="emotional support",  # identical label → similarity 1.0
            version=parent.version,
            quotes=["different quote"],
        )
        agent = ReviewerAgent(
            codebook=parent,
            live_codes=list(parent.codes),
            embedding_service=embedding_service,
            config=ReviewerConfig(merge_threshold=0.85, similarity_threshold=0.5),
        )
        result = agent.review_code(agg)
        assert agent.last_shortcut == "auto_merge"
        edges = list(result.derivation_sources)
        assert len(edges) == 2
        assert [e.decision for e in edges] == [DECISION_MERGE, DECISION_MERGE]
        # Quote union: target's quote + new aggregator's quote
        texts = {q.text for q in result.supporting_quotes}
        assert texts == {"q-emotional support", "different quote"}
        # Label is the target's label (kept on MERGE)
        assert result.code == "emotional support"

    def test_llm_merge_resolves_target_by_label(self, db_setup, embedding_service):
        parent, target = _load_parent_with_one_reviewer_code(
            embedding_service, "emotional support"
        )
        agg = _add_agg_code(
            segment_id=target.segment_id,
            code="peer comfort",
            version=parent.version,
            quotes=["felt cared for"],
        )
        # Force the LLM path: low similarity threshold, low merge threshold,
        # but mock the LLM to return a merge decision.
        agent = ReviewerAgent(
            codebook=parent,
            live_codes=list(parent.codes),
            embedding_service=embedding_service,
            config=ReviewerConfig(
                similarity_threshold=-1.1, merge_threshold=1.1
            ),
        )
        with patch.object(
            ReviewerAgent,
            "_call_llm",
            return_value=json.dumps({
                "decision": "merge",
                "target_code": "emotional support",
                "rationale": "same idea",
            }),
        ):
            result = agent.review_code(agg)
        edges = list(result.derivation_sources)
        assert [e.decision for e in edges] == [DECISION_MERGE, DECISION_MERGE]
        assert result.code == "emotional support"

    def test_llm_update_renames_target(self, db_setup, embedding_service):
        parent, target = _load_parent_with_one_reviewer_code(
            embedding_service, "old label"
        )
        agg = _add_agg_code(
            segment_id=target.segment_id,
            code="new better label",
            version=parent.version,
            quotes=["q"],
        )
        agent = ReviewerAgent(
            codebook=parent,
            live_codes=list(parent.codes),
            embedding_service=embedding_service,
            config=ReviewerConfig(
                similarity_threshold=-1.1, merge_threshold=1.1
            ),
        )
        with patch.object(
            ReviewerAgent,
            "_call_llm",
            return_value=json.dumps({
                "decision": "update",
                "target_code": "old label",
                "rationale": "better phrasing",
            }),
        ):
            result = agent.review_code(agg)
        edges = list(result.derivation_sources)
        assert [e.decision for e in edges] == [DECISION_UPDATE, DECISION_UPDATE]
        assert result.code == "new better label"

    def test_unparseable_response_falls_back_to_add(
        self, db_setup, embedding_service
    ):
        parent, target = _load_parent_with_one_reviewer_code(
            embedding_service, "existing"
        )
        agg = _add_agg_code(
            segment_id=target.segment_id,
            code="something",
            version=parent.version,
            quotes=["q"],
        )
        agent = ReviewerAgent(
            codebook=parent,
            live_codes=list(parent.codes),
            embedding_service=embedding_service,
            config=ReviewerConfig(
                similarity_threshold=-1.1, merge_threshold=1.1
            ),
        )
        with patch.object(
            ReviewerAgent, "_call_llm", return_value="not json at all"
        ):
            result = agent.review_code(agg)
        edges = list(result.derivation_sources)
        assert len(edges) == 1
        assert edges[0].decision == DECISION_ADD

    def test_does_not_mutate_target(self, db_setup, embedding_service):
        """The agent must never mutate existing rows in live_codes."""
        parent, target = _load_parent_with_one_reviewer_code(
            embedding_service, "X"
        )
        original_label = target.code
        original_quote_ids = {q.quote_id for q in target.supporting_quotes}
        agg = _add_agg_code(
            segment_id=target.segment_id,
            code="X",
            version=parent.version,
            quotes=["new"],
        )
        agent = ReviewerAgent(
            codebook=parent,
            live_codes=list(parent.codes),
            embedding_service=embedding_service,
            config=ReviewerConfig(merge_threshold=0.5),
        )
        agent.review_code(agg)
        assert target.code == original_label
        assert {q.quote_id for q in target.supporting_quotes} == original_quote_ids


class TestResponseSchema:
    def test_schema_drops_skip(self):
        decision_enum = REVIEWER_RESPONSE_SCHEMA["json_schema"]["schema"][
            "properties"
        ]["decision"]["enum"]
        assert "skip" not in decision_enum
        assert set(decision_enum) == {"merge", "update", "add_new"}
