import {
  ActionIcon,
  Anchor,
  Card,
  Group,
  Pagination,
  Select,
  Stack,
  Table,
  Text,
  Title,
  Tooltip,
} from "@mantine/core";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { IconTrash } from "@tabler/icons-react";
import { useState } from "react";
import { Link } from "react-router-dom";
import { notifications } from "@mantine/notifications";
import { api } from "../api";
import { ErrorAlert } from "../components/ErrorAlert";
import { StatusBadge } from "../components/StatusBadge";
import { useConfirmDelete } from "../components/ConfirmDelete";

const PAGE_SIZE = 50;

export function Aggregations() {
  const qc = useQueryClient();
  const [page, setPage] = useState(1);
  const [status, setStatus] = useState<string | null>(null);
  const { data, error } = useQuery({
    queryKey: ["aggregations", page, status],
    queryFn: () =>
      api.aggregations({
        status: status ?? undefined,
        limit: PAGE_SIZE,
        offset: (page - 1) * PAGE_SIZE,
      }),
  });

  const del = useMutation({
    mutationFn: (id: number) => api.deleteAggregation(id),
    onSuccess: () => {
      notifications.show({ message: "Aggregation deleted", color: "teal" });
      qc.invalidateQueries({ queryKey: ["aggregations"] });
      qc.invalidateQueries({ queryKey: ["status"] });
    },
    onError: (e) =>
      notifications.show({
        message: e instanceof Error ? e.message : String(e),
        color: "red",
      }),
  });
  const confirm = useConfirmDelete();

  if (error) return <ErrorAlert error={error} />;

  return (
    <Stack gap="md">
      {confirm.modal}
      <Title order={2}>Aggregations</Title>
      <Card padding="md">
        <Group>
          <Select
            label="Status"
            placeholder="All"
            clearable
            data={["pending", "done", "failed"]}
            value={status}
            onChange={(v) => {
              setStatus(v);
              setPage(1);
            }}
            style={{ width: 180 }}
          />
          <Text size="sm" c="dimmed" ml="auto" mt={24}>
            {data?.total ?? 0} aggregation(s)
          </Text>
        </Group>
      </Card>
      <Card padding={0}>
        <Table verticalSpacing="xs" striped highlightOnHover>
          <Table.Thead>
            <Table.Tr>
              <Table.Th>ID</Table.Th>
              <Table.Th>Segment</Table.Th>
              <Table.Th>Status</Table.Th>
              <Table.Th ta="right"># codes</Table.Th>
              <Table.Th>Created</Table.Th>
              <Table.Th>Finished</Table.Th>
              <Table.Th>Error</Table.Th>
              <Table.Th></Table.Th>
            </Table.Tr>
          </Table.Thead>
          <Table.Tbody>
            {data?.items.map((a) => (
              <Table.Tr key={a.id}>
                <Table.Td>
                  <Text size="xs" c="dimmed">
                    #{a.id}
                  </Text>
                </Table.Td>
                <Table.Td>
                  <Anchor component={Link} to={`/segments/${a.segment_id}`} size="sm">
                    {a.segment_id}
                  </Anchor>
                </Table.Td>
                <Table.Td>
                  <StatusBadge status={a.status} />
                </Table.Td>
                <Table.Td ta="right">{a.n_codes}</Table.Td>
                <Table.Td>
                  <Text size="xs" c="dimmed">
                    {a.created_at}
                  </Text>
                </Table.Td>
                <Table.Td>
                  <Text size="xs" c="dimmed">
                    {a.finished_at ?? "—"}
                  </Text>
                </Table.Td>
                <Table.Td>
                  {a.error && (
                    <Text size="xs" c="red" lineClamp={1}>
                      {a.error}
                    </Text>
                  )}
                </Table.Td>
                <Table.Td>
                  <Tooltip label="Delete (will be re-aggregated)">
                    <ActionIcon
                      variant="subtle"
                      color="red"
                      onClick={() =>
                        confirm.ask({
                          title: `Delete aggregation #${a.id}?`,
                          body: "Cascades to aggregated_codes and any review decisions for them.",
                          onConfirm: () => del.mutateAsync(a.id),
                        })
                      }
                    >
                      <IconTrash size={16} />
                    </ActionIcon>
                  </Tooltip>
                </Table.Td>
              </Table.Tr>
            ))}
            {data && data.items.length === 0 && (
              <Table.Tr>
                <Table.Td colSpan={8}>
                  <Text c="dimmed" ta="center" py="md">
                    No aggregations.
                  </Text>
                </Table.Td>
              </Table.Tr>
            )}
          </Table.Tbody>
        </Table>
      </Card>
      {data && data.total > PAGE_SIZE && (
        <Group justify="center">
          <Pagination
            total={Math.ceil(data.total / PAGE_SIZE)}
            value={page}
            onChange={setPage}
          />
        </Group>
      )}
    </Stack>
  );
}
