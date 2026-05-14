import {
  ActionIcon,
  Anchor,
  Badge,
  Card,
  Code,
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
import { useConfirmDelete } from "../components/ConfirmDelete";

const PAGE_SIZE = 50;

export function ReviewDecisions() {
  const qc = useQueryClient();
  const [page, setPage] = useState(1);
  const [decision, setDecision] = useState<string | null>(null);
  const { data, error } = useQuery({
    queryKey: ["review-decisions", page, decision],
    queryFn: () =>
      api.reviewDecisions({
        decision: decision ?? undefined,
        limit: PAGE_SIZE,
        offset: (page - 1) * PAGE_SIZE,
      }),
  });
  const del = useMutation({
    mutationFn: (id: number) => api.deleteReviewDecision(id),
    onSuccess: () => {
      notifications.show({ message: "Decision deleted", color: "teal" });
      qc.invalidateQueries({ queryKey: ["review-decisions"] });
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
      <Title order={2}>Review decisions</Title>
      <Card padding="md">
        <Group>
          <Select
            label="Decision"
            placeholder="All"
            clearable
            data={["add", "merge", "update", "skip"]}
            value={decision}
            onChange={(v) => {
              setDecision(v);
              setPage(1);
            }}
            style={{ width: 180 }}
          />
          <Text size="sm" c="dimmed" ml="auto" mt={24}>
            {data?.total ?? 0} decision(s)
          </Text>
        </Group>
      </Card>
      <Card padding={0}>
        <Table verticalSpacing="xs" striped highlightOnHover>
          <Table.Thead>
            <Table.Tr>
              <Table.Th>ID</Table.Th>
              <Table.Th>Segment</Table.Th>
              <Table.Th>Aggregated code</Table.Th>
              <Table.Th>Decision</Table.Th>
              <Table.Th>Target</Table.Th>
              <Table.Th>Rationale</Table.Th>
              <Table.Th>v</Table.Th>
              <Table.Th></Table.Th>
            </Table.Tr>
          </Table.Thead>
          <Table.Tbody>
            {data?.items.map((d) => (
              <Table.Tr key={d.id}>
                <Table.Td>
                  <Text size="xs" c="dimmed">
                    #{d.id}
                  </Text>
                </Table.Td>
                <Table.Td>
                  <Anchor component={Link} to={`/segments/${d.segment_id}`} size="sm">
                    {d.segment_id}
                  </Anchor>
                </Table.Td>
                <Table.Td>
                  <Code>{d.aggregated_code}</Code>
                </Table.Td>
                <Table.Td>
                  <Badge
                    color={
                      d.applied
                        ? d.decision === "skip"
                          ? "gray"
                          : "teal"
                        : "yellow"
                    }
                    variant="light"
                  >
                    {d.decision}
                  </Badge>
                </Table.Td>
                <Table.Td>
                  {d.target_code ? <Code>{d.target_code}</Code> : "—"}
                </Table.Td>
                <Table.Td>
                  <Text size="xs" lineClamp={2}>
                    {d.rationale}
                  </Text>
                </Table.Td>
                <Table.Td>
                  {d.resulting_version ? (
                    <Anchor
                      component={Link}
                      to={`/codebook/${d.resulting_version}`}
                      size="xs"
                    >
                      v{d.resulting_version}
                    </Anchor>
                  ) : (
                    "—"
                  )}
                </Table.Td>
                <Table.Td>
                  <Tooltip label="Delete decision (will be re-reviewed)">
                    <ActionIcon
                      variant="subtle"
                      color="red"
                      onClick={() =>
                        confirm.ask({
                          title: `Delete decision #${d.id}?`,
                          body: "The codebook is not rolled back. The aggregated code will simply be picked up by the next review pass.",
                          onConfirm: () => del.mutateAsync(d.id),
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
                    No review decisions.
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
