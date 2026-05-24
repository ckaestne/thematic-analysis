import {
  Alert,
  Anchor,
  Badge,
  Card,
  Code,
  Grid,
  Group,
  NumberInput,
  SimpleGrid,
  Stack,
  Switch,
  Table,
  Text,
  Title,
  Tooltip,
} from "@mantine/core";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  IconAlertCircle,
  IconBook2,
  IconClipboardCheck,
  IconDatabase,
  IconStack2,
  IconUsers,
} from "@tabler/icons-react";
import { useState } from "react";
import { notifications } from "@mantine/notifications";
import { Link } from "react-router-dom";
import { api } from "../api";
import { ErrorAlert } from "../components/ErrorAlert";
import { StatCard } from "../components/StatCard";
import { StatusBar } from "../components/StatusBar";

function bytes(n: number) {
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  return `${(n / 1024 / 1024).toFixed(1)} MB`;
}

function BackgroundRunnerCard() {
  const qc = useQueryClient();
  const { data, error } = useQuery({
    queryKey: ["background-runner"],
    queryFn: api.backgroundRunner,
    refetchInterval: 2000,
  });
  const [workers, setWorkers] = useState<number>(1);

  const start = useMutation({
    mutationFn: () => api.startBackgroundRunner({ workers }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["background-runner"] });
      qc.invalidateQueries({ queryKey: ["status"] });
    },
    onError: (e) =>
      notifications.show({
        message: e instanceof Error ? e.message : String(e),
        color: "red",
      }),
  });
  const stop = useMutation({
    mutationFn: () => api.stopBackgroundRunner(),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["background-runner"] });
      qc.invalidateQueries({ queryKey: ["status"] });
    },
    onError: (e) =>
      notifications.show({
        message: e instanceof Error ? e.message : String(e),
        color: "red",
      }),
  });

  if (error) return <ErrorAlert error={error} />;
  if (!data) return null;

  const llmOk = data.llm_configured;
  const running = data.running;
  const busy = start.isPending || stop.isPending;

  return (
    <Card padding="md">
      <Stack gap="sm">
        <Group justify="space-between" align="center">
          <Title order={4}>Background runner</Title>
          <Group gap="sm">
            <NumberInput
              size="xs"
              w={110}
              min={1}
              max={32}
              value={running ? data.workers : workers}
              onChange={(v) => setWorkers(Number(v) || 1)}
              disabled={running || !llmOk}
              label="Workers"
            />
            <Switch
              checked={running}
              disabled={!llmOk || busy}
              onChange={(e) => {
                if (e.currentTarget.checked) start.mutate();
                else stop.mutate();
              }}
              label={running ? "On" : "Off"}
            />
          </Group>
        </Group>

        <Text size="sm" c="dimmed">
          When on, drains review tasks first, then aggregations, then
          coding tasks. Coding runs with the configured number of
          concurrent workers. Never updates the codebook itself —
          that's still a separate action.
        </Text>

        {!llmOk && (
          <Alert
            icon={<IconAlertCircle size={16} />}
            color="yellow"
            variant="light"
          >
            <Text size="sm">
              LLM is not configured —{" "}
              {data.llm_unavailable_reason ??
                "set LLM_API_KEY (and optionally LLM_MODEL) before starting the runner."}
            </Text>
          </Alert>
        )}

        <Group gap="md">
          <Badge color={running ? "teal" : "gray"} variant="light">
            {running ? "running" : "stopped"}
          </Badge>
          <Text size="sm">
            reviewed: <b>{data.counters.reviewed}</b>
          </Text>
          <Text size="sm">
            aggregated: <b>{data.counters.aggregated}</b>
          </Text>
          <Text size="sm">
            coded: <b>{data.counters.coded}</b>
          </Text>
          <Text size="sm" c={data.counters.failed > 0 ? "red" : undefined}>
            failed: <b>{data.counters.failed}</b>
          </Text>
        </Group>

        {data.last_event && (
          <Text size="xs" c="dimmed">
            last: {data.last_event.kind} ({data.last_event.ok ? "ok" : "failed"})
            {typeof data.last_event.segment_id === "number"
              ? ` · segment ${data.last_event.segment_id}`
              : ""}
            {typeof data.last_event.code === "string"
              ? ` · ${data.last_event.code}`
              : ""}
          </Text>
        )}

        {data.last_error && (
          <Text size="xs" c="red">
            last error: {data.last_error}
          </Text>
        )}
      </Stack>
    </Card>
  );
}

