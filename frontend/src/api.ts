// Thin fetch wrapper around the ta-web REST API.

export type Status = {
  db_path: string;
  db_size_bytes: number;
  research_context_set: boolean;
  research_context_description: string;
  latest_research_context_version: number | null;
  stage1: {
    documents_total: number;
    segments_total: number;
    segments_by_status: Record<string, number>;
    codes_total: number;
    quotes_total: number;
    themes_total: number;
    coders_total: number;
    coder_runs_total: number;
    coder_runs_by_status: Record<string, number>;
    coding_queue_total: number;
    coding_queue_by_status: Record<string, number>;
    aggregations_total: number;
    aggregations_by_status: Record<string, number>;
    review_decisions_total: number;
    review_decisions_applied: number;
    codebook_version: number;
    codebook_codes: number;
    documents_by_coding_status: Record<string, number>;
    reviews_pending: number;
    reviews_completed_since_codebook: number;
  };
  per_coder: Array<{
    coder_id: string;
    identity: string;
    segments_total: number;
    runs_done: number;
    runs_running: number;
    runs_failed: number;
  }>;
};

export type CodebookPreviewSource = {
  code_id: number;
  code: string | null;
  coder_id: number | null;
  decision: string | null;
  rationale: string | null;
  segment_id: number | null;
  document_id: number | null;
  document_filename: string | null;
};

export type CodebookPreviewAdded = {
  code_id: number;
  code: string;
  description: string;
  coder_id: number;
  n_quotes: number;
  change: "new" | "merge" | "added";
  sources: CodebookPreviewSource[];
};

export type CodebookPreviewRemoved = {
  code_id: number;
  code: string;
  description: string;
  coder_id: number;
  n_quotes: number;
};

export type CodebookPreview = {
  parent_version: number | null;
  added: CodebookPreviewAdded[];
  removed: CodebookPreviewRemoved[];
  unchanged_count: number;
  has_changes: boolean;
};

export type RecentCode = {
  code_id: number;
  code: string;
  description: string;
  coder_id: number;
  coder_identity: string | null;
  kind: "coder" | "aggregation" | "review";
  codebook_used_id: number;
  segment_id: number | null;
  document_id: number | null;
  document_filename: string | null;
};

export type ResearchContext = {
  description: string;
  tailored_prompts: Record<string, string>;
  roles?: string[];
};

export type Segment = {
  segment_id: string;
  batch: number | null;
  status: string;
  title: string | null;
  document_id: number | null;
  preview: string;
  len: number;
};

export type DocumentCoderProgress = {
  coder_id: string;
  runs_done: number;
  runs_running: number;
  runs_failed: number;
};

export type QueueState = "none" | "earlier" | "latest";

export type Document = {
  document_id: number;
  filename: string;
  created_at: string;
  size_bytes: number;
  segments_total: number;
  per_coder: DocumentCoderProgress[];
  aggregations_by_status: Record<string, number>;
  queue_state: QueueState;
};

export type DocumentSegment = {
  segment_id: string;
  title: string | null;
  status: string;
  text: string;
  batch: number | null;
  len: number;
  coder_runs: CoderRun[];
  aggregation: SegmentDetail["aggregation"];
  aggregated_codes: AggregatedCode[];
  queue_state: QueueState;
};

export type DocumentDetail = {
  document_id: number;
  filename: string;
  created_at: string;
  size_bytes: number;
  segments: DocumentSegment[];
};

export type CoderCode = {
  position: number;
  code_id?: number;
  code: string;
  rationale: string | null;
  description?: string | null;
  is_new: 0 | 1 | null;
  quotes?: Array<{ quote_id: string; text: string }>;
};

export type CoderRun = {
  id: number;
  segment_id: string;
  coder_id: string;
  codebook_version: number;
  status: string;
  claimed_at: string | null;
  finished_at: string | null;
  error: string | null;
  raw_response?: string | null;
  codes: CoderCode[];
};

export type AggregatedCode = {
  id: number;
  code: string;
  codebook_used_id?: number;
  quotes: Array<{ quote_id: string; text: string }>;
  source_coders: string[];
  review: null | {
    id: number;
    decision: string;
    target_code: string | null;
    rationale: string | null;
    applied: number;
    resulting_version: number | null;
    created_at: string;
  };
};

export type SegmentDetail = {
  segment_id: string;
  text: string;
  title: string | null;
  document_id: number | null;
  document_filename: string | null;
  batch: number | null;
  status: string;
  coder_runs: CoderRun[];
  aggregation: null | {
    id: number;
    status: string;
    created_at: string;
    finished_at: string | null;
    error: string | null;
  };
  aggregated_codes: AggregatedCode[];
  queue_state: QueueState;
};

