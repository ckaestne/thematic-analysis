import {
  ActionIcon,
  Alert,
  Anchor,
  Badge,
  Button,
  Card,
  Code,
  Collapse,
  Divider,
  Group,
  Modal,
  Stack,
  Tabs,
  Text,
  TextInput,
  Tooltip,
  UnstyledButton,
} from "@mantine/core";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import {
  IconAlertTriangle,
  IconChevronDown,
  IconChevronRight,
  IconEdit,
  IconExternalLink,
  IconTrash,
} from "@tabler/icons-react";
import {
  useMemo,
  useState,
  type CSSProperties,
  type Dispatch,
  type ReactNode,
  type SetStateAction,
} from "react";
import { Link } from "react-router-dom";
import { notifications } from "@mantine/notifications";
import {
  api,
  type AggregatedCode,
  type CoderRun,
  type QueueState,
  type SegmentDetail,
} from "../api";
import { EnqueueButton } from "./EnqueueButton";
import { StatusBadge } from "./StatusBadge";
import { useConfirmDelete } from "./ConfirmDelete";

export type CodingSegment = {
  segment_id: string;
  title: string | null;
  status: string;
  text: string;
  batch?: number | null;
  coder_runs: CoderRun[];
  aggregation: SegmentDetail["aggregation"];
  aggregated_codes: AggregatedCode[];
  queue_state: QueueState;
};

type Props = {
  segment: CodingSegment;
  invalidateKeys: ReadonlyArray<ReadonlyArray<unknown>>;
  index?: { i: number; total: number };
  showText?: boolean;
  /** External substring(s) of the segment text to wrap in `<mark>` (used by
   * `?quote=…` deep-links). When set, overrides the per-code highlighting. */
  highlight?: string[];
};

// ── Highlight palette ────────────────────────────────────────────────
// Light Mantine colors that work as background under black text. Order
// chosen for good adjacent-color contrast.
const PALETTE = [
  "yellow",
  "blue",
  "teal",
  "grape",
  "orange",
  "lime",
  "pink",
  "cyan",
  "indigo",
  "red",
];

function colorFor(i: number): string {
  return PALETTE[i % PALETTE.length];
}

/** Human-readable explanation of the review-decision letter on an
 * aggregated code. Mirrors `DECISION_ADD/MERGE/MERGE_AND_RENAME` in
 * `db/models.py`. */
function decisionTooltip(decision: string): string {
  switch (decision) {
    case "A":
      return "Add — promote as a new code in the codebook";
    case "M":
      return "Merge — fold into an existing codebook code";
    case "U":
      return "Merge & rename — fold into an existing code under a new name";
    case "skip":
      return "Skip — no codebook change";
    default:
      return `Decision: ${decision}`;
  }
}

// Blog-post-style prose container. Wider serif stack with good rendering
// hints, a comfortable line length, and slightly off-black text.
const PROSE_STYLE: CSSProperties = {
  fontFamily:
    "Charter, 'Iowan Old Style', 'Sitka Text', Cambria, 'Source Serif Pro', Georgia, 'Times New Roman', serif",
  fontSize: 18,
  lineHeight: 1.65,
  letterSpacing: "0.005em",
  color: "var(--mantine-color-dark-8)",
  maxWidth: "68ch",
  WebkitFontSmoothing: "antialiased",
  MozOsxFontSmoothing: "grayscale",
  hyphens: "auto",
};

type HighlightCode = {
  key: string;
  color: string;
  label: string;
  quotes: string[];
};

type Span = {
  start: number;
  end: number;
  /** Ordered keys of codes that match this span. */
  codes: string[];
};

/** Compute non-overlapping spans of `text` annotated with which code
 * keys cover each region. */
