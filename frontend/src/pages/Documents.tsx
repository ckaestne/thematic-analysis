import {
  ActionIcon,
  Anchor,
  Button,
  Card,
  Group,
  Progress,
  Stack,
  Table,
  Text,
  Title,
  Tooltip,
} from "@mantine/core";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { IconPlayerPlay, IconTrash, IconUpload } from "@tabler/icons-react";
import { useRef } from "react";
import { Link } from "react-router-dom";
import { notifications } from "@mantine/notifications";
import { api } from "../api";
import { ErrorAlert } from "../components/ErrorAlert";
import { useConfirmDelete } from "../components/ConfirmDelete";

const STATUS_COLORS: Record<string, string> = {
  pending: "gray",
  coding: "blue",
  running: "blue",
  aggregating: "violet",
  reviewing: "yellow",
  done: "teal",
  failed: "red",
};

function ProgressMini({
  counts,
  total,
}: {
  counts: Record<string, number>;
  total: number;
}) {
  if (total === 0) {
    return (
      <Text size="xs" c="dimmed">
        —
      </Text>
    );
  }
  const entries = Object.entries(counts).filter(([, n]) => n > 0);
  return (
    <Progress.Root size={8} radius="sm">
      {entries.map(([k, n]) => (
        <Tooltip key={k} label={`${k}: ${n}`} withinPortal>
          <Progress.Section
            value={(n / total) * 100}
            color={STATUS_COLORS[k] ?? "gray"}
          />
        </Tooltip>
      ))}
    </Progress.Root>
  );
}