export function Overview() {
  const { data, error, isLoading } = useQuery({
    queryKey: ["status"],
    queryFn: api.status,
  });

  if (error) return <ErrorAlert error={error} />;
  if (isLoading || !data) return <Text c="dimmed">Loading…</Text>;

  const { stage1, per_coder } = data;

  return (
    <Stack gap="md">
      <Group justify="space-between" align="end">
        <div>
          <Title order={2}>Overview</Title>
          <Text c="dimmed" size="sm">
            <Code>{data.db_path}</Code> · {bytes(data.db_size_bytes)} ·{" "}
            {data.research_context_set ? (
              <Anchor component={Link} to="/research-context">
                research context set
              </Anchor>
            ) : (
              <Anchor component={Link} to="/research-context" c="orange">
                research context not set
              </Anchor>
            )}
          </Text>
        </div>
      </Group>

      <SimpleGrid cols={{ base: 2, sm: 3, md: 5 }} spacing="md">
        <StatCard
          label="Segments"
          value={stage1.segments_total}
          icon={<IconDatabase size={28} />}
        />
        <StatCard
          label="Coders"
          value={stage1.coders_total}
          icon={<IconUsers size={28} />}
        />
        <StatCard
          label="Aggregations"
          value={stage1.aggregations_total}
          icon={<IconStack2 size={28} />}
        />
        <StatCard
          label="Reviews"
          value={`${stage1.review_decisions_applied}/${stage1.review_decisions_total}`}
          hint="applied / total"
          icon={<IconClipboardCheck size={28} />}
        />
        <StatCard
          label="Codebook"
          value={`v${stage1.codebook_version}`}
          hint={`${stage1.codebook_codes} codes`}
          icon={<IconBook2 size={28} />}
        />
      </SimpleGrid>

      <Grid>
        <Grid.Col span={{ base: 12, md: 12 }}>
          <Card padding="md">
            <Stack gap="sm">
              <Title order={4}>Stage 1 progress</Title>
              <Text size="sm" c="dimmed">
                Segments
              </Text>
              <StatusBar counts={stage1.segments_by_status} />
              <Text size="sm" c="dimmed" mt="xs">
                Coder runs
              </Text>
              <StatusBar counts={stage1.coder_runs_by_status} />
              <Text size="sm" c="dimmed" mt="xs">
                Aggregations
              </Text>
              <StatusBar counts={stage1.aggregations_by_status} />
            </Stack>
          </Card>
        </Grid.Col>
      </Grid>

      <BackgroundRunnerCard />

      <Card padding="md">
        <Title order={4} mb="sm">
          Per-coder progress (Stage 1)
        </Title>
        {per_coder.length === 0 ? (
          <Text c="dimmed" size="sm">
            No coders registered. Use{" "}
            <Code>ta-stage1 add-coder ID IDENTITY</Code> to add one, or use the{" "}
            <Anchor component={Link} to="/coders">
              Coders
            </Anchor>{" "}
            page.
          </Text>
        ) : (
          <Table verticalSpacing="xs" striped>
            <Table.Thead>
              <Table.Tr>
                <Table.Th>Coder</Table.Th>
                <Table.Th>Identity</Table.Th>
                <Table.Th>Progress</Table.Th>
                <Table.Th ta="right">Done</Table.Th>
                <Table.Th ta="right">Running</Table.Th>
                <Table.Th ta="right">Failed</Table.Th>
              </Table.Tr>
            </Table.Thead>
            <Table.Tbody>
              {per_coder.map((c) => (
                <Table.Tr key={c.coder_id}>
                  <Table.Td>{c.coder_id}</Table.Td>
                  <Table.Td>
                    <Tooltip label={c.identity} multiline w={300}>
                      <Text size="sm" lineClamp={1}>
                        {c.identity}
                      </Text>
                    </Tooltip>
                  </Table.Td>
                  <Table.Td style={{ minWidth: 200 }}>
                    <StatusBar
                      counts={{
                        done: c.runs_done,
                        running: c.runs_running,
                        failed: c.runs_failed,
                        pending: Math.max(
                          0,
                          c.segments_total -
                            c.runs_done -
                            c.runs_running -
                            c.runs_failed,
                        ),
                      }}
                      total={c.segments_total}
                    />
                  </Table.Td>
                  <Table.Td ta="right">{c.runs_done}</Table.Td>
                  <Table.Td ta="right">{c.runs_running}</Table.Td>
                  <Table.Td ta="right">
                    {c.runs_failed > 0 ? (
                      <Text c="red" fw={600}>
                        {c.runs_failed}
                      </Text>
                    ) : (
                      c.runs_failed
                    )}
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