function computeSpans(text: string, codes: HighlightCode[]): Span[] {
  // Find every (start,end,key) match.
  type Match = { start: number; end: number; key: string };
  const matches: Match[] = [];
  for (const c of codes) {
    for (const q of c.quotes) {
      if (!q) continue;
      let idx = 0;
      while (true) {
        const found = text.indexOf(q, idx);
        if (found < 0) break;
        matches.push({ start: found, end: found + q.length, key: c.key });
        idx = found + Math.max(1, q.length);
      }
    }
  }
  if (matches.length === 0) {
    return [{ start: 0, end: text.length, codes: [] }];
  }
  // Build event points.
  const points = new Set<number>([0, text.length]);
  for (const m of matches) {
    points.add(m.start);
    points.add(m.end);
  }
  const sorted = [...points].sort((a, b) => a - b);
  const out: Span[] = [];
  for (let i = 0; i < sorted.length - 1; i++) {
    const s = sorted[i];
    const e = sorted[i + 1];
    if (s >= e) continue;
    const keys: string[] = [];
    for (const m of matches) {
      if (m.start <= s && m.end >= e && !keys.includes(m.key)) {
        keys.push(m.key);
      }
    }
    out.push({ start: s, end: e, codes: keys });
  }
  return out;
}

/** Render `text` as paragraphs with highlights driven by `codes`. A
 * hovered code key (if any) is emphasized; non-hovered codes fade. */
function HighlightedText({
  text,
  codes,
  hoveredKey,
  onHover,
}: {
  text: string;
  codes: HighlightCode[];
  hoveredKey: string | null;
  onHover: (k: string | null) => void;
}) {
  const spans = useMemo(() => computeSpans(text, codes), [text, codes]);
  const colorByKey = useMemo(() => {
    const m: Record<string, string> = {};
    for (const c of codes) m[c.key] = c.color;
    return m;
  }, [codes]);
  const labelByKey = useMemo(() => {
    const m: Record<string, string> = {};
    for (const c of codes) m[c.key] = c.label;
    return m;
  }, [codes]);
  // For each global span index, list of code keys whose coverage ends
  // here (i.e. the next span doesn't include them). We render a small
  // anchor-chip after the span for each ending code.
  const endingByIndex = useMemo(() => {
    const m: Record<number, string[]> = {};
    for (let i = 0; i < spans.length; i++) {
      const here = spans[i].codes;
      const next = spans[i + 1]?.codes ?? [];
      const ending = here.filter((k) => !next.includes(k));
      if (ending.length > 0) m[i] = ending;
    }
    return m;
  }, [spans]);

  // Group consecutive spans into paragraphs by splitting the text on
  // double newlines (blog-style paragraph breaks). Carry the original
  // span index so we can look up ending-code chips per render row.
  type PSpan = Span & { gi: number };
  const paragraphs: Array<Array<PSpan>> = [];
  let current: PSpan[] = [];
  for (let gi = 0; gi < spans.length; gi++) {
    const s = spans[gi];
    const slice = text.slice(s.start, s.end);
    const parts = slice.split(/\n\n+/);
    if (parts.length === 1) {
      current.push({ ...s, gi });
      continue;
    }
    let cursor = s.start;
    for (let i = 0; i < parts.length; i++) {
      const p = parts[i];
      const segEnd = cursor + p.length;
      if (segEnd > cursor) {
        current.push({ start: cursor, end: segEnd, codes: s.codes, gi });
      }
      cursor = segEnd;
      if (i < parts.length - 1) {
        while (cursor < s.end && text[cursor] === "\n") cursor++;
        paragraphs.push(current);
        current = [];
      }
    }
  }
  if (current.length > 0) paragraphs.push(current);

  return (
    <div style={PROSE_STYLE}>
      {paragraphs.map((paraSpans, pi) => (
        <p key={pi} style={{ margin: "0 0 1em 0", whiteSpace: "pre-wrap" }}>
          {paraSpans.map((s, si) => {
            const slice = text.slice(s.start, s.end);
            if (s.codes.length === 0) {
              return <span key={si}>{slice}</span>;
            }
            // Choose styling.
            const isHoverActive = hoveredKey !== null;
            const inHover = hoveredKey !== null && s.codes.includes(hoveredKey);
            const primaryKey = inHover ? hoveredKey! : s.codes[0];
            const color = colorByKey[primaryKey];
            const bgShade = isHoverActive && !inHover ? 0 : 2;
            const opacity = isHoverActive && !inHover ? 0.55 : 1;
            const style: CSSProperties = {
              backgroundColor:
                bgShade === 0
                  ? "transparent"
                  : `var(--mantine-color-${color}-${bgShade})`,
              opacity,
              cursor: "pointer",
              transition: "background-color 120ms, opacity 120ms",
              borderRadius: 2,
              padding: "0 1px",
            };
            const tooltipLabel = (
              <Stack gap={2}>
                {s.codes.map((k) => (
                  <Group key={k} gap={6} wrap="nowrap">
                    <span
                      style={{
                        display: "inline-block",
                        width: 8,
                        height: 8,
                        borderRadius: 2,
                        background: `var(--mantine-color-${colorByKey[k]}-5)`,
                      }}
                    />
                    <Text size="xs">{labelByKey[k]}</Text>
                  </Group>
                ))}
              </Stack>
            );
            const ending = endingByIndex[s.gi] ?? [];
            return (
              <span key={si}>
                <Tooltip
                  label={tooltipLabel}
                  withArrow
                  multiline
                  position="top"
                  openDelay={120}
                >
                  <span
                    style={style}
                    onMouseEnter={() => onHover(s.codes[0])}
                    onMouseLeave={() => onHover(null)}
                  >
                    {slice}
                  </span>
                </Tooltip>
                {ending.map((k) => {
                  const inactive = hoveredKey !== null && hoveredKey !== k;
                  const bgShade = hoveredKey === k ? 7 : 6;
                  return (
                    <span
                      key={k}
                      onMouseEnter={() => onHover(k)}
                      onMouseLeave={() => onHover(null)}
                      style={{
                        display: "inline-block",
                        marginLeft: 4,
                        padding: "0 0.6em",
                        fontSize: "0.75rem",
                        lineHeight: 1.5,
                        fontFamily: "inherit",
                        fontWeight: 500,
                        background: inactive
                          ? "var(--mantine-color-gray-3)"
                          : `var(--mantine-color-${colorByKey[k]}-${bgShade})`,
                        color: inactive
                          ? "var(--mantine-color-gray-7)"
                          : "white",
                        borderRadius: 4,
                        cursor: "pointer",
                        verticalAlign: "baseline",
                        whiteSpace: "nowrap",
                        transition: "background-color 120ms, color 120ms",
                      }}
                    >
                      {labelByKey[k]}
                    </span>
                  );
                })}
              </span>
            );
          })}
        </p>
      ))}
    </div>
  );
}

