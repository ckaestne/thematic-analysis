import {
  Anchor,
  Card,
  Stack,
  Table,
  Text,
  Title,
} from "@mantine/core";
import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { api } from "../api";
import { ErrorAlert } from "../components/ErrorAlert";

export function Documents() {
  const { data, error, isLoading } = useQuery({
    queryKey: ["documents"],
    queryFn: api.documents,
  });

  if (error) return <ErrorAlert error={error} />;

  return (
    <Stack gap="md">
      <Title order={2}>Documents</Title>
      <Card padding={0}>
        <Table verticalSpacing="sm" highlightOnHover>
          <Table.Thead>
            <Table.Tr>
              <Table.Th>ID</Table.Th>
              <Table.Th>Filename</Table.Th>
              <Table.Th>Segments</Table.Th>
              <Table.Th>Size</Table.Th>
              <Table.Th>Added</Table.Th>
            </Table.Tr>
          </Table.Thead>
          <Table.Tbody>
            {isLoading && (
              <Table.Tr>
                <Table.Td colSpan={5}>Loading…</Table.Td>
              </Table.Tr>
            )}
            {data?.items.map((d) => (
              <Table.Tr key={d.document_id}>
                <Table.Td>
                  <Anchor component={Link} to={`/documents/${d.document_id}`}>
                    {d.document_id}
                  </Anchor>
                </Table.Td>
                <Table.Td>
                  <Anchor component={Link} to={`/documents/${d.document_id}`}>
                    {d.filename}
                  </Anchor>
                </Table.Td>
                <Table.Td>{d.segments_total}</Table.Td>
                <Table.Td>
                  <Text size="xs" c="dimmed">
                    {formatBytes(d.size_bytes)}
                  </Text>
                </Table.Td>
                <Table.Td>
                  <Text size="xs" c="dimmed">
                    {d.created_at}
                  </Text>
                </Table.Td>
              </Table.Tr>
            ))}
            {data && data.items.length === 0 && (
              <Table.Tr>
                <Table.Td colSpan={5}>
                  <Text c="dimmed" ta="center" py="md">
                    No documents added yet.
                  </Text>
                </Table.Td>
              </Table.Tr>
            )}
          </Table.Tbody>
        </Table>
      </Card>
    </Stack>
  );
}

function formatBytes(n: number): string {
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  return `${(n / 1024 / 1024).toFixed(2)} MB`;
}
