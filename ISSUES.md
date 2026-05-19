# Thematic-LM Implementation Issues

This document contains the implementation checklist for Thematic-LM, based on the paper:
"Thematic-LM: A LLM-based Multi-agent System for Large-scale Thematic Analysis" (WWW '25)

## Core Agent Components

### Issue 1: Implement Coder Agent
**Priority:** High
**Description:**
Implement the Coder Agent that analyzes text data and outputs codes with corresponding quotes.

**Requirements:**
- Accept text data (social media posts) as input
- Generate 1-3 codes per piece of data capturing concepts/ideas with analytical interest
- Extract a representative quote from the data for each code
- Output codes, quotes, and quote IDs in structured format
- Support custom identity perspectives via system message

**Acceptance Criteria:**
- [x] CoderAgent class with configurable identity
- [x] Proper prompt template for coding instructions
- [x] JSON output format with codes, quotes, and quote_ids
- [x] Unit tests for coder agent

---

### Issue 2: Implement Code Aggregator Agent
**Priority:** High
**Description:**
Implement the Code Aggregator Agent that merges and organizes codes from multiple coder agents.

**Requirements:**
- Accept codes and quotes from multiple coder agents
- Merge codes with similar meanings
- Retain codes that represent different concepts
- Store quotes under merged codes
- Keep top K most relevant quotes per code
- Output structured JSON format

**Acceptance Criteria:**
- [ ] CodeAggregatorAgent class
- [ ] Code merging logic
- [ ] Quote organization with top-K selection
- [ ] JSON output format
- [ ] Unit tests

---

### Issue 3: Implement Reviewer Agent with Adaptive Codebook
**Priority:** High
**Description:**
Implement the Reviewer Agent that maintains and updates the adaptive codebook during the coding stage.

**Requirements:**
- Maintain codebook storing codes, quotes, and quote IDs in JSON format
- Generate code embeddings using Sentence Transformer
- Retrieve top-k similar codes using cosine similarity
- Compare new codes with existing similar codes
- Decide whether to merge or update codes
- Update codebook with new codes and merged codes

**Acceptance Criteria:**
- [ ] ReviewerAgent class
- [ ] Codebook data structure (JSON format)
- [ ] Sentence Transformer embedding generation
- [ ] Cosine similarity-based retrieval
- [ ] Code merging and updating logic
- [ ] Unit tests for reviewer and codebook

---

### Issue 4: Implement Theme Coder Agent
**Priority:** High
**Description:**
Implement the Theme Coder Agent that develops themes from the finalized codebook.

**Requirements:**
- Accept complete codebook as input
- Analyze codes and associated quotes holistically
- Identify overarching themes reflecting deeper insights
- Write description for each theme
- Keep top K most relevant quotes per theme (max 10)
- Support codebook compression (optional LLMLingua integration)

**Acceptance Criteria:**
- [ ] ThemeCoderAgent class
- [ ] Theme identification from codebook
- [ ] Theme description generation
- [ ] Quote selection per theme
- [ ] JSON output format
- [ ] Unit tests

---

### Issue 5: Implement Theme Aggregator Agent
**Priority:** High
**Description:**
Implement the Theme Aggregator Agent that refines and organizes themes from multiple theme coders.

**Requirements:**
- Accept themes from multiple theme coder agents
- Merge similar themes
- Refine theme descriptions
- Organize final themes with quotes
- Output final themes in JSON format

**Acceptance Criteria:**
- [ ] ThemeAggregatorAgent class
- [ ] Theme merging logic
- [ ] Description refinement
- [ ] Final JSON output
- [ ] Unit tests

---

## Pipeline and Orchestration

### Issue 6: Implement Two-Stage Pipeline Orchestration
**Priority:** High
**Description:**
Implement the two-stage pipeline orchestrating the coding and theme development stages.

**Requirements:**
- Stage 1 (Coding): Data → Multiple Coders → Aggregator → Reviewer → Codebook
- Stage 2 (Theme Development): Codebook → Multiple Theme Coders → Theme Aggregator → Final Themes
- Support configurable number of coders (1, 2, 5, etc.)
- Support batch processing for large datasets
- Maintain quote IDs throughout the pipeline

**Acceptance Criteria:**
- [ ] ThematicLMPipeline class
- [ ] Configurable number of coders
- [ ] Batch processing support
- [ ] Quote ID tracking
- [ ] End-to-end pipeline test

---

## Identity Perspectives

### Issue 7: Implement Coder Identity Perspectives
**Priority:** Medium
**Description:**
Implement the identity perspective system for coder agents to simulate diverse viewpoints.

**Requirements:**
- Support predefined identity perspectives:
  - Human-Driven Climate Change Agent
  - Natural Climate Change Agent  
  - Progressive View Agent
  - Conservative View Agent
  - Indigenous View Agent
- Allow custom identity definitions
- Integrate identity into agent system message
- Instruct agents to interpret data through assigned identity lens

**Acceptance Criteria:**
- [ ] IdentityPerspective class/configuration
- [ ] Predefined identity templates
- [ ] Custom identity support
- [ ] Identity integration with CoderAgent
- [ ] Tests for identity-based coding

---

## Evaluation Framework

### Issue 8: Implement Credibility & Confirmability Evaluation
**Priority:** Medium
**Description:**
Implement LLM-as-judge evaluation for credibility and confirmability of themes.

**Requirements:**
- Retrieve associated data through quote IDs
- Use evaluator agent to check theme-data consistency
- Compute percentage of quoted data consistent with themes
- Identify potential hallucinations or biases

**Acceptance Criteria:**
- [ ] EvaluatorAgent class
- [ ] Theme-data consistency checking
- [ ] Credibility/Confirmability score computation
- [ ] Unit tests

---

### Issue 9: Implement Dependability Evaluation
**Priority:** Medium
**Description:**
Implement dependability evaluation using inter-rater reliability via ROUGE scores.

**Requirements:**
- Support repeated TA runs
- Compute pairwise ROUGE-1 and ROUGE-2 scores between theme sets
- Calculate average ROUGE scores for dependability metric
- Support comparison of codes and themes

**Acceptance Criteria:**
- [ ] ROUGE score computation
- [ ] Pairwise comparison logic
- [ ] Dependability score aggregation
- [ ] Unit tests

---

### Issue 10: Implement Transferability Evaluation
**Priority:** Medium
**Description:**
Implement transferability evaluation by measuring theme generalization across dataset splits.

**Requirements:**
- Split dataset into training and validation sets
- Perform TA independently on both sets
- Compute overlap between themes via ROUGE scores
- Assess theme generalization capability

**Acceptance Criteria:**
- [ ] Dataset splitting utility
- [ ] Independent TA on splits
- [ ] Transferability score computation
- [ ] Unit tests

---

## Configuration and Utilities

### Issue 11: Implement Sentence Embedding with Sentence Transformer
**Priority:** High
**Description:**
Implement code embedding generation using Sentence Transformer for similarity-based retrieval.

**Requirements:**
- Load Sentence Transformer model
- Generate embeddings for codes
- Support cosine similarity computation
- Efficient batch embedding generation

**Acceptance Criteria:**
- [ ] EmbeddingService class
- [ ] Sentence Transformer integration
- [ ] Cosine similarity helper
- [ ] Unit tests

---

### Issue 12: Implement Codebook Data Structure
**Priority:** High
**Description:**
Implement the adaptive codebook data structure for storing and managing codes.

**Requirements:**
- JSON-based storage format
- Support for codes, quotes, quote IDs
- Embedding storage for similarity retrieval
- Top-k similar code retrieval
- Code merging operations
- Persistence (save/load)

**Acceptance Criteria:**
- [ ] Codebook class
- [ ] CRUD operations for codes
- [ ] Similarity-based retrieval
- [ ] Persistence support
- [ ] Unit tests

---

### Issue 13: Add CLI Interface
**Priority:** Low
**Description:**
Add a command-line interface for running thematic analysis.

**Requirements:**
- Accept input data file (CSV/JSON)
- Configurable number of coders
- Optional identity perspectives
- Output themes to file
- Progress reporting

**Acceptance Criteria:**
- [ ] CLI entry point
- [ ] Configuration options
- [ ] File I/O
- [ ] Progress display

---

### Issue 14: Add Documentation and Examples
**Priority:** Low
**Description:**
Add comprehensive documentation and example usage.

**Requirements:**
- README with installation and usage instructions
- API documentation
- Example scripts for:
  - Basic thematic analysis
  - Multi-coder analysis
  - Identity perspective analysis
  - Evaluation

**Acceptance Criteria:**
- [ ] README.md
- [ ] API docs
- [ ] Example scripts
- [ ] Usage guide

---

## Summary Checklist

| Issue | Component | Priority | Status |
|-------|-----------|----------|--------|
| 1 | Coder Agent | High | ✅ |
| 2 | Code Aggregator Agent | High | ⬜ |
| 3 | Reviewer Agent & Codebook | High | ⬜ |
| 4 | Theme Coder Agent | High | ⬜ |
| 5 | Theme Aggregator Agent | High | ⬜ |
| 6 | Pipeline Orchestration | High | ⬜ |
| 7 | Identity Perspectives | Medium | ⬜ |
| 8 | Credibility Evaluation | Medium | ⬜ |
| 9 | Dependability Evaluation | Medium | ⬜ |
| 10 | Transferability Evaluation | Medium | ⬜ |
| 11 | Sentence Embeddings | High | ⬜ |
| 12 | Codebook Data Structure | High | ⬜ |
| 13 | CLI Interface | Low | ⬜ |
| 14 | Documentation | Low | ⬜ |
