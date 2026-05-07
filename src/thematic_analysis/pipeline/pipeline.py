"""Two-stage pipeline for thematic analysis.

This module implements the full Thematic-LM pipeline with:
- Parallel async execution of multiple coders
- Independent codebook copies per coder (no shared state)
- Integration with NegotiationStrategy from iterative module
- HITL checkpoints for human intervention
- Built-in evaluation options

Based on Section 3 of 'Thematic-LM: A LLM-based Multi-agent System for
Large-scale Thematic Analysis' (WWW '25)
"""

import asyncio
import json
import uuid
from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path

from thematic_analysis.agents import (
    AggregationResult,
    AggregatorConfig,
    CodeAggregatorAgent,
    CodeAssignment,
    CoderAgent,
    CoderConfig,
    ReviewerAgent,
    ReviewerConfig,
    ThemeAggregationResult,
    ThemeAggregatorAgent,
    ThemeAggregatorConfig,
    ThemeCoderAgent,
    ThemeCoderConfig,
    ThemeResult,
)
from thematic_analysis.codebook import Codebook, EmbeddingService
from thematic_analysis.iterative import (
    IterationMetrics,
    IterativePipelineConfig,
    IterativePipelineController,
    NegotiationStrategy,
    calculate_agreement_rate,
)
from thematic_analysis.research_context import ResearchContext


class ExecutionMode(Enum):
    """Execution mode for the pipeline."""

    SEQUENTIAL = "sequential"  # Original sequential execution
    PARALLEL = "parallel"  # Parallel async execution (recommended)


@dataclass
class EvaluationConfig:
    """Configuration for evaluation."""

    evaluate_credibility: bool = False
    evaluate_dependability: bool = False
    evaluate_transferability: bool = False
    credibility_model: str = "gpt-4o-mini"


@dataclass
class HITLCheckpoint:
    """A checkpoint for human-in-the-loop intervention."""

    stage: str
    batch_number: int
    codebook_snapshot: dict
    metrics: dict
    requires_approval: bool = False


@dataclass
class PipelineConfig:
    """Configuration for the thematic analysis pipeline."""

    # Stage 1: Coding configuration
    num_coders: int = 3
    coder_config: CoderConfig = field(default_factory=CoderConfig)
    aggregator_config: AggregatorConfig = field(default_factory=AggregatorConfig)
    reviewer_config: ReviewerConfig = field(default_factory=ReviewerConfig)

    # Stage 2: Theme development configuration
    num_theme_coders: int = 3
    theme_coder_config: ThemeCoderConfig = field(default_factory=ThemeCoderConfig)
    theme_aggregator_config: ThemeAggregatorConfig = field(
        default_factory=ThemeAggregatorConfig
    )

    # Processing configuration
    batch_size: int = 10
    use_mock_embeddings: bool = False
    execution_mode: ExecutionMode = ExecutionMode.PARALLEL

    # Coder identities (optional)
    coder_identities: list[str] = field(default_factory=list)
    theme_coder_identities: list[str] = field(default_factory=list)

    # Negotiation strategy for code agreement (from iterative module)
    negotiation_strategy: NegotiationStrategy = NegotiationStrategy.CONSENSUS

    # Iterative pipeline settings
    iterative_config: IterativePipelineConfig | None = None

    # Evaluation settings
    evaluation_config: EvaluationConfig | None = None

    # HITL settings
    enable_hitl_checkpoints: bool = False
    checkpoint_frequency: int = 5  # Checkpoint every N batches

    # Research context (passed to coder/reviewer/theme_coder to keep
    # analysis on-topic relative to the research questions)
    research_context: ResearchContext | None = None

    # If set, the pipeline writes intermediate artifacts (per-coder
    # assignments, per-segment aggregations, codebook snapshots, per-theme-coder
    # results, final theme aggregation) under this directory as JSON.
    debug_dir: str | None = None


@dataclass
class DataSegment:
    """A segment of data to be coded."""

    segment_id: str
    text: str


@dataclass
class EvaluationResult:
    """Results from evaluation."""

    credibility_score: float | None = None
    dependability_score: float | None = None
    transferability_score: float | None = None


