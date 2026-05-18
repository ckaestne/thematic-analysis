import {
  Anchor,
  Breadcrumbs,
  Card,
  Group,
  Stack,
  Text,
  Title,
} from "@mantine/core";
import { useQuery } from "@tanstack/react-query";
import { Link, useParams } from "react-router-dom";
import { api } from "../api";
import { ErrorAlert } from "../components/ErrorAlert";
import { SegmentCodingCard } from "../components/SegmentCodingCard";

export function DocumentDetail() {
  const { id } = useParams<{ id: string }>();
  const documentId = Number(id);
  const { data, error, isLoading } = useQuery({
    queryKey: ["document", documentId],
    queryFn: () => api.document(documentId),
    enabled: Number.isFinite(documentId),
  });

  if (error) return <ErrorAlert error={error} />;
  if (isLoading || !data) return <Text c="dimmed">Loading…</Text>;

  const invalidateKeys: ReadonlyArray<ReadonlyArray<unknown>> = [
    ["document", documentId],
    ["documents"],
  ];

  return (
    <Stack gap="md">
      <Breadcrumbs>
        <Anchor component={Link} to="/documents">
          Documents
        </Anchor>
        <Text>{data.filename}</Text>
      </Breadcrumbs>

      <Group justify="space-between" align="flex-end">
        <Title order={2}>{data.filename}</Title>
        <Group gap="lg">
          <Text size="sm" c="dimmed">
            doc_id {data.document_id}
          </Text>
          <Text size="sm" c="dimmed">
            {data.segments.length} segment{data.segments.length === 1 ? "" : "s"}
          </Text>
          <Text size="sm" c="dimmed">
            added {data.created_at}
          </Text>
        </Group>
      </Group>

      {data.segments.length === 0 ? (
        <Card padding="md">
          <Text c="dimmed" ta="center">
            No segments linked to this document.
          </Text>
        </Card>
      ) : (
        <Stack gap="sm">
          {data.segments.map((s, i) => (
            <SegmentCodingCard
              key={s.segment_id}
              segment={s}
              invalidateKeys={invalidateKeys}
              index={{ i, total: data.segments.length }}
            />
          ))}
        </Stack>
      )}
    </Stack>
  );
}