// The shape `/api/segments/{id}` and `/api/documents/{id}.segments[]` actually
// return today — different field names + flatter nesting than `SegmentDetail`.
// Kept narrow + close to the backend payload so the adapter below is the only
// place that needs to know about the gap.
export type ApiSegmentPayload = {
  segment_id: number;
  document_id: number | null;
  document_filename: string | null;
  title: string | null;
  content: string;
  line_from: number;
  line_to: number;
  position: number;
  status: string;
  coder_codes: Array<{
    coder_id: number;
    status: string;
    codebook_version: number;
    finished_at: string | null;
    no_codes: boolean;
    codes: Array<{
      code_id: number;
      code: string;
      rationale: string | null;
      description: string | null;
      quotes?: Array<{ quote_id: string; text: string }>;
    }>;
  }>;
  aggregator_codes: Array<{
    code_id: number;
    code: string;
    description: string | null;
    rationale: string | null;
    codebook_used_id?: number;
    quotes: Array<{ quote_id: string; text: string }>;
    review: null | {
      new_code_id: number;
      decision: string;
      rationale: string | null;
    };
  }>;
  aggregator_no_codes: boolean;
  queue_state: QueueState;
};

export function adaptSegmentPayload(p: ApiSegmentPayload): SegmentDetail {
  const segIdStr = String(p.segment_id);
  const coder_runs: CoderRun[] = p.coder_codes.map((cc) => ({
    id: Number(`${p.segment_id}${cc.coder_id}`),
    segment_id: segIdStr,
    coder_id: String(cc.coder_id),
    codebook_version: cc.codebook_version,
    status: cc.status,
    claimed_at: null,
    finished_at: cc.finished_at,
    error: null,
    codes: cc.codes.map((c, i) => ({
      position: i,
      code_id: c.code_id,
      code: c.code,
      rationale: c.rationale,
      description: c.description,
      is_new: null,
      quotes: c.quotes ?? [],
    })),
  }));
  const aggregated_codes: AggregatedCode[] = p.aggregator_codes.map((ac) => ({
    id: ac.code_id,
    code: ac.code,
    codebook_used_id: ac.codebook_used_id,
    quotes: ac.quotes,
    source_coders: [],
    review: ac.review
      ? {
          id: ac.review.new_code_id,
          decision: ac.review.decision,
          target_code: null,
          rationale: ac.review.rationale,
          applied: 0,
          resulting_version: null,
          created_at: "",
        }
      : null,
  }));
  return {
    segment_id: segIdStr,
    text: p.content,
    title: p.title,
    document_id: p.document_id,
    document_filename: p.document_filename,
    batch: null,
    status: p.status,
    coder_runs,
    aggregation:
      aggregated_codes.length > 0
        ? {
            id: p.segment_id,
            status: "done",
            created_at: "",
            finished_at: null,
            error: null,
          }
        : null,
    aggregated_codes,
    queue_state: p.queue_state ?? "none",
  };
}

export type CodebookVersionMeta = {
  version: number;
  parent_version: number | null;
  research_context_version: number;
  created_at: string;
  n_codes: number;
};

export type CodebookQuote = {
  quote_id: number;
  text: string;
  segment_id: number;
  document_id: number | null;
  document_filename: string | null;
};

export type CodebookChangeTag = "new" | "renamed" | "new_quotes";

export type CodebookCode = {
  code_id: number;
  code: string;
  description: string;
  rationale: string;
  coder_id: number;
  quotes: CodebookQuote[];
  change_tags: CodebookChangeTag[];
};

export type CodebookVersionDetail = {
  version: number;
  parent_version: number | null;
  research_context_version: number;
  created_at: string;
  n_codes: number;
  codes: CodebookCode[];
};

export type CodeDerivationSource = {
  code_id: number;
  derivation_type: "A" | "R";
  decision: "A" | "M" | "U" | null;
  rationale: string | null;
  code: string | null;
  description: string | null;
  coder_id: number | null;
  coder_identity: string | null;
  codebook_used_id: number | null;
  segment_id: number | null;
  document_id: number | null;
  document_filename: string | null;
  has_more_sources: boolean;
};

