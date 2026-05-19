import {
  ActionIcon,
  Anchor,
  Badge,
  Card,
  Group,
  Pagination,
  Select,
  Stack,
  Table,
  Text,
  TextInput,
  Title,
  Tooltip,
} from "@mantine/core";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { IconSearch, IconTrash } from "@tabler/icons-react";
import { useState } from "react";
import { Link } from "react-router-dom";
import { notifications } from "@mantine/notifications";
import { api } from "../api";
import { ErrorAlert } from "../components/ErrorAlert";
import { StatusBadge } from "../components/StatusBadge";
import { StatusBar } from "../components/StatusBar";
import { useConfirmDelete } from "../components/ConfirmDelete";

const PAGE_SIZE = 50;

export function Segments() {
  const qc = useQueryClient();
  const [page, setPage] = useState(1);
  const [status, setStatus] = useState<string | null>(null);
  const [q, setQ] = useState("");

  const { data, error } = useQuery({
    queryKey: ["segments", page, status, q],
    queryFn: () =>
      api.segments({
        status: status ?? undefined,
        q: q || undefined,
        limit: PAGE_SIZE,
        offset: (page - 1) * PAGE_SIZE,
      }),
  });

  const del = useMutation({
    mutationFn: (id: string) => api.deleteSegment(id),
    onSuccess: () => {
      notifications.show({ message: "Segment deleted", color: "teal" });
      qc.invalidateQueries({ queryKey: ["segments"] });
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

  const statuses = data ? Object.keys(data.status_counts) : [];

  return (
    <Stack gap="md">
      {confirm.modal}
      <Title order={2}>Segments</Title>
      <Card padding="md">
        <Stack gap="sm">
          <StatusBar counts={data?.status_counts ?? {}} />
          <Group>
            <TextInput
              placeholder="Filter by id or text…"
              leftSection={<IconSearch size={16} />}
              value={q}
              onChange={(e) => {
                setQ(e.currentTarget.value);
                setPage(1);
              }}
              style={{ flex: 1, maxWidth: 360 }}
            />
            <Select
              placeholder="All statuses"
              clearable
              data={statuses}
              value={status}
              onChange={(v) => {
                setStatus(v);
                setPage(1);
              }}
              style={{ width: 180 }}
            />
            <Text size="sm" c="dimmed" ml="auto">
              {data?.total ?? 0} segment(s)
            </Text>
          </Group>
        </Stack>
      </Card>

      <Card padding={0}>
        <Table verticalSpacing="xs" striped highlightOnHover>
          <Table.Thead>
            <Table.Tr>
              <Table.Th>ID</Table.Th>
              <Table.Th>Status</Table.Th>
              <Table.Th>Batch</Table.Th>
              <Table.Th>Preview</Table.Th>
              <Table.Th ta="right">Length</Table.Th>
              <Table.Th></Table.Th>
            </Table.Tr>
          </Table.Thead>
          <Table.Tbody>
            {data?.items.map((s) => (
              <Table.Tr key={s.segment_id}>
                <Table.Td>
                  <Stack gap={2}>
                    <Anchor component={Link} to={`/segments/${s.segment_id}`}>
                      {s.segment_id}
                    </Anchor>
                    {s.title ? (
                      <Text size="sm" fw={600} lineClamp={1}>
                        {s.title}
                      </Text>
                    ) : null}
                  </Stack>
                </Table.Td>
                <Table.Td>
                  <StatusBadge status={s.status} />
                </Table.Td>
                <Table.Td>
                  {s.batch != null ? <Badge variant="default">{s.batch}</Badge> : "—"}
                </Table.Td>
                <Table.Td>
                  <Text size="sm" lineClamp={2}>
                    {s.preview}
                  </Text>
                </Table.Td>
                <Table.Td ta="right">
                  <Text size="xs" c="dimmed">
                    {s.len}
                  </Text>
                </Table.Td>
                <Table.Td>
                  <Tooltip label="Delete segment and all derived data">
                    <ActionIcon
                      variant="subtle"
                      color="red"
                      onClick={() =>
                        confirm.ask({
                          title: `Delete segment ${s.segment_id}?`,
                          body: `This permanently deletes the segment and all coder runs, aggregations, and review decisions derived from it. The codebook is not affected.`,
                          onConfirm: () => del.mutateAsync(s.segment_id),
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
                <Table.Td colSpan={6}>
                  <Text c="dimmed" ta="center" py="md">
                    No segments match.
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
