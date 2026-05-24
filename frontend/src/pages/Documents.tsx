import {
  ActionIcon,
  Anchor,
  Button,
  Card,
  Center,
  Group,
  Progress,
  Stack,
  Table,
  Text,
  Title,
  Tooltip,
  UnstyledButton,
} from "@mantine/core";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  IconTrash,
  IconDice,
  IconChevronUp,
  IconChevronDown,
  IconSelector,
  IconUpload,
} from "@tabler/icons-react";
import { Link } from "react-router-dom";
import { useMemo, useRef, useState } from "react";
import { notifications } from "@mantine/notifications";
import { api, type Document } from "../api";
import { ErrorAlert } from "../components/ErrorAlert";
import { useConfirmDelete } from "../components/ConfirmDelete";
import { EnqueueButton } from "../components/EnqueueButton";

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

type SortKey =
  | "document_id"
  | "filename"
  | "segments_total"
  | "size_bytes"
  | "created_at"
  | "aggregations_done"
  | `coder:${string}`;

type SortState = { key: SortKey; dir: "asc" | "desc" } | null;

function sortValue(d: Document, key: SortKey): number | string {
  if (key === "aggregations_done") {
    const done = d.aggregations_by_status.done ?? 0;
    return d.segments_total > 0 ? done / d.segments_total : -1;
  }
  if (key.startsWith("coder:")) {
    const cid = key.slice("coder:".length);
    const p = d.per_coder.find((x) => x.coder_id === cid);
    const done = p?.runs_done ?? 0;
    return d.segments_total > 0 ? done / d.segments_total : -1;
  }
  return d[key as keyof Document] as number | string;
}

function SortHeader({
  label,
  sortKey,
  state,
  onToggle,
  align = "left",
  style,
}: {
  label: React.ReactNode;
  sortKey: SortKey;
  state: SortState;
  onToggle: (key: SortKey) => void;
  align?: "left" | "right";
  style?: React.CSSProperties;
}) {
  const active = state?.key === sortKey;
  const Icon = active
    ? state!.dir === "asc"
      ? IconChevronUp
      : IconChevronDown
    : IconSelector;
  return (
    <Table.Th style={style}>
      <UnstyledButton
        onClick={() => onToggle(sortKey)}
        style={{ width: "100%" }}
      >
        <Group
          gap={4}
          wrap="nowrap"
          justify={align === "right" ? "flex-end" : "flex-start"}
        >
          <Text fw={600} size="sm">
            {label}
          </Text>
          <Center>
            <Icon size={14} opacity={active ? 1 : 0.4} />
          </Center>
        </Group>
      </UnstyledButton>
    </Table.Th>
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
  const [sort, setSort] = useState<SortState>({
    key: "document_id",
    dir: "asc",
  });

  const toggleSort = (key: SortKey) => {
    setSort((prev) => {
      if (!prev || prev.key !== key) return { key, dir: "asc" };
      if (prev.dir === "asc") return { key, dir: "desc" };
      return null;
    });
  };

  const sortedItems = useMemo(() => {
    const items = data?.items ?? [];
    if (!sort) return items;
    const dir = sort.dir === "asc" ? 1 : -1;
    return [...items].sort((a, b) => {
      const av = sortValue(a, sort.key);
      const bv = sortValue(b, sort.key);
      if (typeof av === "number" && typeof bv === "number") {
        return (av - bv) * dir;
      }
      return String(av).localeCompare(String(bv)) * dir;
    });
  }, [data?.items, sort]);

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

  const upload = useMutation({
    mutationFn: (file: File) => api.uploadDocument(file),
    onSuccess: (res) => {
      notifications.show({
        message: `Uploaded "${res.filename}" — ${res.segments_total} segment${res.segments_total === 1 ? "" : "s"} created`,
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

  const enqueueRandom = useMutation({
    mutationFn: () => api.enqueueRandomDocuments(10),
    onSuccess: (res) => {
      const nDocs = res.document_ids.length;
      notifications.show({
        message:
          nDocs > 0
            ? `Enqueued ${nDocs} document${nDocs === 1 ? "" : "s"} (${res.enqueued} segment/coder pair${res.enqueued === 1 ? "" : "s"})`
            : "No un-enqueued documents available",
        color: nDocs > 0 ? "teal" : "yellow",
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
      <Group justify="space-between">
        <Title order={2}>Documents</Title>
        <Group gap="xs">
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
          <Tooltip label="Upload a plain-text or Markdown file; it will be split into paragraph-based segments">
            <Button
              variant="light"
              leftSection={<IconUpload size={16} />}
              loading={upload.isPending}
              onClick={() => fileInputRef.current?.click()}
            >
              Upload document
            </Button>
          </Tooltip>
          <Tooltip label="Pick 10 random documents that have never been enqueued and queue every segment for all coders">
            <Button
              variant="light"
              leftSection={<IconDice size={16} />}
              loading={enqueueRandom.isPending}
              onClick={() => enqueueRandom.mutate()}
            >
              Enqueue 10 random
            </Button>
          </Tooltip>
        </Group>
      </Group>
      <Card padding={0}>
        <Table verticalSpacing="sm" highlightOnHover>
          <Table.Thead>
            <Table.Tr>
              <SortHeader
                label="ID"
                sortKey="document_id"
                state={sort}
                onToggle={toggleSort}
              />
              <SortHeader
                label="Filename"
                sortKey="filename"
                state={sort}
                onToggle={toggleSort}
              />
              <SortHeader
                label="Segments"
                sortKey="segments_total"
                state={sort}
                onToggle={toggleSort}
                align="right"
              />
              {coderIds.map((cid) => (
                <SortHeader
                  key={cid}
                  label={cid}
                  sortKey={`coder:${cid}`}
                  state={sort}
                  onToggle={toggleSort}
                  style={{ minWidth: 140 }}
                />
              ))}
              <SortHeader
                label="Aggregations"
                sortKey="aggregations_done"
                state={sort}
                onToggle={toggleSort}
                style={{ minWidth: 140 }}
              />
              <SortHeader
                label="Size"
                sortKey="size_bytes"
                state={sort}
                onToggle={toggleSort}
              />
              <SortHeader
                label="Added"
                sortKey="created_at"
                state={sort}
                onToggle={toggleSort}
              />
              <Table.Th w={80}></Table.Th>
            </Table.Tr>
          </Table.Thead>
          <Table.Tbody>
            {isLoading && (
              <Table.Tr>
                <Table.Td colSpan={6 + coderIds.length}>Loading…</Table.Td>
              </Table.Tr>
            )}
            {sortedItems.map((d) => (
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
                  <EnqueueButton
                    queueState={d.queue_state}
                    kind="document"
                    hoverLabel="Enqueue every segment in this document for coding by all registered coders (at the latest codebook + research-context revisions)"
                    isLoading={
                      enqueue.isPending &&
                      enqueue.variables === d.document_id
                    }
                    onEnqueue={() => enqueue.mutate(d.document_id)}
                  />
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
