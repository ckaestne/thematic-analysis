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
import { Link, useSearchParams } from "react-router-dom";
import { notifications } from "@mantine/notifications";
import { api } from "../api";
import { ErrorAlert } from "../components/ErrorAlert";
import { StatusBadge } from "../components/StatusBadge";
import { useConfirmDelete } from "../components/ConfirmDelete";

const PAGE_SIZE = 50;

export function CoderRuns() {
  const qc = useQueryClient();
  const [params, setParams] = useSearchParams();
  const coderId = params.get("coder_id") ?? "";
  const status = params.get("status") ?? "";
  const [page, setPage] = useState(1);

  const coders = useQuery({ queryKey: ["coders"], queryFn: api.coders });
  const runs = useQuery({
    queryKey: ["coder-runs", coderId, status, page],
    queryFn: () =>
      api.coderRuns({
        coder_id: coderId || undefined,
        status: status || undefined,
        limit: PAGE_SIZE,
        offset: (page - 1) * PAGE_SIZE,
      }),
  });

  const del = useMutation({
    mutationFn: (id: number) => api.deleteCoderRun(id),
    onSuccess: () => {
      notifications.show({ message: "Run deleted", color: "teal" });
      qc.invalidateQueries({ queryKey: ["coder-runs"] });
      qc.invalidateQueries({ queryKey: ["status"] });
    },
    onError: (e) =>
      notifications.show({
        message: e instanceof Error ? e.message : String(e),
        color: "red",
      }),
  });

  const confirm = useConfirmDelete();

  if (runs.error) return <ErrorAlert error={runs.error} />;

  const setParam = (k: string, v: string | null) => {
    const next = new URLSearchParams(params);
    if (v) next.set(k, v);
    else next.delete(k);
    setParams(next);
    setPage(1);
  };

  return (
    <Stack gap="md">
      {confirm.modal}
      <Title order={2}>Coder runs</Title>
      <Card padding="md">
        <Group>
          <Select
            label="Coder"
            placeholder="All"
            clearable
            data={
              coders.data?.map((c) => ({
                value: String(c.coder_id),
                label: c.identity ?? String(c.coder_id),
              })) ?? []
            }
            value={coderId || null}
            onChange={(v) => setParam("coder_id", v)}
            style={{ width: 220 }}
          />
          <Select
            label="Status"
            placeholder="All"
            clearable
            data={["running", "done", "failed"]}
            value={status || null}
            onChange={(v) => setParam("status", v)}
            style={{ width: 180 }}
          />
          <Text size="sm" c="dimmed" ml="auto" mt={24}>
            {runs.data?.total ?? 0} run(s)
          </Text>
        </Group>
      </Card>

      <Card padding={0}>
        <Table verticalSpacing="xs" striped highlightOnHover>
          <Table.Thead>
            <Table.Tr>
              <Table.Th>ID</Table.Th>
              <Table.Th>Segment</Table.Th>
              <Table.Th>Coder</Table.Th>
              <Table.Th>Status</Table.Th>
              <Table.Th>Codebook</Table.Th>
              <Table.Th ta="right"># codes</Table.Th>
              <Table.Th>Finished</Table.Th>
              <Table.Th></Table.Th>
            </Table.Tr>
          </Table.Thead>
          <Table.Tbody>
            {runs.data?.items.map((r) => (
              <Table.Tr key={r.id}>
                <Table.Td>
                  <Text size="xs" c="dimmed">
                    #{r.id}
                  </Text>
                </Table.Td>
                <Table.Td>
                  <Anchor component={Link} to={`/segments/${r.segment_id}`} size="sm">
                    {r.segment_id}
                  </Anchor>
                </Table.Td>
                <Table.Td>{r.coder_id}</Table.Td>
                <Table.Td>
                  <StatusBadge status={r.status} />
                </Table.Td>
                <Table.Td>
                  <Text size="xs">v{r.codebook_version}</Text>
                </Table.Td>
                <Table.Td ta="right">{r.n_codes}</Table.Td>
                <Table.Td>
                  <Text size="xs" c="dimmed">
                    {r.finished_at ?? "—"}
                  </Text>
                </Table.Td>
                <Table.Td>
                  <Tooltip label="Delete run (will be re-coded)">
                    <ActionIcon
                      variant="subtle"
                      color="red"
                      onClick={() =>
                        confirm.ask({
                          title: `Delete run #${r.id}?`,
                          body: `Deletes this run by ${r.coder_id} on ${r.segment_id} and any downstream aggregation. The pipeline will recompute them.`,
                          onConfirm: () => del.mutateAsync(r.id),
                        })
                      }
                    >
                      <IconTrash size={16} />
                    </ActionIcon>
                  </Tooltip>
                </Table.Td>
              </Table.Tr>
            ))}
            {runs.data && runs.data.items.length === 0 && (
              <Table.Tr>
                <Table.Td colSpan={9}>
                  <Text c="dimmed" ta="center" py="md">
                    No runs match.
                  </Text>
                </Table.Td>
              </Table.Tr>
            )}
          </Table.Tbody>
        </Table>
      </Card>

      {runs.data && runs.data.total > PAGE_SIZE && (
        <Group justify="center">
          <Pagination
            total={Math.ceil(runs.data.total / PAGE_SIZE)}
            value={page}
            onChange={setPage}
          />
        </Group>
      )}
    </Stack>
  );
}
