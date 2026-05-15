import {
  Anchor,
  Breadcrumbs,
  Card,
  Divider,
  Group,
  Stack,
  Text,
  Title,
} from "@mantine/core";
import { useQuery } from "@tanstack/react-query";
import { Link, useParams } from "react-router-dom";
import { api } from "../api";
import { ErrorAlert } from "../components/ErrorAlert";
import { StatusBadge } from "../components/StatusBadge";

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
          {data.segments.map((s, i) => {
            const words = s.text.split(/\s+/).filter(Boolean).length;
            return (
              <Card key={s.segment_id} padding="md" withBorder>
                <Group justify="space-between" align="flex-start" wrap="nowrap">
                  <Stack gap={2} style={{ flex: 1, minWidth: 0 }}>
                    <Group gap="xs" wrap="nowrap">
                      <Text size="xs" c="dimmed" ff="monospace">
                        [{i + 1}/{data.segments.length}]
                      </Text>
                      <Anchor
                        component={Link}
                        to={`/segments/${s.segment_id}`}
                        size="sm"
                        ff="monospace"
                      >
                        {s.segment_id}
                      </Anchor>
                      <Text size="xs" c="dimmed">
                        {words} words · {s.len} chars
                      </Text>
                    </Group>
                    {s.title && (
                      <Text size="md" fw={600}>
                        {s.title}
                      </Text>
                    )}
                  </Stack>
                  <StatusBadge status={s.status} />
                </Group>
                <Divider my="xs" />
                <Text
                  size="sm"
                  style={{
                    whiteSpace: "pre-wrap",
                    fontFamily: "Georgia, serif",
                  }}
                >
                  {s.text}
                </Text>
              </Card>
            );
          })}
        </Stack>
      )}
    </Stack>
  );
}
