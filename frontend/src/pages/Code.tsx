import {
  Anchor,
  Badge,
  Breadcrumbs,
  Card,
  Code as CodeText,
  Collapse,
  Group,
  Stack,
  Text,
  Title,
  UnstyledButton,
} from "@mantine/core";
import { IconChevronDown, IconChevronRight } from "@tabler/icons-react";
import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { Link, useParams } from "react-router-dom";
import {
  api,
  type CodebookQuote,
  type CodeDerivationSource,
  type CodeDetail,
} from "../api";
import { ErrorAlert } from "../components/ErrorAlert";

function coderLabel(coder_id: number | null | undefined, identity?: string | null) {
  if (coder_id === -1) return "reviewer";
  if (coder_id === 0) return "aggregator";
  if (coder_id == null) return "?";
  return identity ? `coder ${coder_id} (${identity})` : `coder ${coder_id}`;
}

function coderColor(coder_id: number | null | undefined): string {
  if (coder_id === -1) return "violet";
  if (coder_id === 0) return "teal";
  return "blue";
}

function QuoteLink({ q }: { q: CodebookQuote }) {
  const to = q.document_id
    ? `/documents/${q.document_id}?segment=${q.segment_id}&quote=${q.quote_id}`
    : `/segments/${q.segment_id}?quote=${q.quote_id}`;
  return (
    <Anchor
      component={Link}
      to={to}
      style={{
        display: "block",
        borderLeft: "3px solid var(--mantine-color-indigo-6)",
        padding: "4px 10px",
        background: "var(--mantine-color-default-hover)",
        borderRadius: 3,
        textDecoration: "none",
        color: "inherit",
      }}
    >
      <Text size="xs" ff="monospace">
        {q.document_filename ?? "(no document)"}#{q.segment_id}
        <Text span size="xs" c="dimmed">
          {" "}
          · quote {q.quote_id}
        </Text>
      </Text>
      <Text size="sm">{q.text}</Text>
    </Anchor>
  );
}

/** Label introducing a single edge, based on its derivation kind. */
function sourceLabel(s: CodeDerivationSource): string {
  if (s.derivation_type === "A") return "Aggregated from";
  if (s.derivation_type === "R") {
    if (s.decision === "A") return "New from";
    // 'M' = merge, 'U' = merge & rename — both are merges from the
    // user's point of view.
    return "Merged from";
  }
  return "From";
}

/** One-sentence summary of how this code came into existence, shown at
 * the top of the lineage section so the tree below has a frame of
 * reference. */
function originSentence(d: CodeDetail): string {
  if (d.derivation_sources.length === 0) {
    return "This code has no derivation sources — it is an originating coder code.";
  }
  if (d.coder_id === -1) {
    const decisions = new Set(
      d.derivation_sources.map((s) => s.decision).filter(Boolean),
    );
    if (decisions.has("A")) {
      return "This code was created by adopting a new code from the aggregator.";
    }
    if (decisions.has("M") || decisions.has("U")) {
      return "This code was created by merging two codes.";
    }
    return "This code was created from:";
  }
  if (d.coder_id === 0) {
    return "This code was aggregated from one or more coder codes.";
  }
  return "Derived from:";
}

/**
 * One lineage tree node. Shows the edge label (e.g. "Aggregated from"),
 * then the source card inline; when expanded, fetches /api/codes/{id}
 * for that source and recurses on its own derivation_sources. Each
 * expansion is its own query.
 */