/** Legacy single-needle marker for `?quote=…` deep links. */
function legacyHighlightedText(text: string, needles: string[]): ReactNode {
  const valid = needles.filter((n) => n && text.includes(n));
  if (valid.length === 0) return text;
  const escaped = valid.map((n) => n.replace(/[.*+?^${}()|[\]\\]/g, "\\$&"));
  const re = new RegExp(`(${escaped.join("|")})`, "g");
  const parts = text.split(re);
  return parts.map((p, i) =>
    i % 2 === 1 ? (
      <mark key={i} style={{ background: "var(--mantine-color-yellow-2)" }}>
        {p}
      </mark>
    ) : (
      <span key={i}>{p}</span>
    ),
  );
}

// ── Tab data shape ───────────────────────────────────────────────────
type TabDescriptor = {
  key: string;
  label: string;
  kind: "agg" | "coder";
  run?: CoderRun;
  /** Codes painted in the highlight layer & listed in the tab body. */
  highlightCodes: HighlightCode[];
};

export function SegmentCodingCard({
  segment,
  invalidateKeys,
  index,
  showText = true,
  highlight,
}: Props) {
  const qc = useQueryClient();
  const confirm = useConfirmDelete();
  const [editing, setEditing] = useState<
    | { kind: "coder"; run_id: number; position: number; code: string }
    | { kind: "agg"; id: number; code: string }
    | null
  >(null);
  const [hoveredKey, setHoveredKey] = useState<string | null>(null);
  const [expanded, setExpanded] = useState<Record<string, boolean>>({});

  const invalidate = () => {
    for (const key of invalidateKeys) {
      qc.invalidateQueries({ queryKey: [...key] });
    }
  };

  const enqueue = useMutation({
    mutationFn: () => api.enqueueSegment(segment.segment_id),
    onSuccess: (res) => {
      notifications.show({
        message:
          res.enqueued > 0
            ? `Enqueued ${res.enqueued} (segment, coder) pair${res.enqueued === 1 ? "" : "s"} for coding`
            : "Already enqueued at the current codebook + research-context revisions",
        color: "teal",
      });
      invalidate();
      qc.invalidateQueries({ queryKey: ["coding-queue"] });
    },
    onError: (e) =>
      notifications.show({
        message: e instanceof Error ? e.message : String(e),
        color: "red",
      }),
  });

  const saveCoderCode = useMutation({
    mutationFn: (v: { run_id: number; position: number; code: string }) =>
      api.editCoderCode(v.run_id, v.position, v.code),
    onSuccess: () => {
      notifications.show({ message: "Code updated", color: "teal" });
      setEditing(null);
      invalidate();
    },
    onError: (e) =>
      notifications.show({
        message: e instanceof Error ? e.message : String(e),
        color: "red",
      }),
  });

  const saveAggCode = useMutation({
    mutationFn: (v: { id: number; code: string }) =>
      api.editAggregatedCode(v.id, v.code),
    onSuccess: () => {
      notifications.show({ message: "Code updated", color: "teal" });
      setEditing(null);
      invalidate();
    },
    onError: (e) =>
      notifications.show({
        message: e instanceof Error ? e.message : String(e),
        color: "red",
      }),
  });

  const delCoderRun = useMutation({
    mutationFn: (run_id: number) => api.deleteCoderRun(run_id),
    onSuccess: () => {
      notifications.show({
        message: "Run deleted — segment will be re-coded.",
        color: "teal",
      });
      invalidate();
      qc.invalidateQueries({ queryKey: ["status"] });
    },
    onError: (e) =>
      notifications.show({
        message: e instanceof Error ? e.message : String(e),
        color: "red",
      }),
  });

  const delAgg = useMutation({
    mutationFn: (agg_id: number) => api.deleteAggregation(agg_id),
    onSuccess: () => {
      notifications.show({ message: "Aggregation deleted", color: "teal" });
      invalidate();
      qc.invalidateQueries({ queryKey: ["status"] });
    },
    onError: (e) =>
      notifications.show({
        message: e instanceof Error ? e.message : String(e),
        color: "red",
      }),
  });

  const delAggCode = useMutation({
    mutationFn: (ac_id: number) => api.deleteAggregatedCode(ac_id),
    onSuccess: () => {
      notifications.show({ message: "Code deleted", color: "teal" });
      invalidate();
    },
    onError: (e) =>
      notifications.show({
        message: e instanceof Error ? e.message : String(e),
        color: "red",
      }),
  });

  const words = segment.text.split(/\s+/).filter(Boolean).length;

  // Build tabs: Aggregated (if any) + one per coder run.
  const tabs: TabDescriptor[] = useMemo(() => {
    const out: TabDescriptor[] = [];
    if (segment.aggregated_codes.length > 0) {
      out.push({
        key: "agg",
        label: "Aggregated",
        kind: "agg",
        highlightCodes: segment.aggregated_codes.map((ac, i) => ({
          key: `agg-${ac.id}`,
          color: colorFor(i),
          label: ac.code,
          quotes: ac.quotes.map((q) => q.text),
        })),
      });
    }
    for (const run of segment.coder_runs) {
      out.push({
        key: `c-${run.coder_id}`,
        label: run.coder_id,
        kind: "coder",
        run,
        highlightCodes: run.codes.map((c, i) => ({
          key: `c-${run.coder_id}-${c.position}`,
          color: colorFor(i),
          label: c.code,
          quotes: (c.quotes ?? []).map((q) => q.text),
        })),
      });
    }
    return out;
  }, [segment]);

  const defaultTab = tabs[0]?.key;
  const [activeTab, setActiveTab] = useState<string | null>(defaultTab ?? null);
  const active = tabs.find((t) => t.key === (activeTab ?? defaultTab)) ?? tabs[0];

  return (
    <Card padding="md" withBorder>
      {confirm.modal}

      <Modal
        opened={editing !== null}
        onClose={() => setEditing(null)}
        title="Edit code"
        centered
      >
        {editing && (
          <Stack>
            <TextInput
              autoFocus
              value={editing.code}
              onChange={(e) =>
                setEditing({ ...editing, code: e.currentTarget.value })
              }
            />
            <Group justify="flex-end">
              <Button variant="default" onClick={() => setEditing(null)}>
                Cancel
              </Button>
              <Button
                loading={saveCoderCode.isPending || saveAggCode.isPending}
                onClick={() => {
                  if (editing.kind === "coder")
                    saveCoderCode.mutate({
                      run_id: editing.run_id,
                      position: editing.position,
                      code: editing.code,
                    });
                  else saveAggCode.mutate({ id: editing.id, code: editing.code });
                }}
              >
                Save
              </Button>
            </Group>
          </Stack>
        )}
      </Modal>

      <Group justify="space-between" align="flex-start" wrap="nowrap">
        <Stack gap={2} style={{ flex: 1, minWidth: 0 }}>
          <Group gap="xs" wrap="nowrap">
            {index && (
              <Text size="xs" c="dimmed" ff="monospace">
                [{index.i + 1}/{index.total}]
              </Text>
            )}
            <Anchor
              component={Link}
              to={`/segments/${segment.segment_id}`}
              size="sm"
              ff="monospace"
            >
              {segment.segment_id}
            </Anchor>
            <Text size="xs" c="dimmed">
              {words} words · {segment.text.length} chars
            </Text>
            {segment.batch != null && (
              <Badge variant="default" size="xs">
                batch {segment.batch}
              </Badge>
            )}
          </Group>
          {segment.title && (
            <Text size="lg" fw={600}>
              {segment.title}
            </Text>
          )}
        </Stack>
        <Group gap="xs" wrap="nowrap">
          <EnqueueButton
            queueState={segment.queue_state}
            kind="segment"
            isLoading={enqueue.isPending}
            onEnqueue={() => enqueue.mutate()}
          />
          <StatusBadge status={segment.status} />
        </Group>
      </Group>

      {showText && (
        <>
          <Divider my="xs" />
          {highlight && highlight.length > 0 ? (
            <div style={{ ...PROSE_STYLE, whiteSpace: "pre-wrap" }}>
              {legacyHighlightedText(segment.text, highlight)}
            </div>
          ) : active ? (
            <HighlightedText
              text={segment.text}
              codes={active.highlightCodes}
              hoveredKey={hoveredKey}
              onHover={setHoveredKey}
            />
          ) : (
            <div style={{ ...PROSE_STYLE, whiteSpace: "pre-wrap" }}>
              {segment.text}
            </div>
          )}
        </>
      )}

      <Divider my="sm" />

      {tabs.length === 0 ? (
        <Text c="dimmed" size="sm">
          No codes yet for this segment.
        </Text>
      ) : (
        <Tabs
          value={activeTab ?? defaultTab}
          onChange={(v) => {
            setActiveTab(v);
            setHoveredKey(null);
            setExpanded({});
          }}
          keepMounted={false}
        >
          <Tabs.List>
            {tabs.map((t) => (
              <Tabs.Tab key={t.key} value={t.key}>
                {t.label}
                {t.kind === "coder" && t.run && (
                  <Text component="span" size="xs" c="dimmed" ml={6}>
                    ({t.run.codes.length})
                  </Text>
                )}
                {t.kind === "agg" && (
                  <Text component="span" size="xs" c="dimmed" ml={6}>
                    ({segment.aggregated_codes.length})
                  </Text>
                )}
              </Tabs.Tab>
            ))}
          </Tabs.List>

          {tabs.map((t) => (
            <Tabs.Panel key={t.key} value={t.key} pt="sm">
              {t.kind === "coder" && t.run ? (
                <CoderTabBody
                  run={t.run}
                  highlightCodes={t.highlightCodes}
                  hoveredKey={hoveredKey}
                  onHover={setHoveredKey}
                  expanded={expanded}
                  setExpanded={setExpanded}
                  onEdit={(position, code) =>
                    setEditing({
                      kind: "coder",
                      run_id: t.run!.id,
                      position,
                      code,
                    })
                  }
                  onDeleteRun={() =>
                    confirm.ask({
                      title: `Delete run by ${t.run!.coder_id}?`,
                      body: `Deletes this coder run, its codes, and any aggregation/reviews for the segment. The pipeline will recreate them on the next 'code'/'aggregate' run.`,
                      onConfirm: () => delCoderRun.mutateAsync(t.run!.id),
                    })
                  }
                />
              ) : (
                <AggregatedTabBody
                  segment={segment}
                  highlightCodes={t.highlightCodes}
                  hoveredKey={hoveredKey}
                  onHover={setHoveredKey}
                  expanded={expanded}
                  setExpanded={setExpanded}
                  onEdit={(id, code) =>
                    setEditing({ kind: "agg", id, code })
                  }
                  onDeleteCode={(id) =>
                    confirm.ask({
                      title: "Delete merged code?",
                      body: "Removes the aggregated code and any review decision for it.",
                      onConfirm: () => delAggCode.mutateAsync(id),
                    })
                  }
                  onDeleteAggregation={() =>
                    segment.aggregation &&
                    confirm.ask({
                      title: "Delete aggregation?",
                      body: "Deletes the aggregation, its merged codes, and any review decisions on them. The aggregator will rebuild it.",
                      onConfirm: () =>
                        delAgg.mutateAsync(segment.aggregation!.id),
                    })
                  }
                />
              )}
            </Tabs.Panel>
          ))}
        </Tabs>
      )}
    </Card>
  );
}

