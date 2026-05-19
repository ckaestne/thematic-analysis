import {
  ActionIcon,
  Alert,
  Anchor,
  Badge,
  Button,
  Card,
  Code,
  Divider,
  Group,
  Modal,
  Stack,
  Table,
  Text,
  TextInput,
  Title,
  Tooltip,
} from "@mantine/core";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import {
  IconAlertTriangle,
  IconEdit,
  IconExternalLink,
  IconPlayerPlay,
  IconTrash,
} from "@tabler/icons-react";
import { useState } from "react";
import { Link } from "react-router-dom";
import { notifications } from "@mantine/notifications";
import { api, type AggregatedCode, type CoderRun, type SegmentDetail } from "../api";
import { StatusBadge } from "./StatusBadge";
import { useConfirmDelete } from "./ConfirmDelete";

export type CodingSegment = {
  segment_id: string;
  title: string | null;
  status: string;
  text: string;
  batch?: number | null;
  coder_runs: CoderRun[];
  aggregation: SegmentDetail["aggregation"];
  aggregated_codes: AggregatedCode[];
};

type Props = {
  segment: CodingSegment;
  /** React-Query keys to invalidate after a successful mutation. */
  invalidateKeys: ReadonlyArray<ReadonlyArray<unknown>>;
  /** Optional `[i+1/total]` index marker shown next to the segment id. */
  index?: { i: number; total: number };
  /** When true, show the raw segment text in a sub-card. */
  showText?: boolean;
};

export function SegmentCodingCard({
  segment,
  invalidateKeys,
  index,
  showText = true,
}: Props) {
  const qc = useQueryClient();
  const confirm = useConfirmDelete();
  const [editing, setEditing] = useState<
    | { kind: "coder"; run_id: number; position: number; code: string }
    | { kind: "agg"; id: number; code: string }
    | null
  >(null);

  const invalidate = () => {
    for (const key of invalidateKeys) {
      qc.invalidateQueries({ queryKey: [...key] });
    }
  };

  const enqueue = useMutation({
    mutationFn: () => api.enqueueSegment(segment.segment_id),
    onSuccess: (res) => {
      notifications.show({
        message:
          res.enqueued > 0
            ? `Enqueued ${res.enqueued} (segment, coder) pair${res.enqueued === 1 ? "" : "s"} for coding`
            : "Already enqueued at the current codebook + research-context revisions",
        color: "teal",
      });
      invalidate();
      qc.invalidateQueries({ queryKey: ["coding-queue"] });
    },
    onError: (e) =>
      notifications.show({
        message: e instanceof Error ? e.message : String(e),
        color: "red",
      }),
  });

  const saveCoderCode = useMutation({
    mutationFn: (v: { run_id: number; position: number; code: string }) =>
      api.editCoderCode(v.run_id, v.position, v.code),
    onSuccess: () => {
      notifications.show({ message: "Code updated", color: "teal" });
      setEditing(null);
      invalidate();
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
      invalidate();
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
      invalidate();
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
      invalidate();
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
      invalidate();
    },
    onError: (e) =>
      notifications.show({
        message: e instanceof Error ? e.message : String(e),
        color: "red",
      }),
  });

  const words = segment.text.split(/\s+/).filter(Boolean).length;

  return (
    <Card padding="md" withBorder>
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

      <Group justify="space-between" align="flex-start" wrap="nowrap">
        <Stack gap={2} style={{ flex: 1, minWidth: 0 }}>
          <Group gap="xs" wrap="nowrap">
            {index && (
              <Text size="xs" c="dimmed" ff="monospace">
                [{index.i + 1}/{index.total}]
              </Text>
            )}
            <Anchor
              component={Link}
              to={`/segments/${segment.segment_id}`}
              size="sm"
              ff="monospace"
            >
              {segment.segment_id}
            </Anchor>
            <Text size="xs" c="dimmed">
              {words} words · {segment.text.length} chars
            </Text>
            {segment.batch != null && (
              <Badge variant="default" size="xs">
                batch {segment.batch}
              </Badge>
            )}
          </Group>
          {segment.title && (
            <Text size="md" fw={600}>
              {segment.title}
            </Text>
          )}
        </Stack>
        <Group gap="xs" wrap="nowrap">
          <Tooltip label="Enqueue this segment for coding by all registered coders (at the latest codebook + research-context revisions)">
            <ActionIcon
              variant="subtle"
              color="blue"
              loading={enqueue.isPending}
              onClick={() => enqueue.mutate()}
            >
              <IconPlayerPlay size={16} />
            </ActionIcon>
          </Tooltip>
          <StatusBadge status={segment.status} />
        </Group>
      </Group>

      {showText && (
        <>
          <Divider my="xs" />
          <Text
            size="sm"
            style={{
              whiteSpace: "pre-wrap",
              fontFamily: "Georgia, serif",
            }}
          >
            {segment.text}
          </Text>
        </>
      )}

      <Divider my="sm" />

      <Title order={6} c="dimmed" mb="xs">
        Coder runs ({segment.coder_runs.length})
      </Title>
      {segment.coder_runs.length === 0 ? (
        <Text c="dimmed" size="sm">
          No coders have processed this segment yet.
        </Text>
      ) : (
        <Stack gap="sm">
          {segment.coder_runs.map((run) => (
            <Card key={run.id} padding="sm" withBorder shadow="none">
              <Group justify="space-between" mb="xs">
                <Group>
                  <Text fw={600}>{run.coder_id}</Text>
                  <StatusBadge status={run.status} />
                  <Text size="xs" c="dimmed">
                    codebook v{run.codebook_version} · research context v
                    {run.research_context_version}
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
                run.status === "done" ? (
                  <Text size="sm" c="dimmed">
                    Coder produced no codes for this segment (e.g. out of scope).
                  </Text>
                ) : run.status === "running" ? (
                  <Text size="sm" c="dimmed">
                    Coding in progress — no codes recorded yet.
                  </Text>
                ) : run.status === "failed" ? null : (
                  <Text size="sm" c="dimmed">
                    No codes recorded.
                  </Text>
                )
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
      )}

      <Divider my="sm" />

      <Group justify="space-between" mb="xs">
        <Title order={6} c="dimmed">
          Aggregation
        </Title>
        {segment.aggregation && (
          <Group gap="xs">
            <StatusBadge status={segment.aggregation.status} />
            <Tooltip label="Delete aggregation (will be re-aggregated)">
              <ActionIcon
                variant="subtle"
                color="red"
                onClick={() =>
                  confirm.ask({
                    title: "Delete aggregation?",
                    body: "Deletes the aggregation, its merged codes, and any review decisions on them. The aggregator will rebuild it.",
                    onConfirm: () =>
                      delAgg.mutateAsync(segment.aggregation!.id),
                  })
                }
              >
                <IconTrash size={16} />
              </ActionIcon>
            </Tooltip>
          </Group>
        )}
      </Group>
      {!segment.aggregation ? (
        <Text c="dimmed" size="sm">
          Not aggregated yet.
        </Text>
      ) : segment.aggregation.error ? (
        <Alert color="red" variant="light">
          <Code>{segment.aggregation.error}</Code>
        </Alert>
      ) : segment.aggregated_codes.length === 0 ? (
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
            {segment.aggregated_codes.map((ac) => (
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
  );
}