export type CodeDetail = {
  code_id: number;
  code: string;
  description: string;
  rationale: string;
  coder_id: number;
  coder_identity: string | null;
  codebook_used_id: number;
  segment_id: number | null;
  segment: null | {
    segment_id: number;
    title: string | null;
    document_id: number;
    document_filename: string | null;
    line_from: number;
    line_to: number;
    preview: string;
  };
  quotes: CodebookQuote[];
  derivation_sources: CodeDerivationSource[];
  in_codebook_versions: number[];
};

export type Coder = {
  coder_id: string;
  identity: string;
  created_at: string;
};

export type ThemeSource = "job" | "aggregator" | "manual";

export type ThemeSummary = {
  theme_id: number;
  title: string;
  description: string;
  rationale: string;
  source: ThemeSource;
  theme_coding_job_id: number | null;
  codebook_used_id: number | null;
  deleted: boolean;
  created_at: string | null;
  n_codes: number;
  n_quotes: number;
};

export type ThemeCodeRef = {
  code_id: number;
  code: string;
  description: string;
  coder_id: number;
  n_quotes: number;
};

export type ThemeQuoteRef = {
  quote_id: number;
  text: string;
  segment_id: number | null;
  document_id: number | null;
  document_filename: string | null;
};

export type ThemeFull = ThemeSummary & {
  codes: ThemeCodeRef[];
  quotes: ThemeQuoteRef[];
};

export type ThemeDetail = ThemeFull & {
  derived_from: ThemeSummary[];
  derived_into: ThemeSummary[];
};

export type ThemeCodingJobRunStatus =
  | "not_run"
  | "running"
  | "no_themes"
  | "has_themes";

export type ThemeCodingJobSummary = {
  id: number;
  codebook_used_id: number;
  prompt: string;
  created_at: string | null;
  n_themes: number;
  n_themes_active: number;
  run_status: ThemeCodingJobRunStatus;
};

export type BackgroundRunnerStatus = {
  running: boolean;
  workers: number;
  use_mock_embeddings: boolean;
  batch_size: number;
  started_at: number | null;
  counters: {
    coded: number;
    aggregated: number;
    reviewed: number;
    failed: number;
  };
  last_event: null | {
    kind: "coded" | "aggregated" | "reviewed";
    ok: boolean;
    at: number;
    [k: string]: unknown;
  };
  last_error: string | null;
  llm_configured: boolean;
  llm_unavailable_reason: string | null;
};

export type ThemeCodingJobDetail = {
  id: number;
  codebook_used_id: number;
  prompt: string;
  created_at: string | null;
  run_status: ThemeCodingJobRunStatus;
  themes: ThemeFull[];
};

async function jsonFetch<T>(
  url: string,
  init?: RequestInit,
): Promise<T> {
  const res = await fetch(url, {
    headers: { "Content-Type": "application/json" },
    ...init,
  });
  if (!res.ok) {
    let detail = `${res.status} ${res.statusText}`;
    try {
      const body = await res.json();
      if (body && typeof body.detail === "string") detail = body.detail;
    } catch {
      /* ignore */
    }
    throw new Error(detail);
  }
  if (res.status === 204) return undefined as T;
  return (await res.json()) as T;
}