// ── Per-coder tab body ───────────────────────────────────────────────
function CoderTabBody({
  run,
  highlightCodes,
  hoveredKey,
  onHover,
  expanded,
  setExpanded,
  onEdit,
  onDeleteRun,
}: {
  run: CoderRun;
  highlightCodes: HighlightCode[];
  hoveredKey: string | null;
  onHover: (k: string | null) => void;
  expanded: Record<string, boolean>;
  setExpanded: Dispatch<SetStateAction<Record<string, boolean>>>;
  onEdit: (position: number, code: string) => void;
  onDeleteRun: () => void;
}) {
  return (
    <Stack gap="xs">
      <Group justify="space-between">
        <Group gap="xs">
          <StatusBadge status={run.status} />
          <Text size="xs" c="dimmed">
            codebook v{run.codebook_version}
          </Text>
          {run.finished_at && (
            <Text size="xs" c="dimmed">
              {run.finished_at}
            </Text>
          )}
        </Group>
        <Tooltip label="Delete run (will be re-coded; downstream aggregation also reset)">
          <ActionIcon variant="subtle" color="red" onClick={onDeleteRun}>
            <IconTrash size={16} />
          </ActionIcon>
        </Tooltip>
      </Group>
      {run.error && (
        <Alert
          variant="light"
          color="red"
          icon={<IconAlertTriangle size={16} />}
        >
          <Code>{run.error}</Code>
        </Alert>
      )}
      {run.codes.length === 0 ? (
        run.status === "done" ? (
          <Text size="sm" c="dimmed">
            Coder produced no codes for this segment (e.g. out of scope).
          </Text>
        ) : run.status === "running" ? (
          <Text size="sm" c="dimmed">
            Coding in progress — no codes recorded yet.
          </Text>
        ) : null
      ) : (
        <Stack gap={4}>
          {run.codes.map((c, i) => {
            const hc = highlightCodes[i];
            const isExpanded = expanded[hc.key] ?? false;
            const isHovered = hoveredKey === hc.key;
            return (
              <CodeRow
                key={hc.key}
                color={hc.color}
                isHovered={isHovered}
                onHover={(v) => onHover(v ? hc.key : null)}
                expanded={isExpanded}
                onToggleExpand={() =>
                  setExpanded((s) => ({ ...s, [hc.key]: !isExpanded }))
                }
                codeText={c.code}
                rationale={c.rationale ?? ""}
                badges={
                  c.is_new === 1 ? (
                    <Badge size="xs" color="teal" variant="light">
                      new
                    </Badge>
                  ) : c.is_new === 0 ? (
                    <Badge size="xs" variant="default">
                      existing
                    </Badge>
                  ) : null
                }
                quotes={(c.quotes ?? []).map((q) => q.text)}
                onEdit={() => onEdit(c.position, c.code)}
              />
            );
          })}
        </Stack>
      )}
    </Stack>
  );
}