function LineageNode({
  source,
  depth = 0,
  defaultOpen = false,
}: {
  source: CodeDerivationSource;
  depth?: number;
  defaultOpen?: boolean;
}) {
  const [open, setOpen] = useState(defaultOpen);
  const expandable = source.has_more_sources;
  const detail = useQuery({
    queryKey: ["code", source.code_id],
    queryFn: () => api.code(source.code_id),
    enabled: open && expandable,
  });

  return (
    <div
      style={{
        marginLeft: depth === 0 ? 0 : 16,
        borderLeft:
          depth === 0 ? undefined : "2px solid var(--mantine-color-gray-3)",
        paddingLeft: depth === 0 ? 0 : 12,
        marginTop: 8,
      }}
    >
      <Text size="xs" c="dimmed" mb={4}>
        {sourceLabel(source)}
      </Text>
      <Card padding="sm" withBorder shadow="none">
        <Group justify="space-between" wrap="nowrap" align="flex-start">
          <Stack gap={2} style={{ flex: 1, minWidth: 0 }}>
            <Group gap="xs" wrap="nowrap">
              <Anchor
                component={Link}
                to={`/code/${source.code_id}`}
                style={{ minWidth: 0 }}
              >
                <CodeText style={{ fontSize: 14 }}>
                  {source.code ?? `code #${source.code_id}`}
                </CodeText>
              </Anchor>
              <Badge size="xs" color={coderColor(source.coder_id)}>
                {coderLabel(source.coder_id, source.coder_identity)}
              </Badge>
            </Group>
            <Group gap="xs" wrap="wrap">
              {source.codebook_used_id != null && (
                <Anchor
                  component={Link}
                  to={`/codebook/${source.codebook_used_id}`}
                  size="xs"
                >
                  codebook v{source.codebook_used_id}
                </Anchor>
              )}
              {source.segment_id != null && (
                <Anchor
                  component={Link}
                  to={
                    source.document_id
                      ? `/documents/${source.document_id}?segment=${source.segment_id}`
                      : `/segments/${source.segment_id}`
                  }
                  size="xs"
                  ff="monospace"
                >
                  {source.document_filename ?? "(no document)"}#
                  {source.segment_id}
                </Anchor>
              )}
            </Group>
            {source.description && (
              <Text size="xs" c="dimmed">
                {source.description}
              </Text>
            )}
            {source.rationale && (
              <Text size="xs" c="dimmed" fs="italic">
                edge rationale: {source.rationale}
              </Text>
            )}
          </Stack>
          {expandable ? (
            <UnstyledButton onClick={() => setOpen((o) => !o)}>
              <Group gap={4} wrap="nowrap">
                {open ? (
                  <IconChevronDown size={16} />
                ) : (
                  <IconChevronRight size={16} />
                )}
                <Text size="xs" c="dimmed">
                  {open ? "hide" : "expand"}
                </Text>
              </Group>
            </UnstyledButton>
          ) : (
            <Text size="xs" c="dimmed">
              leaf
            </Text>
          )}
        </Group>
      </Card>
      {expandable && (
        <Collapse in={open}>
          {detail.isLoading && (
            <Text size="xs" c="dimmed" pl="md" pt={4}>
              Loading…
            </Text>
          )}
          {detail.error && <ErrorAlert error={detail.error} />}
          {detail.data?.derivation_sources.map((s) => (
            <LineageNode
              key={s.code_id}
              source={s}
              depth={depth + 1}
              // When the user expands an aggregator, they almost always
              // want to see its coder-code children — open them eagerly
              // so a reviewer→aggregator→coders walk is one click.
              defaultOpen={source.coder_id === 0}
            />
          ))}
          {detail.data && detail.data.derivation_sources.length === 0 && (
            <Text size="xs" c="dimmed" pl="md" pt={4}>
              No further sources.
            </Text>
          )}
        </Collapse>
      )}
    </div>
  );
}

export function CodePage() {
  const { id } = useParams<{ id: string }>();
  const codeId = Number(id);
  const detail = useQuery({
    queryKey: ["code", codeId],
    queryFn: () => api.code(codeId),
    enabled: Number.isFinite(codeId),
  });

  if (detail.error) return <ErrorAlert error={detail.error} />;
  if (!detail.data) return <Text c="dimmed">Loading…</Text>;
  const d = detail.data;

  return (
    <Stack gap="md">
      <Breadcrumbs>
        <Anchor component={Link} to="/codebook">
          Codebook
        </Anchor>
        <Text>code {d.code_id}</Text>
      </Breadcrumbs>

      <Card padding="md" withBorder>
        <Group justify="space-between" align="flex-start" wrap="nowrap">
          <Stack gap={4} style={{ flex: 1, minWidth: 0 }}>
            <Group gap="xs">
              <CodeText style={{ fontSize: 18, fontWeight: 700 }}>
                {d.code}
              </CodeText>
              <Badge color={coderColor(d.coder_id)}>
                {coderLabel(d.coder_id, d.coder_identity)}
              </Badge>
            </Group>
            {d.description && <Text>{d.description}</Text>}
            {d.rationale && (
              <Text size="sm" c="dimmed" fs="italic">
                Rationale: {d.rationale}
              </Text>
            )}
            <Group gap="md" mt="xs">
              <Anchor
                component={Link}
                to={`/codebook/${d.codebook_used_id}`}
                size="sm"
              >
                authored against codebook v{d.codebook_used_id}
              </Anchor>
              {d.segment && (
                <Anchor
                  component={Link}
                  to={`/documents/${d.segment.document_id}?segment=${d.segment.segment_id}`}
                  size="sm"
                  ff="monospace"
                >
                  {d.segment.document_filename ?? "(no document)"}#
                  {d.segment.segment_id} (lines {d.segment.line_from}–
                  {d.segment.line_to})
                </Anchor>
              )}
              {d.in_codebook_versions.length > 0 && (
                <Text size="sm" c="dimmed">
                  member of:{" "}
                  {d.in_codebook_versions.map((v, i) => (
                    <span key={v}>
                      {i > 0 && ", "}
                      <Anchor component={Link} to={`/codebook/${v}`} size="sm">
                        v{v}
                      </Anchor>
                    </span>
                  ))}
                </Text>
              )}
            </Group>
          </Stack>
        </Group>
      </Card>

      {d.quotes.length > 0 && (
        <Card padding="md" withBorder>
          <Title order={5} mb="xs">
            Supporting quotes ({d.quotes.length})
          </Title>
          <Stack gap={6}>
            {d.quotes.map((q) => (
              <QuoteLink key={q.quote_id} q={q} />
            ))}
          </Stack>
        </Card>
      )}

      <Card padding="md" withBorder>
        <Title order={5} mb="xs">
          Lineage
        </Title>
        <Text size="sm" mb="xs">
          {originSentence(d)}
        </Text>
        {d.derivation_sources.map((s) => (
          <LineageNode key={s.code_id} source={s} defaultOpen={false} />
        ))}
      </Card>
    </Stack>
  );
}
