import {
  Anchor,
  Breadcrumbs,
  Card,
  Code as CodeText,
  Group,
  Stack,
  Text,
  Title,
} from "@mantine/core";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link, useParams } from "react-router-dom";
import { api } from "../api";
import { ErrorAlert } from "../components/ErrorAlert";
import { ThemeCard } from "../components/ThemeCard";
import { useConfirmDelete } from "../components/ConfirmDelete";

export function ThemeCodingJobDetailPage() {
  const { id } = useParams<{ id: string }>();
  const jobId = Number(id);
  const qc = useQueryClient();
  const { ask, modal } = useConfirmDelete();

  const job = useQuery({
    queryKey: ["theme-coding-job", jobId],
    queryFn: () => api.themeCodingJob(jobId),
    enabled: Number.isFinite(jobId),
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
            <Title order={3}>Job #{d.id}</Title>
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

      <Group justify="space-between" align="flex-end">
        <Title order={3}>Themes ({active.length})</Title>
      </Group>

      {active.length === 0 ? (
        <Card padding="md" withBorder>
          <Text c="dimmed" ta="center" py="md">
            No active themes from this job.
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
                      Mark <b>{t.title}</b> as deleted? It will move to the
                      end of this page and disappear from the top-level
                      Themes listing.
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
      {modal}
    </Stack>
  );
}