export function Documents() {
  const qc = useQueryClient();
  const { data, error, isLoading } = useQuery({
    queryKey: ["documents"],
    queryFn: api.documents,
  });
  const confirm = useConfirmDelete();
  const fileInputRef = useRef<HTMLInputElement>(null);

  const upload = useMutation({
    mutationFn: (file: File) => api.uploadDocument(file),
    onSuccess: (res) => {
      notifications.show({
        message: `Uploaded "${res.filename}" — ${res.segments_inserted} segment${res.segments_inserted === 1 ? "" : "s"} added`,
        color: "teal",
      });
      qc.invalidateQueries({ queryKey: ["documents"] });
      qc.invalidateQueries({ queryKey: ["status"] });
    },
    onError: (e) =>
      notifications.show({
        message: e instanceof Error ? e.message : String(e),
        color: "red",
      }),
  });

  const del = useMutation({
    mutationFn: (id: number) => api.deleteDocument(id),
    onSuccess: () => {
      notifications.show({ message: "Document deleted", color: "teal" });
      qc.invalidateQueries({ queryKey: ["documents"] });
      qc.invalidateQueries({ queryKey: ["status"] });
    },
    onError: (e) =>
      notifications.show({
        message: e instanceof Error ? e.message : String(e),
        color: "red",
      }),
  });

  const enqueue = useMutation({
    mutationFn: (id: number) => api.enqueueDocument(id),
    onSuccess: (res) => {
      notifications.show({
        message:
          res.enqueued > 0
            ? `Enqueued ${res.enqueued} (segment, coder) pair${res.enqueued === 1 ? "" : "s"} for coding`
            : "Already enqueued at the current codebook + research-context revisions",
        color: "teal",
      });
      qc.invalidateQueries({ queryKey: ["documents"] });
      qc.invalidateQueries({ queryKey: ["status"] });
      qc.invalidateQueries({ queryKey: ["coding-queue"] });
    },
    onError: (e) =>
      notifications.show({
        message: e instanceof Error ? e.message : String(e),
        color: "red",
      }),
  });

  if (error) return <ErrorAlert error={error} />;

  const coderIds = data?.coder_ids ?? [];

  return (
    <Stack gap="md">
      {confirm.modal}
      <input
        ref={fileInputRef}
        type="file"
        accept=".txt,.md,.text"
        style={{ display: "none" }}
        onChange={(e) => {
          const file = e.target.files?.[0];
          if (file) upload.mutate(file);
          e.target.value = "";
        }}
      />
      <Group justify="space-between">
        <Title order={2}>Documents</Title>
        <Button
          leftSection={<IconUpload size={16} />}
          variant="light"
          loading={upload.isPending}
          onClick={() => fileInputRef.current?.click()}
        >
          Upload document
        </Button>
      </Group>
      <Card padding={0}>
        <Table verticalSpacing="sm" highlightOnHover>
          <Table.Thead>
            <Table.Tr>
              <Table.Th>ID</Table.Th>
              <Table.Th>Filename</Table.Th>
              <Table.Th ta="right">Segments</Table.Th>
              {coderIds.map((cid) => (
                <Table.Th key={cid} style={{ minWidth: 140 }}>
                  {cid}
                </Table.Th>
              ))}
              <Table.Th style={{ minWidth: 140 }}>Aggregations</Table.Th>
              <Table.Th>Size</Table.Th>
              <Table.Th>Added</Table.Th>
              <Table.Th w={80}></Table.Th>
            </Table.Tr>
          </Table.Thead>
          <Table.Tbody>
            {isLoading && (
              <Table.Tr>
                <Table.Td colSpan={6 + coderIds.length}>Loading…</Table.Td>
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
                <Table.Td ta="right">{d.segments_total}</Table.Td>
                {coderIds.map((cid) => {
                  const p = d.per_coder.find((x) => x.coder_id === cid);
                  const done = p?.runs_done ?? 0;
                  const running = p?.runs_running ?? 0;
                  const failed = p?.runs_failed ?? 0;
                  const pending = Math.max(
                    0,
                    d.segments_total - done - running - failed,
                  );
                  return (
                    <Table.Td key={cid}>
                      <Stack gap={2}>
                        <Text size="xs" c="dimmed">
                          {done}/{d.segments_total} coded
                          {failed > 0 && (
                            <Text span c="red" fw={600} size="xs">
                              {" "}
                              · {failed} failed
                            </Text>
                          )}
                        </Text>
                        <ProgressMini
                          counts={{ done, running, failed, pending }}
                          total={d.segments_total}
                        />
                      </Stack>
                    </Table.Td>
                  );
                })}
                <Table.Td>
                  {d.segments_total > 0 ? (
                    (() => {
                      const counts = { ...d.aggregations_by_status };
                      const counted = Object.values(counts).reduce(
                        (a, b) => a + b,
                        0,
                      );
                      const unstarted = Math.max(
                        0,
                        d.segments_total - counted,
                      );
                      if (unstarted > 0) {
                        counts.pending = (counts.pending ?? 0) + unstarted;
                      }
                      return (
                        <Stack gap={2}>
                          <Text size="xs" c="dimmed">
                            {d.aggregations_by_status.done ?? 0}/
                            {d.segments_total}
                          </Text>
                          <ProgressMini
                            counts={counts}
                            total={d.segments_total}
                          />
                        </Stack>
                      );
                    })()
                  ) : (
                    <Text size="xs" c="dimmed">
                      —
                    </Text>
                  )}
                </Table.Td>
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
                <Table.Td>
                  <Tooltip label="Enqueue every segment in this document for coding by all registered coders (at the latest codebook + research-context revisions)">
                    <ActionIcon
                      variant="subtle"
                      color="blue"
                      loading={
                        enqueue.isPending &&
                        enqueue.variables === d.document_id
                      }
                      onClick={() => enqueue.mutate(d.document_id)}
                    >
                      <IconPlayerPlay size={16} />
                    </ActionIcon>
                  </Tooltip>
                  <Tooltip label="Delete document (and all its segments + derived data)">
                    <ActionIcon
                      variant="subtle"
                      color="red"
                      onClick={() =>
                        confirm.ask({
                          title: `Delete document ${d.filename}?`,
                          body: `Permanently deletes the document, its ${d.segments_total} segment${
                            d.segments_total === 1 ? "" : "s"
                          }, and all coder runs, aggregations, and review decisions derived from them. The codebook is not affected.`,
                          onConfirm: () => del.mutateAsync(d.document_id),
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
                <Table.Td colSpan={6 + coderIds.length}>
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