// ── Aggregated tab body ──────────────────────────────────────────────
function AggregatedTabBody({
  segment,
  highlightCodes,
  hoveredKey,
  onHover,
  expanded,
  setExpanded,
  onEdit,
  onDeleteCode,
  onDeleteAggregation,
}: {
  segment: CodingSegment;
  highlightCodes: HighlightCode[];
  hoveredKey: string | null;
  onHover: (k: string | null) => void;
  expanded: Record<string, boolean>;
  setExpanded: Dispatch<SetStateAction<Record<string, boolean>>>;
  onEdit: (id: number, code: string) => void;
  onDeleteCode: (id: number) => void;
  onDeleteAggregation: () => void;
}) {
  if (!segment.aggregation) {
    return (
      <Text c="dimmed" size="sm">
        Not aggregated yet.
      </Text>
    );
  }
  if (segment.aggregation.error) {
    return (
      <Alert color="red" variant="light">
        <Code>{segment.aggregation.error}</Code>
      </Alert>
    );
  }
  if (segment.aggregated_codes.length === 0) {
    return (
      <Text c="dimmed" size="sm">
        No aggregated codes (nothing to review).
      </Text>
    );
  }
  return (
    <Stack gap="xs">
      <Group justify="space-between">
        <StatusBadge status={segment.aggregation.status} />
        <Tooltip label="Delete aggregation (will be re-aggregated)">
          <ActionIcon
            variant="subtle"
            color="red"
            onClick={onDeleteAggregation}
          >
            <IconTrash size={16} />
          </ActionIcon>
        </Tooltip>
      </Group>
      <Stack gap={4}>
        {segment.aggregated_codes.map((ac, i) => {
          const hc = highlightCodes[i];
          const isExpanded = expanded[hc.key] ?? false;
          const isHovered = hoveredKey === hc.key;
          return (
            <CodeRow
              key={hc.key}
              color={hc.color}
              isHovered={isHovered}
              onHover={(v) => onHover(v ? hc.key : null)}
              expanded={isExpanded}
              onToggleExpand={() =>
                setExpanded((s) => ({ ...s, [hc.key]: !isExpanded }))
              }
              codeText={ac.code}
              rationale=""
              badges={
                <Group gap={4} wrap="nowrap">
                  {ac.source_coders.map((c) => (
                    <Badge key={c} variant="light" size="xs">
                      {c}
                    </Badge>
                  ))}
                  {ac.review && (
                    <Tooltip label={decisionTooltip(ac.review.decision)} withArrow>
                      <Badge
                        size="xs"
                        color={
                          ac.review.applied
                            ? "teal"
                            : ac.review.decision === "skip"
                              ? "gray"
                              : "yellow"
                        }
                        variant="light"
                        style={{ cursor: "help" }}
                      >
                        {ac.review.decision}
                        {ac.review.target_code
                          ? ` → ${ac.review.target_code}`
                          : ""}
                      </Badge>
                    </Tooltip>
                  )}
                  {ac.codebook_used_id != null && (
                    <Tooltip
                      label={`Created against codebook v${ac.codebook_used_id}`}
                      withArrow
                    >
                      <Anchor
                        component={Link}
                        to={`/codebook/${ac.codebook_used_id}`}
                        size="xs"
                        c="dimmed"
                      >
                        v{ac.codebook_used_id}
                      </Anchor>
                    </Tooltip>
                  )}
                  {ac.review?.resulting_version && (
                    <Tooltip
                      label={`Review produced codebook v${ac.review.resulting_version}`}
                      withArrow
                    >
                      <Anchor
                        component={Link}
                        to={`/codebook/${ac.review.resulting_version}`}
                        size="xs"
                      >
                        → v{ac.review.resulting_version}{" "}
                        <IconExternalLink
                          size={12}
                          style={{ verticalAlign: "middle" }}
                        />
                      </Anchor>
                    </Tooltip>
                  )}
                </Group>
              }
              quotes={ac.quotes.map((q) => q.text)}
              onEdit={() => onEdit(ac.id, ac.code)}
              onDelete={() => onDeleteCode(ac.id)}
            />
          );
        })}
      </Stack>
    </Stack>
  );
}

