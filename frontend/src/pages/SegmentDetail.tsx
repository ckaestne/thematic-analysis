import {
  ActionIcon,
  Alert,
  Anchor,
  Badge,
  Breadcrumbs,
  Button,
  Card,
  Code,
  Group,
  Modal,
  Stack,
  Table,
  Text,
  TextInput,
  Title,
  Tooltip,
} from "@mantine/core";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  IconAlertTriangle,
  IconEdit,
  IconExternalLink,
  IconTrash,
} from "@tabler/icons-react";
import { useState } from "react";
import { Link, useParams } from "react-router-dom";
import { notifications } from "@mantine/notifications";
import { api } from "../api";
import { ErrorAlert } from "../components/ErrorAlert";
import { StatusBadge } from "../components/StatusBadge";
import { useConfirmDelete } from "../components/ConfirmDelete";

export function SegmentDetail() {
  const { id = "" } = useParams();
  const qc = useQueryClient();
  const { data, error, isLoading } = useQuery({
    queryKey: ["segment", id],
    queryFn: () => api.segment(id),
  });
  const confirm = useConfirmDelete();

  const [editing, setEditing] = useState<
    | { kind: "coder"; run_id: number; position: number; code: string }
    | { kind: "agg"; id: number; code: string }
    | null
  >(null);

  const saveCoderCode = useMutation({
    mutationFn: (v: { run_id: number; position: number; code: string }) =>
      api.editCoderCode(v.run_id, v.position, v.code),
    onSuccess: () => {
      notifications.show({ message: "Code updated", color: "teal" });
      setEditing(null);
      qc.invalidateQueries({ queryKey: ["segment", id] });
    },
    onError: (e) =>
      notifications.show({
        message: e instanceof Error ? e.message : String(e),
        color: "red",
      }),
  });

  const saveAggCode = useMutation({
    mutationFn: (v: { id: number; code: string }) =>
      api.editAggregatedCode(v.id, v.code),
    onSuccess: () => {
      notifications.show({ message: "Code updated", color: "teal" });
      setEditing(null);
      qc.invalidateQueries({ queryKey: ["segment", id] });
    },
    onError: (e) =>
      notifications.show({
        message: e instanceof Error ? e.message : String(e),
        color: "red",
      }),
  });

  const delCoderRun = useMutation({
    mutationFn: (run_id: number) => api.deleteCoderRun(run_id),
    onSuccess: () => {
      notifications.show({
        message: "Run deleted — segment will be re-coded.",
        color: "teal",
      });
      qc.invalidateQueries({ queryKey: ["segment", id] });
      qc.invalidateQueries({ queryKey: ["status"] });
    },
    onError: (e) =>
      notifications.show({
        message: e instanceof Error ? e.message : String(e),
        color: "red",
      }),
  });

  const delAgg = useMutation({
    mutationFn: (agg_id: number) => api.deleteAggregation(agg_id),
    onSuccess: () => {
      notifications.show({ message: "Aggregation deleted", color: "teal" });
      qc.invalidateQueries({ queryKey: ["segment", id] });
      qc.invalidateQueries({ queryKey: ["status"] });
    },
    onError: (e) =>
      notifications.show({
        message: e instanceof Error ? e.message : String(e),
        color: "red",
      }),
  });

  const delAggCode = useMutation({
    mutationFn: (ac_id: number) => api.deleteAggregatedCode(ac_id),
    onSuccess: () => {
      notifications.show({ message: "Code deleted", color: "teal" });
      qc.invalidateQueries({ queryKey: ["segment", id] });
    },
    onError: (e) =>
      notifications.show({
        message: e instanceof Error ? e.message : String(e),
        color: "red",
      }),
  });

  if (error) return <ErrorAlert error={error} />;
  if (isLoading || !data) return <Text c="dimmed">Loading…</Text>;

  return (
    <Stack gap="md">
      {confirm.modal}

      <Modal
        opened={editing !== null}
        onClose={() => setEditing(null)}
        title="Edit code"
        centered
      >
        {editing && (
          <Stack>
            <TextInput
              autoFocus
              value={editing.code}
              onChange={(e) =>
                setEditing({ ...editing, code: e.currentTarget.value })
              }
            />
            <Group justify="flex-end">
              <Button variant="default" onClick={() => setEditing(null)}>
                Cancel
              </Button>
              <Button
                loading={saveCoderCode.isPending || saveAggCode.isPending}
                onClick={() => {
                  if (editing.kind === "coder")
                    saveCoderCode.mutate({
                      run_id: editing.run_id,
                      position: editing.position,
                      code: editing.code,
                    });
                  else saveAggCode.mutate({ id: editing.id, code: editing.code });
                }}
              >
                Save
              </Button>
            </Group>
          </Stack>
        )}
      </Modal>

      <Breadcrumbs>
        <Anchor component={Link} to="/segments">
          Segments
        </Anchor>
        <Text>{data.segment_id}</Text>
      </Breadcrumbs>

      <Group justify="space-between" align="end">
        <Stack gap={2}>
          <Title order={2}>{data.segment_id}</Title>
          <Group gap="xs">
            <StatusBadge status={data.status} />
            {data.batch != null && (
              <Badge variant="default">batch {data.batch}</Badge>
            )}
          </Group>
        </Stack>
      </Group>

      <Card padding="md">
        <Title order={5} mb="xs" c="dimmed">
          Segment text
        </Title>
        <Text
          style={{ whiteSpace: "pre-wrap", fontFamily: "Georgia, serif" }}
          size="sm"
        >
          {data.text}
        </Text>
      </Card>

      <Card padding="md">
        <Title order={4} mb="sm">
          Coder runs ({data.coder_runs.length})
        </Title>
        {data.coder_runs.length === 0 && (
          <Text c="dimmed" size="sm">
            No coders have processed this segment yet.
          </Text>
        )}
        <Stack gap="md">
          {data.coder_runs.map((run) => (
            <Card key={run.id} padding="sm" withBorder shadow="none">
              <Group justify="space-between" mb="xs">
                <Group>
                  <Text fw={600}>{run.coder_id}</Text>
                  <StatusBadge status={run.status} />
                  <Text size="xs" c="dimmed">
                    codebook v{run.codebook_version}
                  </Text>
                  {run.finished_at && (
                    <Text size="xs" c="dimmed">
                      {run.finished_at}
                    </Text>
                  )}
                </Group>
                <Tooltip label="Delete run (will be re-coded; downstream aggregation also reset)">
                  <ActionIcon
                    variant="subtle"
                    color="red"
                    onClick={() =>
                      confirm.ask({
                        title: `Delete run by ${run.coder_id}?`,
                        body: `Deletes this coder run, its codes, and any aggregation/reviews for the segment. The pipeline will recreate them on the next 'code'/'aggregate' run.`,
                        onConfirm: () => delCoderRun.mutateAsync(run.id),
                      })
                    }
                  >
                    <IconTrash size={16} />
                  </ActionIcon>
                </Tooltip>
              </Group>
              {run.error && (
                <Alert
                  variant="light"
                  color="red"
                  icon={<IconAlertTriangle size={16} />}
                  mb="xs"
                >
                  <Code>{run.error}</Code>
                </Alert>
              )}
              {run.codes.length === 0 ? (
                <Text size="sm" c="dimmed">
                  No codes produced (out of scope, or still running).
                </Text>
              ) : (
                <Table verticalSpacing={4}>
                  <Table.Thead>
                    <Table.Tr>
                      <Table.Th w={60}>#</Table.Th>
                      <Table.Th>Code</Table.Th>
                      <Table.Th>Rationale</Table.Th>
                      <Table.Th w={70}>New?</Table.Th>
                      <Table.Th w={40}></Table.Th>
                    </Table.Tr>
                  </Table.Thead>
                  <Table.Tbody>
                    {run.codes.map((c) => (
                      <Table.Tr key={c.position}>
                        <Table.Td>{c.position + 1}</Table.Td>
                        <Table.Td>
                          <Code>{c.code}</Code>
                        </Table.Td>
                        <Table.Td>
                          <Text size="sm" c="dimmed">
                            {c.rationale}
                          </Text>
                        </Table.Td>
                        <Table.Td>
                          {c.is_new === 1 ? (
                            <Badge size="xs" color="teal" variant="light">
                              new
                            </Badge>
                          ) : c.is_new === 0 ? (
                            <Badge size="xs" variant="default">
                              existing
                            </Badge>
                          ) : null}
                        </Table.Td>
                        <Table.Td>
                          <Tooltip label="Edit code text">
                            <ActionIcon
                              size="sm"
                              variant="subtle"
                              onClick={() =>
                                setEditing({
                                  kind: "coder",
                                  run_id: run.id,
                                  position: c.position,
                                  code: c.code,
                                })
                              }
                            >
                              <IconEdit size={14} />
                            </ActionIcon>
                          </Tooltip>
                        </Table.Td>
                      </Table.Tr>
                    ))}
                  </Table.Tbody>
                </Table>
              )}
            </Card>
          ))}
        </Stack>
      </Card>

      <Card padding="md">
        <Group justify="space-between" mb="sm">
          <Title order={4}>Aggregation</Title>
          {data.aggregation && (
            <Group gap="xs">
              <StatusBadge status={data.aggregation.status} />
              <Tooltip label="Delete aggregation (will be re-aggregated)">
                <ActionIcon
                  variant="subtle"
                  color="red"
                  onClick={() =>
                    confirm.ask({
                      title: "Delete aggregation?",
                      body: "Deletes the aggregation, its merged codes, and any review decisions on them. The aggregator will rebuild it.",
                      onConfirm: () =>
                        delAgg.mutateAsync(data.aggregation!.id),
                    })
                  }
                >
                  <IconTrash size={16} />
                </ActionIcon>
              </Tooltip>
            </Group>
          )}
        </Group>
        {!data.aggregation ? (
          <Text c="dimmed" size="sm">
            Not aggregated yet.
          </Text>
        ) : data.aggregation.error ? (
          <Alert color="red" variant="light">
            <Code>{data.aggregation.error}</Code>
          </Alert>
        ) : data.aggregated_codes.length === 0 ? (
          <Text c="dimmed" size="sm">
            No aggregated codes (nothing to review).
          </Text>
        ) : (
          <Table verticalSpacing="xs">
            <Table.Thead>
              <Table.Tr>
                <Table.Th>Merged code</Table.Th>
                <Table.Th>Source coders</Table.Th>
                <Table.Th>Review</Table.Th>
                <Table.Th w={80}></Table.Th>
              </Table.Tr>
            </Table.Thead>
            <Table.Tbody>
              {data.aggregated_codes.map((ac) => (
                <Table.Tr key={ac.id}>
                  <Table.Td>
                    <Code>{ac.code}</Code>
                  </Table.Td>
                  <Table.Td>
                    <Group gap={4}>
                      {ac.source_coders.map((c) => (
                        <Badge key={c} variant="light" size="xs">
                          {c}
                        </Badge>
                      ))}
                    </Group>
                  </Table.Td>
                  <Table.Td>
                    {ac.review ? (
                      <Group gap="xs">
                        <Badge
                          color={
                            ac.review.applied
                              ? "teal"
                              : ac.review.decision === "skip"
                                ? "gray"
                                : "yellow"
                          }
                          variant="light"
                        >
                          {ac.review.decision}
                        </Badge>
                        {ac.review.target_code && (
                          <Text size="xs">→ {ac.review.target_code}</Text>
                        )}
                        {ac.review.resulting_version && (
                          <Anchor
                            component={Link}
                            to={`/codebook/${ac.review.resulting_version}`}
                            size="xs"
                          >
                            v{ac.review.resulting_version}{" "}
                            <IconExternalLink
                              size={12}
                              style={{ verticalAlign: "middle" }}
                            />
                          </Anchor>
                        )}
                      </Group>
                    ) : (
                      <Text size="xs" c="dimmed">
                        pending
                      </Text>
                    )}
                  </Table.Td>
                  <Table.Td>
                    <Group gap={2}>
                      <Tooltip label="Edit merged code">
                        <ActionIcon
                          size="sm"
                          variant="subtle"
                          onClick={() =>
                            setEditing({
                              kind: "agg",
                              id: ac.id,
                              code: ac.code,
                            })
                          }
                        >
                          <IconEdit size={14} />
                        </ActionIcon>
                      </Tooltip>
                      <Tooltip label="Delete this merged code">
                        <ActionIcon
                          size="sm"
                          variant="subtle"
                          color="red"
                          onClick={() =>
                            confirm.ask({
                              title: "Delete merged code?",
                              body: "Removes the aggregated code and any review decision for it.",
                              onConfirm: () => delAggCode.mutateAsync(ac.id),
                            })
                          }
                        >
                          <IconTrash size={14} />
                        </ActionIcon>
                      </Tooltip>
                    </Group>
                  </Table.Td>
                </Table.Tr>
              ))}
            </Table.Tbody>
          </Table>
        )}
      </Card>
    </Stack>
  );
}
