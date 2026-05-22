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
import { useEffect } from "react";
import { Link, useParams, useSearchParams } from "react-router-dom";
import { api } from "../api";
import { ErrorAlert } from "../components/ErrorAlert";
import { SegmentCodingCard } from "../components/SegmentCodingCard";

export function DocumentDetail() {
  const { id } = useParams<{ id: string }>();
  const documentId = Number(id);
  const [params] = useSearchParams();
  const focusSegmentId = params.get("segment");
  const focusQuoteId = params.get("quote");

  const { data, error, isLoading } = useQuery({
    queryKey: ["document", documentId],
    queryFn: () => api.document(documentId),
    enabled: Number.isFinite(documentId),
  });

  // Fetch the highlighted quote's text (if any) so we can <mark> it in
  // the matching segment.
  const quote = useQuery({
    queryKey: ["quote", focusQuoteId],
    queryFn: () => api.quote(Number(focusQuoteId)),
    enabled: !!focusQuoteId,
  });

  // Scroll the focused segment into view once the data has rendered.
  useEffect(() => {
    if (!focusSegmentId || !data) return;
    const el = document.getElementById(`segment-${focusSegmentId}`);
    if (el) {
      el.scrollIntoView({ behavior: "smooth", block: "start" });
    }
  }, [focusSegmentId, data]);

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
          {data.segments.map((s, i) => {
            const isFocus = focusSegmentId === String(s.segment_id);
            return (
              <div
                key={s.segment_id}
                id={`segment-${s.segment_id}`}
                style={
                  isFocus
                    ? {
                        outline:
                          "2px solid var(--mantine-color-yellow-5)",
                        borderRadius: 6,
                      }
                    : undefined
                }
              >
                <SegmentCodingCard
                  segment={s}
                  invalidateKeys={invalidateKeys}
                  index={{ i, total: data.segments.length }}
                  highlight={
                    isFocus && quote.data ? [quote.data.text] : undefined
                  }
                />
              </div>
            );
          })}
        </Stack>
      )}
    </Stack>
  );
}
