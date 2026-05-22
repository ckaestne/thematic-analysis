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

/**
 * One lineage tree node. Renders the source row inline and, when
 * expanded, fetches /api/codes/{id} for that source and recurses on
 * its own derivation_sources. Each expansion is its own query.
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

  const edgeLabel =
    source.derivation_type === "R"
      ? `review${source.decision ? ` · ${source.decision}` : ""}`
      : "aggregation";

  return (
    <div
      style={{
        marginLeft: depth === 0 ? 0 : 16,
        borderLeft:
          depth === 0 ? undefined : "2px solid var(--mantine-color-gray-3)",
        paddingLeft: depth === 0 ? 0 : 12,
        marginTop: 6,
      }}
    >
      <Card padding="sm" withBorder shadow="none">
        <Group justify="space-between" wrap="nowrap" align="flex-start">
          <Stack gap={2} style={{ flex: 1, minWidth: 0 }}>
            <Group gap="xs" wrap="wrap">
              <Badge size="xs" variant="light">
                {edgeLabel}
              </Badge>
              <Badge size="xs" color={coderColor(source.coder_id)}>
                {coderLabel(source.coder_id, source.coder_identity)}
              </Badge>
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
            </Group>
            {source.description && (
              <Text size="xs" c="dimmed">
                {source.description}
              </Text>
            )}
            {source.rationale && (
              <Text size="xs" c="dimmed" fs="italic">
                edge: {source.rationale}
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
              // Aggregator codes typically have many children; open the
              // first level eagerly when an aggregator was just expanded
              // so users see the merged coder codes without an extra
              // click.
              defaultOpen={
                source.coder_id === 0 &&
                depth === 0 &&
                source.derivation_type === "R"
              }
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

function selfAsSource(d: CodeDetail): CodeDerivationSource {
  return {
    code_id: d.code_id,
    derivation_type: "R",
    decision: null,
    rationale: null,
    code: d.code,
    description: d.description,
    coder_id: d.coder_id,
    coder_identity: d.coder_identity,
    codebook_used_id: d.codebook_used_id,
    segment_id: d.segment_id,
    document_id: d.segment?.document_id ?? null,
    document_filename: d.segment?.document_filename ?? null,
    has_more_sources: d.derivation_sources.length > 0,
  };
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
        {d.derivation_sources.length === 0 ? (
          <Text c="dimmed" size="sm">
            This code has no derivation sources — it is an originating coder
            code.
          </Text>
        ) : (
          <>
            <Text size="xs" c="dimmed" mb="xs">
              {d.coder_id === -1
                ? "Reviewer codes merge a prior reviewer code (in the parent codebook) with one or more aggregator codes."
                : d.coder_id === 0
                  ? "Aggregator codes merge codes from multiple coders on the same segment."
                  : "Derived from:"}
            </Text>
            {/* Root node is the current code — render it as a non-expandable
                header so the tree visually starts here, then show the
                immediate sources expanded. */}
            <LineageNode source={selfAsSource(d)} defaultOpen={true} />
          </>
        )}
      </Card>
    </Stack>
  );
}
