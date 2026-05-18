import { Anchor, Breadcrumbs, Stack, Text } from "@mantine/core";
import { useQuery } from "@tanstack/react-query";
import { Link, useParams } from "react-router-dom";
import { api } from "../api";
import { ErrorAlert } from "../components/ErrorAlert";
import { SegmentCodingCard } from "../components/SegmentCodingCard";

export function SegmentDetail() {
  const { id = "" } = useParams();
  const { data, error, isLoading } = useQuery({
    queryKey: ["segment", id],
    queryFn: () => api.segment(id),
  });

  if (error) return <ErrorAlert error={error} />;
  if (isLoading || !data) return <Text c="dimmed">Loading…</Text>;

  return (
    <Stack gap="md">
      <Breadcrumbs>
        {data.document_id != null ? (
          <Anchor component={Link} to={`/documents/${data.document_id}`}>
            Documents
          </Anchor>
        ) : (
          <Anchor component={Link} to="/documents">
            Documents
          </Anchor>
        )}
        <Text>{data.segment_id}</Text>
      </Breadcrumbs>

      <SegmentCodingCard
        segment={data}
        invalidateKeys={[["segment", id], ["documents"]]}
      />
    </Stack>
  );
}
