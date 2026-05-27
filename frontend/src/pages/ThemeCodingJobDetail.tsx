import {
  Alert,
  Anchor,
  Badge,
  Breadcrumbs,
  Button,
  Card,
  Code as CodeText,
  Group,
  Stack,
  Text,
  Title,
} from "@mantine/core";
import {
  IconInfoCircle,
  IconLoader2,
  IconPlayerPlay,
} from "@tabler/icons-react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link, useParams } from "react-router-dom";
import { api, type ThemeCodingJobRunStatus } from "../api";
import { ErrorAlert } from "../components/ErrorAlert";
import { ThemeCard } from "../components/ThemeCard";
import { useConfirmDelete } from "../components/ConfirmDelete";

const STATUS_LABEL: Record<ThemeCodingJobRunStatus, string> = {
  not_run: "not run",
  running: "running…",
  no_themes: "ran — no themes produced",
  has_themes: "ran",
};

const STATUS_COLOR: Record<ThemeCodingJobRunStatus, string> = {
  not_run: "yellow",
  running: "blue",
  no_themes: "gray",
  has_themes: "green",
};

export function ThemeCodingJobDetailPage() {
  const { id } = useParams<{ id: string }>();
  const jobId = Number(id);
  const qc = useQueryClient();
  const { ask, modal } = useConfirmDelete();

  const job = useQuery({
    queryKey: ["theme-coding-job", jobId],
    queryFn: () => api.themeCodingJob(jobId),
    enabled: Number.isFinite(jobId),
    // While the worker is running we want the badge / themes list to
    // update without a manual refresh.
    refetchInterval: (q) =>
      q.state.data?.run_status === "running" ? 2000 : false,
  });

  const run = useMutation({
    mutationFn: () => api.runThemeCodingJob(jobId),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["theme-coding-job", jobId] });
      qc.invalidateQueries({ queryKey: ["theme-coding-jobs"] });
      qc.invalidateQueries({ queryKey: ["themes"] });
    },
  });

  const del = useMutation({
    mutationFn: (themeId: number) => api.deleteTheme(themeId),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["theme-coding-job", jobId] });
      qc.invalidateQueries({ queryKey: ["theme-coding-jobs"] });
      qc.invalidateQueries({ queryKey: ["themes"] });
    },
  });

  const restore = useMutation({
    mutationFn: (themeId: number) => api.restoreTheme(themeId),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["theme-coding-job", jobId] });
      qc.invalidateQueries({ queryKey: ["theme-coding-jobs"] });
      qc.invalidateQueries({ queryKey: ["themes"] });
    },
  });

  if (job.error) return <ErrorAlert error={job.error} />;
  if (!job.data) return <Text c="dimmed">Loading…</Text>;
  const d = job.data;

  const active = d.themes.filter((t) => !t.deleted);
  const deleted = d.themes.filter((t) => t.deleted);

  return (
    <Stack gap="md">
      <Breadcrumbs>
        <Anchor component={Link} to="/theme-coding-jobs">
          Theme coding jobs
        </Anchor>
        <Text>#{d.id}</Text>
      </Breadcrumbs>

      <Card padding="md" withBorder>
        <Stack gap="xs">
          <Group justify="space-between" align="flex-start">
            <Group gap="xs">
              <Title order={3}>Job #{d.id}</Title>
              <Badge color={STATUS_COLOR[d.run_status]} variant="light">
                {STATUS_LABEL[d.run_status]}
              </Badge>
            </Group>
            <Text size="xs" c="dimmed">
              {d.created_at?.slice(0, 19).replace("T", " ")}
            </Text>
          </Group>
          <Text size="sm">
            Codebook:{" "}
            <Anchor component={Link} to={`/codebook/${d.codebook_used_id}`}>
              v{d.codebook_used_id}
            </Anchor>
          </Text>
          <div>
            <Text size="xs" c="dimmed" mb={4}>
              Researcher prompt
            </Text>
            <CodeText
              block
              style={{
                whiteSpace: "pre-wrap",
                fontSize: 12,
                maxHeight: 240,
                overflow: "auto",
              }}
            >
              {d.prompt}
            </CodeText>
          </div>
        </Stack>
      </Card>

      {d.run_status === "not_run" && (
        <Card padding="md" withBorder>
          <Stack gap="sm">
            <Text size="sm">
              This job has not been run yet. Running it invokes the LLM
              theme coder against codebook v{d.codebook_used_id}; the
              call may take a minute.
            </Text>
            {run.error && <ErrorAlert error={run.error} />}
            <Group>
              <Button
                leftSection={<IconPlayerPlay size={16} />}
                loading={run.isPending}
                onClick={() => run.mutate()}
              >
                Run now
              </Button>
            </Group>
          </Stack>
        </Card>
      )}

      {d.run_status === "running" && (
        <Alert
          color="blue"
          icon={<IconLoader2 size={16} />}
          title="Theme coder running"
        >
          The LLM theme coder is processing this job against codebook v
          {d.codebook_used_id}. This page refreshes automatically; results
          will appear once it finishes.
        </Alert>
      )}

      {d.run_status === "no_themes" && (
        <Alert
          color="gray"
          icon={<IconInfoCircle size={16} />}
          title="No themes produced"
        >
          The theme coder ran against codebook v{d.codebook_used_id} but
          did not return any themes. The empty result is recorded so the
          job won't be picked up again by "run pending".
        </Alert>
      )}

      {d.run_status === "has_themes" && (
        <>
          <Group justify="space-between" align="flex-end">
            <Title order={3}>Themes ({active.length})</Title>
          </Group>

          {active.length === 0 ? (
            <Card padding="md" withBorder>
              <Text c="dimmed" ta="center" py="md">
                No active themes from this job (all have been deleted).
              </Text>
            </Card>
          ) : (
            <Stack gap="sm">
              {active.map((t) => (
                <ThemeCard
                  key={t.theme_id}
                  theme={t}
                  onDelete={() =>
                    ask({
                      title: "Delete theme?",
                      body: (
                        <>
                          Mark <b>{t.title}</b> as deleted? It will move to
                          the end of this page and disappear from the
                          top-level Themes listing.
                        </>
                      ),
                      onConfirm: () => del.mutateAsync(t.theme_id),
                    })
                  }
                />
              ))}
            </Stack>
          )}

          {deleted.length > 0 && (
            <>
              <Title order={4} c="dimmed" mt="md">
                Deleted ({deleted.length})
              </Title>
              <Stack gap="sm">
                {deleted.map((t) => (
                  <ThemeCard
                    key={t.theme_id}
                    theme={t}
                    onRestore={() => restore.mutate(t.theme_id)}
                  />
                ))}
              </Stack>
            </>
          )}
        </>
      )}
      {modal}
    </Stack>
  );
}
