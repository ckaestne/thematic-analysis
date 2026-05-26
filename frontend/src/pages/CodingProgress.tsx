import {
  Anchor,
  Badge,
  Card,
  Code as CodeText,
  Group,
  SimpleGrid,
  Stack,
  Table,
  Text,
  Title,
} from "@mantine/core";
import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import {
  api,
  type CodebookPreviewAdded,
  type RecentCode,
} from "../api";
import { ErrorAlert } from "../components/ErrorAlert";
import { StatCard } from "../components/StatCard";
import { StatusBar } from "../components/StatusBar";
import { BackgroundRunnerCard } from "../components/BackgroundRunnerCard";

function kindColor(kind: RecentCode["kind"]): string {
  if (kind === "review") return "violet";
  if (kind === "aggregation") return "teal";
  return "blue";
}

function kindLabel(kind: RecentCode["kind"]): string {
  if (kind === "review") return "review";
  if (kind === "aggregation") return "aggregation";
  return "coder";
}

function coderLabel(r: RecentCode): string {
  if (r.coder_id === -1) return "reviewer";
  if (r.coder_id === 0) return "aggregator";
  return r.coder_identity
    ? `coder ${r.coder_id} (${r.coder_identity})`
    : `coder ${r.coder_id}`;
}

function decisionLabel(d: string | null): string {
  if (d === "A") return "added";
  if (d === "M") return "merged";
  if (d === "U") return "merged & renamed";
  return d ?? "";
}

function AddedCodeRow({ c }: { c: CodebookPreviewAdded }) {
  return (
    <Stack
      gap={4}
      style={{
        padding: "8px 12px",
        borderLeft: "3px solid var(--mantine-color-teal-6)",
        background: "var(--mantine-color-default-hover)",
        borderRadius: 3,
      }}
    >
      <Group gap="xs" wrap="nowrap">
        <Badge size="xs" color="teal" variant="light">
          {c.change}
        </Badge>
        <Anchor component={Link} to={`/code/${c.code_id}`}>
          <CodeText style={{ fontSize: 13 }}>{c.code}</CodeText>
        </Anchor>
        <Text size="xs" c="dimmed">
          {c.n_quotes} quote{c.n_quotes === 1 ? "" : "s"}
        </Text>
      </Group>
      {c.description && (
        <Text size="xs" c="dimmed">
          {c.description}
        </Text>
      )}
      {c.sources.length > 0 && (
        <Stack gap={2} pl="sm">
          {c.sources.map((s, i) => (
            <Group key={`${s.code_id}-${i}`} gap="xs" wrap="nowrap">
              <Text size="xs" c="dimmed">
                ←{" "}
                <span style={{ fontStyle: "italic" }}>
                  {decisionLabel(s.decision)}
                </span>{" "}
                from
              </Text>
              <Anchor component={Link} to={`/code/${s.code_id}`} size="xs">
                <CodeText style={{ fontSize: 12 }}>
                  {s.code ?? `code #${s.code_id}`}
                </CodeText>
              </Anchor>
              {s.segment_id != null && (
                <Anchor
                  component={Link}
                  to={
                    s.document_id
                      ? `/documents/${s.document_id}?segment=${s.segment_id}`
                      : `/segments/${s.segment_id}`
                  }
                  size="xs"
                  ff="monospace"
                  c="dimmed"
                >
                  {s.document_filename ?? ""}#{s.segment_id}
                </Anchor>
              )}
            </Group>
          ))}
        </Stack>
      )}
    </Stack>
  );
}