@dataclass
class PipelineResult:
    """Result of running the thematic analysis pipeline."""

    themes: ThemeAggregationResult
    codebook: Codebook
    stage1_aggregations: list[AggregationResult] = field(default_factory=list)
    evaluation: EvaluationResult | None = None
    checkpoints: list[HITLCheckpoint] = field(default_factory=list)
    metrics: dict = field(default_factory=dict)

    def to_json(self) -> str:
        """Convert result to JSON format."""
        return json.dumps(self.to_dict(), indent=2)

    def to_dict(self) -> dict:
        """Convert result to dictionary format."""
        result = {
            "themes": self.themes.to_dict(),
            "codebook": self.codebook.to_dict(),
            "stage1_aggregations": [a.to_dict() for a in self.stage1_aggregations],
            "metrics": self.metrics,
        }
        if self.evaluation:
            result["evaluation"] = {
                "credibility_score": self.evaluation.credibility_score,
                "dependability_score": self.evaluation.dependability_score,
                "transferability_score": self.evaluation.transferability_score,
            }
        return result


class ThematicLMPipeline:
    """Two-stage pipeline for large-scale thematic analysis.

    Architecture (per paper Figure 2):
    ```
    Stage 1 (Coding):
    ┌─────────┐   ┌─────────┐   ┌─────────┐
    │ Coder 1 │   │ Coder 2 │   │ Coder 3 │   ← Independent, parallel
    └────┬────┘   └────┬────┘   └────┬────┘
         │             │             │
         └──────┬──────┴──────┬──────┘
                │             │
                ▼             ▼
         ┌─────────────────────────┐
         │      Aggregator         │   ← Merges codes using NegotiationStrategy
         └───────────┬─────────────┘
                     │
                     ▼
         ┌─────────────────────────┐
         │       Reviewer          │   ← Updates canonical codebook
         └───────────┬─────────────┘
                     │
                     ▼
                [Codebook]

    Stage 2 (Theme Development):
    Codebook → Theme Coders (parallel) → Theme Aggregator → Final Themes
    ```

    Key improvements over naive implementation:
    - Coders get INDEPENDENT codebook copies (no shared state)
    - Parallel async execution via asyncio.gather
    - NegotiationStrategy integration for code agreement
    - HITL checkpoints for human intervention
    - Built-in evaluation
    """

    def __init__(
        self,
        config: PipelineConfig | None = None,
        embedding_service: EmbeddingService | None = None,
    ):
        """Initialize the pipeline.

        Args:
            config: Pipeline configuration.
            embedding_service: Service for computing embeddings.
        """
        self.config = config or PipelineConfig()
        self.embedding_service = embedding_service or EmbeddingService(
            use_mock=self.config.use_mock_embeddings
        )

        # Initialize codebook (canonical version updated by Reviewer)
        self.codebook = Codebook(
            embedding_service=self.embedding_service,
        )

        # Initialize Stage 1 agents (created fresh per run)
        self._coders: list[CoderAgent] = []
        self._aggregator: CodeAggregatorAgent | None = None
        self._reviewer: ReviewerAgent | None = None

        # Initialize Stage 2 agents
        self._theme_coders: list[ThemeCoderAgent] = []
        self._theme_aggregator: ThemeAggregatorAgent | None = None

        # Iterative controller (if enabled)
        self._iterative_controller: IterativePipelineController | None = None
        if self.config.iterative_config:
            self._iterative_controller = IterativePipelineController(
                self.config.iterative_config
            )

        # HITL checkpoints
        self._checkpoints: list[HITLCheckpoint] = []

        self._debug_root: Path | None = None
        if self.config.debug_dir:
            self._debug_root = Path(self.config.debug_dir)
            self._debug_root.mkdir(parents=True, exist_ok=True)

    def _dump(self, relative_path: str, payload: object) -> None:
        if self._debug_root is None:
            return
        target = self._debug_root / relative_path
        target.parent.mkdir(parents=True, exist_ok=True)
        if hasattr(payload, "to_dict"):
            data = payload.to_dict()
        elif hasattr(payload, "__dataclass_fields__"):
            data = asdict(payload)
        else:
            data = payload
        with target.open("w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, default=str)

    def _init_stage1_agents(self) -> None:
        """Initialize Stage 1 (coding) agents with INDEPENDENT codebook copies."""
        self._coders = []
        for i in range(self.config.num_coders):
            coder_config = CoderConfig(
                max_codes_per_segment=self.config.coder_config.max_codes_per_segment,
                model=self.config.coder_config.model,
                temperature=self.config.coder_config.temperature,
                max_tokens=self.config.coder_config.max_tokens,
            )

            # Set identity if available
            if i < len(self.config.coder_identities):
                coder_config.identity = self.config.coder_identities[i]

            # CRITICAL: Each coder gets INDEPENDENT codebook copy
            # Per paper: "Each coder often works independently to generate codes"
            coder_codebook = self.codebook.copy()
            coder = CoderAgent(
                config=coder_config,
                codebook=coder_codebook,
                research_context=self.config.research_context,
            )
            self._coders.append(coder)

        # Aggregator works with the canonical codebook
        self._aggregator = CodeAggregatorAgent(
            config=self.config.aggregator_config,
            codebook=self.codebook,
        )

        # Reviewer maintains the canonical codebook
        self._reviewer = ReviewerAgent(
            config=self.config.reviewer_config,
            codebook=self.codebook,
            research_context=self.config.research_context,
        )

    def _init_stage2_agents(self) -> None:
        """Initialize Stage 2 (theme development) agents."""
        self._theme_coders = []
        for i in range(self.config.num_theme_coders):
            theme_config = ThemeCoderConfig(
                max_themes=self.config.theme_coder_config.max_themes,
                min_codes_per_theme=self.config.theme_coder_config.min_codes_per_theme,
                model=self.config.theme_coder_config.model,
                temperature=self.config.theme_coder_config.temperature,
                max_tokens=self.config.theme_coder_config.max_tokens,
            )

            if i < len(self.config.theme_coder_identities):
                theme_config.identity = self.config.theme_coder_identities[i]

            # Theme coders also get independent copies
            theme_coder = ThemeCoderAgent(
                config=theme_config,
                codebook=self.codebook.copy(),
                research_context=self.config.research_context,
            )
            self._theme_coders.append(theme_coder)

        self._theme_aggregator = ThemeAggregatorAgent(
            config=self.config.theme_aggregator_config,
            embedding_service=self.embedding_service,
        )

    async def _code_segment_parallel(
        self, segment: DataSegment
    ) -> list[CodeAssignment]:
        """Code a single segment using all coders in parallel.

        Args:
            segment: The segment to code.

        Returns:
            List of CodeAssignments from all coders.
        """
        tasks = [
            coder.code_segment_async(segment.segment_id, segment.text)
            for coder in self._coders
        ]
        return list(await asyncio.gather(*tasks))

    def _code_segment_sequential(self, segment: DataSegment) -> list[CodeAssignment]:
        """Code a single segment using all coders sequentially.

        Args:
            segment: The segment to code.

        Returns:
            List of CodeAssignments from all coders.
        """
        return [
            coder.code_segment(segment.segment_id, segment.text)
            for coder in self._coders
        ]

    def _calculate_batch_metrics(
        self, assignments_per_segment: list[list[CodeAssignment]]
    ) -> IterationMetrics:
        """Calculate metrics for a batch of coding results.

        Args:
            assignments_per_segment: List of assignment lists per segment.

        Returns:
            IterationMetrics for the batch.
        """
        # Calculate agreement rate across coders
        code_sets = []
        for segment_assignments in assignments_per_segment:
            for assignment in segment_assignments:
                code_sets.append(set(assignment.codes))

        agreement_rate = calculate_agreement_rate(code_sets) if code_sets else 0.0

        return IterationMetrics(
            items_processed=len(assignments_per_segment),
            agreement_rate=agreement_rate,
        )

    def _maybe_checkpoint(
        self, batch_number: int, metrics: IterationMetrics
    ) -> HITLCheckpoint | None:
        """Create a HITL checkpoint if enabled and due.

        Args:
            batch_number: Current batch number.
            metrics: Current metrics.

        Returns:
            HITLCheckpoint if created, None otherwise.
        """
        if not self.config.enable_hitl_checkpoints:
            return None

        if batch_number % self.config.checkpoint_frequency != 0:
            return None

        checkpoint = HITLCheckpoint(
            stage="coding",
            batch_number=batch_number,
            codebook_snapshot=self.codebook.to_dict(),
            metrics=metrics.to_dict(),
            requires_approval=False,
        )
        self._checkpoints.append(checkpoint)
        return checkpoint

    async def _process_batch_stage1_async(
        self, segments: list[DataSegment], batch_number: int
    ) -> list[AggregationResult]:
        """Process a batch of segments through Stage 1 (async parallel).

        Args:
            segments: List of data segments to code.
            batch_number: Current batch number for checkpointing.

        Returns:
            List of aggregation results.
        """
        if not self._coders or not self._aggregator:
            self._init_stage1_agents()

        all_aggregations: list[AggregationResult] = []
        assignments_per_segment: list[list[CodeAssignment]] = []

        # Step 1: All coders process ALL segments in parallel
        # This matches paper: "Multiple coders work independently"
        for segment in segments:
            segment_assignments = await self._code_segment_parallel(segment)
            assignments_per_segment.append(segment_assignments)
            for i, assignment in enumerate(segment_assignments):
                self._dump(
                    f"stage1/batch_{batch_number:04d}/segment_{segment.segment_id}/coder_{i}.json",
                    assignment,
                )

        # Step 2: After ALL coders finish, aggregate ONCE per segment
        assert self._aggregator is not None
        assert self._reviewer is not None

        for segment, segment_assignments in zip(
            segments, assignments_per_segment, strict=True
        ):
            aggregation = self._aggregator.aggregate(segment_assignments)
            all_aggregations.append(aggregation)
            self._reviewer.process_aggregation_result(aggregation)
            self._dump(
                f"stage1/batch_{batch_number:04d}/segment_{segment.segment_id}/aggregation.json",
                aggregation,
            )

        self._dump(
            f"stage1/batch_{batch_number:04d}/codebook.json", self.codebook
        )

        # Calculate and record metrics
        metrics = self._calculate_batch_metrics(assignments_per_segment)
        self._maybe_checkpoint(batch_number, metrics)

        return all_aggregations

    def _process_batch_stage1_sync(
        self, segments: list[DataSegment], batch_number: int
    ) -> list[AggregationResult]:
        """Process a batch of segments through Stage 1 (sequential).

        Args:
            segments: List of data segments to code.
            batch_number: Current batch number for checkpointing.

        Returns:
            List of aggregation results.
        """
        if not self._coders or not self._aggregator:
            self._init_stage1_agents()

        all_aggregations: list[AggregationResult] = []
        assignments_per_segment: list[list[CodeAssignment]] = []

        for segment in segments:
            segment_assignments = self._code_segment_sequential(segment)
            assignments_per_segment.append(segment_assignments)
            for i, assignment in enumerate(segment_assignments):
                self._dump(
                    f"stage1/batch_{batch_number:04d}/segment_{segment.segment_id}/coder_{i}.json",
                    assignment,
                )

        assert self._aggregator is not None
        assert self._reviewer is not None

        for segment, segment_assignments in zip(
            segments, assignments_per_segment, strict=True
        ):
            aggregation = self._aggregator.aggregate(segment_assignments)
            all_aggregations.append(aggregation)
            self._reviewer.process_aggregation_result(aggregation)
            self._dump(
                f"stage1/batch_{batch_number:04d}/segment_{segment.segment_id}/aggregation.json",
                aggregation,
            )

        self._dump(
            f"stage1/batch_{batch_number:04d}/codebook.json", self.codebook
        )

        metrics = self._calculate_batch_metrics(assignments_per_segment)
        self._maybe_checkpoint(batch_number, metrics)

        return all_aggregations

    async def run_stage1_async(
        self, segments: list[DataSegment]
    ) -> tuple[Codebook, list[AggregationResult]]:
        """Run Stage 1: Coding (async parallel version).

        Args:
            segments: List of data segments to code.

        Returns:
            Tuple of (codebook, aggregation_results).
        """
        self._init_stage1_agents()
        all_aggregations: list[AggregationResult] = []

        batch_size = self.config.batch_size
        batch_number = 0

        for i in range(0, len(segments), batch_size):
            batch = segments[i : i + batch_size]
            batch_results = await self._process_batch_stage1_async(batch, batch_number)
            all_aggregations.extend(batch_results)
            batch_number += 1

        return self.codebook, all_aggregations

    def run_stage1(
        self, segments: list[DataSegment]
    ) -> tuple[Codebook, list[AggregationResult]]:
        """Run Stage 1: Coding.

        Uses parallel or sequential execution based on config.

        Args:
            segments: List of data segments to code.

        Returns:
            Tuple of (codebook, aggregation_results).
        """
        if self.config.execution_mode == ExecutionMode.PARALLEL:
            return asyncio.run(self.run_stage1_async(segments))

        # Sequential fallback
        self._init_stage1_agents()
        all_aggregations: list[AggregationResult] = []

        batch_size = self.config.batch_size
        batch_number = 0

        for i in range(0, len(segments), batch_size):
            batch = segments[i : i + batch_size]
            batch_results = self._process_batch_stage1_sync(batch, batch_number)
            all_aggregations.extend(batch_results)
            batch_number += 1

        return self.codebook, all_aggregations

    def run_stage2(self, codebook: Codebook | None = None) -> ThemeAggregationResult:
        """Run Stage 2: Theme Development.

        Args:
            codebook: Optional codebook to use. If None, uses pipeline's codebook.

        Returns:
            ThemeAggregationResult with final themes.
        """
        if codebook is not None:
            self.codebook = codebook

        self._init_stage2_agents()

        self._dump("stage2/input_codebook.json", self.codebook)

        all_theme_results: list[ThemeResult] = []
        for i, theme_coder in enumerate(self._theme_coders):
            theme_coder.codebook = self.codebook.copy()
            theme_result = theme_coder.develop_themes()
            all_theme_results.append(theme_result)
            self._dump(f"stage2/theme_coder_{i}.json", theme_result)

        assert self._theme_aggregator is not None
        aggregated = self._theme_aggregator.aggregate(all_theme_results)
        self._dump("stage2/aggregation.json", aggregated)
        return aggregated

    def _run_evaluation(self, result: PipelineResult) -> EvaluationResult:
        """Run configured evaluations.

        Args:
            result: The pipeline result to evaluate.

        Returns:
            EvaluationResult with scores.
        """
        eval_result = EvaluationResult()
        eval_config = self.config.evaluation_config

        if not eval_config:
            return eval_result

        if eval_config.evaluate_credibility:
            from thematic_analysis.evaluation.credibility import (
                CredibilityConfig,
                EvaluatorAgent,
            )

            evaluator = EvaluatorAgent(
                CredibilityConfig(model=eval_config.credibility_model)
            )
            cred_result = evaluator.evaluate_from_pipeline_result(result)
            eval_result.credibility_score = cred_result.overall_score

        return eval_result

    def run(self, segments: list[DataSegment]) -> PipelineResult:
        """Run the full two-stage pipeline.

        Args:
            segments: List of data segments to analyze.

        Returns:
            PipelineResult with themes, codebook, and intermediate results.
        """
        # Reset checkpoints
        self._checkpoints = []

        # Stage 1: Coding
        codebook, aggregations = self.run_stage1(segments)

        # Stage 2: Theme Development
        themes = self.run_stage2(codebook)

        result = PipelineResult(
            themes=themes,
            codebook=codebook,
            stage1_aggregations=aggregations,
            checkpoints=self._checkpoints,
            metrics={
                "num_segments": len(segments),
                "num_coders": self.config.num_coders,
                "num_theme_coders": self.config.num_theme_coders,
                "execution_mode": self.config.execution_mode.value,
                "negotiation_strategy": self.config.negotiation_strategy.value,
            },
        )

        # Run evaluation if configured
        if self.config.evaluation_config:
            result.evaluation = self._run_evaluation(result)

        self._dump("final_result.json", result)
        return result

    async def run_async(self, segments: list[DataSegment]) -> PipelineResult:
        """Run the full two-stage pipeline (async version).

        Args:
            segments: List of data segments to analyze.

        Returns:
            PipelineResult with themes, codebook, and intermediate results.
        """
        self._checkpoints = []

        codebook, aggregations = await self.run_stage1_async(segments)
        themes = self.run_stage2(codebook)

        result = PipelineResult(
            themes=themes,
            codebook=codebook,
            stage1_aggregations=aggregations,
            checkpoints=self._checkpoints,
            metrics={
                "num_segments": len(segments),
                "num_coders": self.config.num_coders,
                "num_theme_coders": self.config.num_theme_coders,
                "execution_mode": "parallel_async",
                "negotiation_strategy": self.config.negotiation_strategy.value,
            },
        )

        if self.config.evaluation_config:
            result.evaluation = self._run_evaluation(result)

        return result

    def run_from_texts(self, texts: list[str]) -> PipelineResult:
        """Run pipeline from a list of text strings.

        Args:
            texts: List of text strings to analyze.

        Returns:
            PipelineResult with themes and codebook.
        """
        segments = [
            DataSegment(
                segment_id=f"seg_{uuid.uuid4().hex[:8]}",
                text=text,
            )
            for text in texts
        ]
        return self.run(segments)

    def run_from_directory(
        self,
        directory: str,
        pattern: str = "*.pdf",
        segmentation: str = "paragraph",
        min_words: int = 20,
        max_words: int = 500,
        recursive: bool = False,
    ) -> PipelineResult:
        """Run pipeline on all documents in a directory.

        Supports PDF, TXT, and MD files. Documents are loaded and segmented
        automatically for thematic analysis.

        Args:
            directory: Path to directory containing documents.
            pattern: Glob pattern for files (default: "*.pdf").
            segmentation: Method to split docs ('paragraph', 'sentence', 'fixed').
            min_words: Minimum words per segment.
            max_words: Maximum words per segment (for 'fixed' method).
            recursive: Whether to search subdirectories.

        Returns:
            PipelineResult with themes and codebook.

        Example:
            >>> pipeline = ThematicLMPipeline()
            >>> result = pipeline.run_from_directory("/path/to/pdfs")
            >>> print(result.themes)
        """
        from thematic_analysis.loaders import documents_to_segments, load_directory

        print(f"Loading documents from {directory} (pattern: {pattern})...")
        documents = load_directory(directory, pattern=pattern, recursive=recursive)
        print(f"Loaded {len(documents)} documents")

        for doc in documents:
            print(f"  - {doc.metadata.filename}: {doc.metadata.word_count} words")

        print(f"\nSegmenting documents (method: {segmentation})...")
        segments = documents_to_segments(
            documents,
            method=segmentation,
            min_words=min_words,
            max_words=max_words,
        )
        print(f"Created {len(segments)} segments for analysis")

        return self.run(segments)

    def run_from_pdf(
        self,
        path: str,
        segmentation: str = "paragraph",
        min_words: int = 20,
    ) -> PipelineResult:
        """Run pipeline on a single PDF file.

        Args:
            path: Path to PDF file.
            segmentation: Method to split doc ('paragraph', 'sentence', 'fixed').
            min_words: Minimum words per segment.

        Returns:
            PipelineResult with themes and codebook.
        """
        from thematic_analysis.loaders import load_pdf

        print(f"Loading PDF: {path}")
        doc = load_pdf(path)
        words = doc.metadata.word_count
        pages = doc.metadata.page_count
        print(f"Loaded {words} words from {pages} pages")

        segments = doc.segment(method=segmentation, min_words=min_words)
        print(f"Created {len(segments)} segments for analysis")

        return self.run(segments)

    @property
    def coders(self) -> list[CoderAgent]:
        """Get the list of coder agents."""
        if not self._coders:
            self._init_stage1_agents()
        return self._coders

    @property
    def theme_coders(self) -> list[ThemeCoderAgent]:
        """Get the list of theme coder agents."""
        if not self._theme_coders:
            self._init_stage2_agents()
        return self._theme_coders