// ── Reusable code row ────────────────────────────────────────────────
function CodeRow({
  color,
  isHovered,
  onHover,
  expanded,
  onToggleExpand,
  codeText,
  rationale,
  badges,
  quotes,
  onEdit,
  onDelete,
}: {
  color: string;
  isHovered: boolean;
  onHover: (hovered: boolean) => void;
  expanded: boolean;
  onToggleExpand: () => void;
  codeText: string;
  rationale: string;
  badges: ReactNode;
  quotes: string[];
  onEdit: () => void;
  onDelete?: () => void;
}) {
  return (
    <Card
      withBorder
      padding="xs"
      onMouseEnter={() => onHover(true)}
      onMouseLeave={() => onHover(false)}
      style={{
        borderLeft: `4px solid var(--mantine-color-${color}-${isHovered ? 6 : 4})`,
        backgroundColor: isHovered
          ? `var(--mantine-color-${color}-0)`
          : undefined,
        transition: "background-color 120ms, border-color 120ms",
      }}
    >
      <Group gap="xs" wrap="nowrap" align="flex-start">
        <ActionIcon
          size="sm"
          variant="subtle"
          onClick={onToggleExpand}
          aria-label={expanded ? "Collapse" : "Expand"}
        >
          {expanded ? (
            <IconChevronDown size={14} />
          ) : (
            <IconChevronRight size={14} />
          )}
        </ActionIcon>
        <UnstyledButton
          onClick={onToggleExpand}
          style={{ flex: 1, minWidth: 0 }}
        >
          <Stack gap={2}>
            <Group gap="xs" wrap="wrap">
              <Code>{codeText}</Code>
              {badges}
            </Group>
            {rationale && (
              <Text size="xs" c="dimmed">
                {rationale}
              </Text>
            )}
          </Stack>
        </UnstyledButton>
        <Group gap={2} wrap="nowrap">
          <Tooltip label="Edit code text">
            <ActionIcon size="sm" variant="subtle" onClick={onEdit}>
              <IconEdit size={14} />
            </ActionIcon>
          </Tooltip>
          {onDelete && (
            <Tooltip label="Delete this code">
              <ActionIcon
                size="sm"
                variant="subtle"
                color="red"
                onClick={onDelete}
              >
                <IconTrash size={14} />
              </ActionIcon>
            </Tooltip>
          )}
        </Group>
      </Group>
      <Collapse in={expanded}>
        {quotes.length === 0 ? (
          <Text size="xs" c="dimmed" mt="xs">
            No supporting quotes recorded.
          </Text>
        ) : (
          <Stack gap={4} mt="xs">
            <Text size="xs" c="dimmed" fw={600}>
              Quotes ({quotes.length})
            </Text>
            {quotes.map((q, i) => (
              <Text
                key={i}
                size="sm"
                style={{
                  borderLeft: `2px solid var(--mantine-color-${color}-4)`,
                  paddingLeft: 8,
                  fontStyle: "italic",
                }}
              >
                {q}
              </Text>
            ))}
          </Stack>
        )}
      </Collapse>
    </Card>
  );
}

