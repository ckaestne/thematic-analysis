import {
  Alert,
  Badge,
  Card,
  Group,
  NumberInput,
  Stack,
  Switch,
  Text,
  Title,
} from "@mantine/core";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { IconAlertCircle } from "@tabler/icons-react";
import { useState } from "react";
import { notifications } from "@mantine/notifications";
import { api } from "../api";
import { ErrorAlert } from "./ErrorAlert";

export function BackgroundRunnerCard() {
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
