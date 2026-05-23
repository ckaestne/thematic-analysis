import { Anchor, Breadcrumbs, Stack, Text } from "@mantine/core";
import { useQuery } from "@tanstack/react-query";
import { Link, useParams, useSearchParams } from "react-router-dom";
import { api } from "../api";
import { ErrorAlert } from "../components/ErrorAlert";
import { SegmentCodingCard } from "../components/SegmentCodingCard";

export function SegmentDetail() {
  const { id = "" } = useParams();
  const [params] = useSearchParams();
  const focusQuoteId = params.get("quote");

  const { data, error, isLoading } = useQuery({
    queryKey: ["segment", id],
    queryFn: () => api.segment(id),
  });
  const quote = useQuery({
    queryKey: ["quote", focusQuoteId],
    queryFn: () => api.quote(Number(focusQuoteId)),
    enabled: !!focusQuoteId,
  });

  if (error) return <ErrorAlert error={error} />;
  if (isLoading || !data) return <Text c="dimmed">Loading…</Text>;

  return (
    <Stack gap="md">
      <Breadcrumbs>
        <Anchor component={Link} to="/documents">
          Documents
        </Anchor>
        {data.document_id != null && (
          <Anchor component={Link} to={`/documents/${data.document_id}`}>
            {data.document_filename ?? `doc ${data.document_id}`}
          </Anchor>
        )}
        <Text>segment {data.segment_id}</Text>
      </Breadcrumbs>

      <SegmentCodingCard
        segment={data}
        invalidateKeys={[["segment", id], ["documents"]]}
        highlight={quote.data ? [quote.data.text] : undefined}
      />
    </Stack>
  );
}