export function CodingProgressPage() {
  const status = useQuery({
    queryKey: ["status"],
    queryFn: api.status,
  });
  const recent = useQuery({
    queryKey: ["recent-codes", 20],
    queryFn: () => api.recentCodes(20),
  });
  const preview = useQuery({
    queryKey: ["codebook-preview"],
    queryFn: api.codebookPreview,
  });

  if (status.error) return <ErrorAlert error={status.error} />;
  if (!status.data) return <Text c="dimmed">Loading…</Text>;

  const s = status.data.stage1;

  const segmentsPending =
    (s.segments_by_status.pending ?? 0) +
    (s.segments_by_status.coding ?? 0);
  const segmentsAggregating = s.segments_by_status.aggregating ?? 0;
  const segmentsReviewing = s.segments_by_status.reviewing ?? 0;
  const segmentsDone = s.segments_by_status.done ?? 0;

  const codingQueuePending =
    (s.coding_queue_by_status.pending ?? 0) +
    (s.coding_queue_by_status.running ?? 0);
  const codingQueueDone = s.coding_queue_by_status.done ?? 0;
  const codingQueueFailed = s.coding_queue_by_status.failed ?? 0;

  const reviewsTotal = s.reviews_pending + s.reviews_completed_since_codebook;

  return (
    <Stack gap="md">
      <Title order={2}>Coding progress</Title>

      <BackgroundRunnerCard />

      <SimpleGrid cols={{ base: 2, sm: 3, md: 4 }} spacing="md">
        <StatCard
          label="Documents remaining"
          value={
            (s.documents_by_coding_status.not_coded ?? 0) +
            (s.documents_by_coding_status.partially_coded ?? 0)
          }
          hint={`of ${s.documents_total}`}
        />
        <StatCard
          label="Segments remaining"
          value={segmentsPending + segmentsAggregating + segmentsReviewing}
          hint={`of ${s.segments_total}`}
        />
        <StatCard
          label="Coding jobs queued"
          value={codingQueuePending}
          hint={`${codingQueueDone} done${codingQueueFailed ? `, ${codingQueueFailed} failed` : ""}`}
        />
        <StatCard
          label="Aggregations remaining"
          value={segmentsAggregating}
          hint={`${segmentsDone + segmentsReviewing} already aggregated`}
        />
      </SimpleGrid>

      <Card padding="md">
        <Stack gap="sm">
          <Title order={4}>Documents</Title>
          <StatusBar
            counts={{
              "fully coded":
                s.documents_by_coding_status.fully_coded ?? 0,
              "partially coded":
                s.documents_by_coding_status.partially_coded ?? 0,
              "not coded":
                s.documents_by_coding_status.not_coded ?? 0,
            }}
          />
        </Stack>
      </Card>

      <Card padding="md">
        <Stack gap="sm">
          <Title order={4}>Segments</Title>
          <StatusBar counts={s.segments_by_status} />
          <Text size="xs" c="dimmed">
            pending/coding: queued for coding. aggregating: all coders
            done, aggregation not yet run. reviewing: aggregated but at
            least one aggregator code still needs reviewing.
          </Text>
        </Stack>
      </Card>

      <Card padding="md">
        <Stack gap="sm">
          <Title order={4}>Coding queue</Title>
          <StatusBar counts={s.coding_queue_by_status} />
          <Text size="xs" c="dimmed">
            One entry per (segment, coder, codebook revision). Pending and
            running entries are work the coder agents still need to do.
          </Text>
        </Stack>
      </Card>

      <Card padding="md">
        <Stack gap="sm">
          <Group justify="space-between">
            <Title order={4}>Reviews since codebook v{s.codebook_version}</Title>
            <Text size="xs" c="dimmed">
              {s.reviews_completed_since_codebook} done /{" "}
              {s.reviews_pending} pending
            </Text>
          </Group>
          {reviewsTotal === 0 ? (
            <Text size="sm" c="dimmed">
              No reviews pending or completed since the latest codebook
              revision.
            </Text>
          ) : (
            <StatusBar
              counts={{
                pending: s.reviews_pending,
                done: s.reviews_completed_since_codebook,
              }}
            />
          )}
          <Text size="xs" c="dimmed">
            Reviews completed since the last codebook revision will be
            integrated into the codebook when it is next materialized.
          </Text>
        </Stack>
      </Card>

      <Card padding="md">
        <Stack gap="sm">
          <Group justify="space-between">
            <Title order={4}>
              Next codebook preview
              {preview.data?.parent_version != null && (
                <Text component="span" size="sm" c="dimmed" ml="xs">
                  (would become v{preview.data.parent_version + 1})
                </Text>
              )}
            </Title>
            {preview.data && (
              <Text size="xs" c="dimmed">
                {preview.data.added.length} added ·{" "}
                {preview.data.unchanged_count} unchanged
              </Text>
            )}
          </Group>
          <Text size="xs" c="dimmed">
            Reviewer decisions made since the latest codebook revision, in
            the form they would take when the codebook is next
            materialized. Same mechanism used by{" "}
            <CodeText>finalize-codebook</CodeText>.
          </Text>
          {preview.error && <ErrorAlert error={preview.error} />}
          {preview.isLoading && <Text c="dimmed">Loading…</Text>}
          {preview.data && !preview.data.has_changes && (
            <Text size="sm" c="dimmed">
              No reviewed code changes pending. The next codebook would be
              identical to v{preview.data.parent_version}.
            </Text>
          )}
          {preview.data && preview.data.added.length > 0 && (
            <Stack gap="xs">
              {preview.data.added.map((c) => (
                <AddedCodeRow key={c.code_id} c={c} />
              ))}
            </Stack>
          )}
        </Stack>
      </Card>

      <Card padding="md">
        <Stack gap="sm">
          <Group justify="space-between">
            <Title order={4}>Most recent codes</Title>
            <Text size="xs" c="dimmed">
              latest 20, newest first
            </Text>
          </Group>
          {recent.error && <ErrorAlert error={recent.error} />}
          {recent.isLoading && <Text c="dimmed">Loading…</Text>}
          {recent.data && recent.data.length === 0 && (
            <Text c="dimmed" size="sm">
              No codes yet.
            </Text>
          )}
          {recent.data && recent.data.length > 0 && (
            <Table verticalSpacing="xs" striped>
              <Table.Thead>
                <Table.Tr>
                  <Table.Th>Code</Table.Th>
                  <Table.Th>Source</Table.Th>
                  <Table.Th>Author</Table.Th>
                  <Table.Th>Segment</Table.Th>
                </Table.Tr>
              </Table.Thead>
              <Table.Tbody>
                {recent.data.map((r) => (
                  <Table.Tr key={r.code_id}>
                    <Table.Td>
                      <Anchor component={Link} to={`/code/${r.code_id}`}>
                        <CodeText style={{ fontSize: 13 }}>{r.code}</CodeText>
                      </Anchor>
                    </Table.Td>
                    <Table.Td>
                      <Badge size="sm" color={kindColor(r.kind)} variant="light">
                        {kindLabel(r.kind)}
                      </Badge>
                    </Table.Td>
                    <Table.Td>
                      <Text size="xs" c="dimmed">
                        {coderLabel(r)}
                      </Text>
                    </Table.Td>
                    <Table.Td>
                      {r.segment_id != null ? (
                        <Anchor
                          component={Link}
                          to={
                            r.document_id
                              ? `/documents/${r.document_id}?segment=${r.segment_id}`
                              : `/segments/${r.segment_id}`
                          }
                          size="xs"
                          ff="monospace"
                        >
                          {r.document_filename ?? "(no document)"}#
                          {r.segment_id}
                        </Anchor>
                      ) : (
                        <Text size="xs" c="dimmed">
                          —
                        </Text>
                      )}
                    </Table.Td>
                  </Table.Tr>
                ))}
              </Table.Tbody>
            </Table>
          )}
        </Stack>
      </Card>
    </Stack>
  );
}