export const api = {
  status: () => jsonFetch<Status>("/api/status"),
  recentCodes: (limit: number = 20) =>
    jsonFetch<RecentCode[]>(`/api/recent-codes?limit=${limit}`),
  codebookPreview: () => jsonFetch<CodebookPreview>("/api/codebook-preview"),

  researchContext: () =>
    jsonFetch<ResearchContext | null>("/api/research-context"),
  researchContextVersion: (v: number) =>
    jsonFetch<ResearchContext & { research_context_version: number }>(
      `/api/research-context/versions/${v}`,
    ),
  putResearchContext: (body: {
    description: string;
    tailored_prompts: Record<string, string>;
  }) =>
    jsonFetch<{
      status: string;
      description: string;
      tailored_prompts: Record<string, string>;
    }>("/api/research-context", {
      method: "PUT",
      body: JSON.stringify(body),
    }),
  regenerateTailoredPrompts: () =>
    jsonFetch<{
      description: string;
      tailored_prompts: Record<string, string>;
      roles: string[];
    }>("/api/research-context/regenerate-prompts", { method: "POST" }),

  segments: (params: {
    status?: string;
    batch?: number;
    q?: string;
    limit?: number;
    offset?: number;
  }) => {
    const qs = new URLSearchParams();
    for (const [k, v] of Object.entries(params)) {
      if (v !== undefined && v !== null && v !== "")
        qs.set(k, String(v));
    }
    return jsonFetch<{
      total: number;
      items: Segment[];
      status_counts: Record<string, number>;
    }>(`/api/segments?${qs.toString()}`);
  },
  segment: (id: string) =>
    jsonFetch<ApiSegmentPayload>(`/api/segments/${id}`).then(adaptSegmentPayload),
  deleteSegment: (id: string) =>
    jsonFetch(`/api/segments/${id}`, { method: "DELETE" }),

  documents: () =>
    jsonFetch<{ items: Document[]; coder_ids: string[] }>("/api/documents"),
  document: (id: number) =>
    jsonFetch<{
      document_id: number;
      filename: string;
      created_at: string;
      segments: ApiSegmentPayload[];
    }>(`/api/documents/${id}`).then((d) => ({
      document_id: d.document_id,
      filename: d.filename,
      created_at: d.created_at,
      size_bytes: 0,
      segments: d.segments.map((s) => {
        const adapted = adaptSegmentPayload(s);
        return {
          ...adapted,
          len: s.content.length,
        };
      }),
    })),
  deleteDocument: (id: number) =>
    jsonFetch(`/api/documents/${id}`, { method: "DELETE" }),
  enqueueDocument: (id: number) =>
    jsonFetch<{ enqueued: number }>(
      `/api/documents/${id}/enqueue`,
      { method: "POST", body: JSON.stringify({}) },
    ),
  enqueueRandomDocuments: (n: number = 10) =>
    jsonFetch<{ document_ids: number[]; enqueued: number }>(
      `/api/documents/enqueue-random?n=${n}`,
      { method: "POST", body: JSON.stringify({}) },
    ),
  enqueueSegment: (id: number | string) =>
    jsonFetch<{ enqueued: number }>(
      `/api/segments/${id}/enqueue`,
      { method: "POST", body: JSON.stringify({}) },
    ),

  coders: () => jsonFetch<Coder[]>("/api/coders"),
  addCoder: (identity: string) =>
    jsonFetch("/api/coders", {
      method: "POST",
      body: JSON.stringify({ identity }),
    }),
  deleteCoder: (id: string, force: boolean) =>
    jsonFetch(`/api/coders/${id}?force=${force}`, { method: "DELETE" }),

  coderRuns: (params: {
    coder_id?: string;
    status?: string;
    limit?: number;
    offset?: number;
  }) => {
    const qs = new URLSearchParams();
    for (const [k, v] of Object.entries(params)) {
      if (v !== undefined && v !== null && v !== "")
        qs.set(k, String(v));
    }
    return jsonFetch<{
      total: number;
      items: Array<{
        id: number;
        segment_id: string;
        coder_id: string;
        codebook_version: number;
        status: string;
        claimed_at: string | null;
        finished_at: string | null;
        error: string | null;
        n_codes: number;
      }>;
    }>(`/api/coder-runs?${qs.toString()}`);
  },
  deleteCoderRun: (id: number) =>
    jsonFetch(`/api/coder-runs/${id}`, { method: "DELETE" }),
  editCoderCode: (run_id: number, position: number, code: string) =>
    jsonFetch(`/api/coder-codes/${run_id}/${position}`, {
      method: "PATCH",
      body: JSON.stringify({ code }),
    }),

  aggregations: (params: {
    status?: string;
    limit?: number;
    offset?: number;
  }) => {
    const qs = new URLSearchParams();
    for (const [k, v] of Object.entries(params)) {
      if (v !== undefined && v !== null && v !== "")
        qs.set(k, String(v));
    }
    return jsonFetch<{
      total: number;
      items: Array<{
        id: number;
        segment_id: string;
        status: string;
        created_at: string;
        finished_at: string | null;
        error: string | null;
        n_codes: number;
      }>;
    }>(`/api/aggregations?${qs.toString()}`);
  },
  deleteAggregation: (id: number) =>
    jsonFetch(`/api/aggregations/${id}`, { method: "DELETE" }),
  editAggregatedCode: (id: number, code: string) =>
    jsonFetch(`/api/aggregated-codes/${id}`, {
      method: "PATCH",
      body: JSON.stringify({ code }),
    }),
  deleteAggregatedCode: (id: number) =>
    jsonFetch(`/api/aggregated-codes/${id}`, { method: "DELETE" }),

  reviewDecisions: (params: {
    decision?: string;
    limit?: number;
    offset?: number;
  }) => {
    const qs = new URLSearchParams();
    for (const [k, v] of Object.entries(params)) {
      if (v !== undefined && v !== null && v !== "")
        qs.set(k, String(v));
    }
    return jsonFetch<{
      total: number;
      items: Array<{
        id: number;
        aggregated_code_id: number;
        decision: string;
        target_code: string | null;
        rationale: string | null;
        applied: number;
        resulting_version: number | null;
        created_at: string;
        aggregated_code: string;
        segment_id: string;
      }>;
    }>(`/api/review-decisions?${qs.toString()}`);
  },
  deleteReviewDecision: (id: number) =>
    jsonFetch(`/api/review-decisions/${id}`, { method: "DELETE" }),

  codebookVersions: () =>
    jsonFetch<CodebookVersionMeta[]>("/api/codebook/versions"),
  codebookVersion: (v: number) =>
    jsonFetch<CodebookVersionDetail>(`/api/codebook/versions/${v}`),

  code: (id: number) => jsonFetch<CodeDetail>(`/api/codes/${id}`),

  themeCodingJobs: () =>
    jsonFetch<ThemeCodingJobSummary[]>("/api/theme-coding-jobs"),
  createThemeCodingJob: (body: { codebook_version: number; prompt: string }) =>
    jsonFetch<{
      id: number;
      codebook_used_id: number;
      prompt: string;
      created_at: string | null;
      run_status: ThemeCodingJobRunStatus;
    }>("/api/theme-coding-jobs", {
      method: "POST",
      body: JSON.stringify(body),
    }),
  runThemeCodingJob: (id: number) =>
    jsonFetch<{
      id: number;
      n_themes: number;
      run_status: ThemeCodingJobRunStatus;
    }>(`/api/theme-coding-jobs/${id}/run`, {
      method: "POST",
      body: JSON.stringify({}),
    }),
  runPendingThemeCodingJobs: () =>
    jsonFetch<{
      n_jobs_run: number;
      n_themes: number;
      n_jobs_empty: number;
    }>("/api/theme-coding-jobs/run-pending", {
      method: "POST",
      body: JSON.stringify({}),
    }),
  themeCodingJob: (id: number) =>
    jsonFetch<ThemeCodingJobDetail>(`/api/theme-coding-jobs/${id}`),
  themeCoderSystemPrompt: () =>
    jsonFetch<{
      system_prompt: string;
      user_framing_template: string;
      user_codebook_template: string;
    }>("/api/theme-coding-jobs/meta/system-prompt"),

  themes: () => jsonFetch<ThemeFull[]>("/api/themes"),
  theme: (id: number) => jsonFetch<ThemeDetail>(`/api/themes/${id}`),
  createManualTheme: (body: {
    title: string;
    description: string;
    rationale: string;
  }) =>
    jsonFetch<ThemeSummary>("/api/themes", {
      method: "POST",
      body: JSON.stringify(body),
    }),
  deleteTheme: (id: number) =>
    jsonFetch<{ deleted: boolean }>(`/api/themes/${id}`, {
      method: "DELETE",
    }),
  restoreTheme: (id: number) =>
    jsonFetch<{ deleted: boolean }>(`/api/themes/${id}/restore`, {
      method: "POST",
    }),
  similarCodes: (id: number, top_k: number = 30) =>
    jsonFetch<{
      codebook_version: number | null;
      items: Array<{
        code_id: number;
        code: string;
        description: string;
        coder_id: number;
        similarity: number;
        segment_id: number | null;
        document_id: number | null;
        n_quotes: number;
      }>;
    }>(`/api/codes/${id}/similar?top_k=${top_k}`),
  mergeCodes: (id: number, selected_code_ids: number[]) =>
    jsonFetch<{ new_code_id: number; new_codebook_version: number }>(
      `/api/codes/${id}/merge`,
      {
        method: "POST",
        body: JSON.stringify({ selected_code_ids }),
      },
    ),
  quote: (id: number) => jsonFetch<CodebookQuote>(`/api/quotes/${id}`),

  backgroundRunner: () =>
    jsonFetch<BackgroundRunnerStatus>("/api/background-runner"),
  startBackgroundRunner: (body: {
    workers: number;
    use_mock_embeddings?: boolean;
  }) =>
    jsonFetch<BackgroundRunnerStatus & { started: boolean }>(
      "/api/background-runner/start",
      { method: "POST", body: JSON.stringify(body) },
    ),
  stopBackgroundRunner: () =>
    jsonFetch<BackgroundRunnerStatus & { stopped: boolean }>(
      "/api/background-runner/stop",
      { method: "POST", body: JSON.stringify({}) },
    ),
};
