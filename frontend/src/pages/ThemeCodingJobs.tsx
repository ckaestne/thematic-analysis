import {
  Anchor,
  Badge,
  Button,
  Card,
  Group,
  Stack,
  Table,
  Text,
  Title,
} from "@mantine/core";
import { IconPlayerPlay, IconPlus } from "@tabler/icons-react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { api, type ThemeCodingJobRunStatus } from "../api";
import { ErrorAlert } from "../components/ErrorAlert";

const STATUS_LABEL: Record<ThemeCodingJobRunStatus, string> = {
  not_run: "not run",
  running: "running…",
  no_themes: "no themes",
  has_themes: "ran",
};

const STATUS_COLOR: Record<ThemeCodingJobRunStatus, string> = {
  not_run: "yellow",
  running: "blue",
  no_themes: "gray",
  has_themes: "green",
};

export function ThemeCodingJobsPage() {
  const qc = useQueryClient();
  const jobs = useQuery({
    queryKey: ["theme-coding-jobs"],
    queryFn: api.themeCodingJobs,
    // Refresh while any job is in-flight so its badge flips when done.
    refetchInterval: (q) =>
      q.state.data?.some((j) => j.run_status === "running") ? 2000 : false,
  });

  const runPending = useMutation({
    mutationFn: api.runPendingThemeCodingJobs,
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["theme-coding-jobs"] });
      qc.invalidateQueries({ queryKey: ["themes"] });
    },
  });

  if (jobs.error) return <ErrorAlert error={jobs.error} />;

  const pendingCount =
    jobs.data?.filter((j) => j.run_status === "not_run").length ?? 0;

  return (
    <Stack gap="md">
      <Group justify="space-between" align="flex-end">
        <div>
          <Title order={2}>Theme coding jobs</Title>
          <Text c="dimmed" size="sm">
            Each job pins a codebook revision and a researcher prompt.
            Creating a job records its parameters; the LLM theme coder
            only runs when you ask it to. A job that ran but produced no
            themes is recorded so it won't be rerun.
          </Text>
        </div>
        <Group gap="xs">
          {pendingCount > 0 && (
            <Button
              variant="light"
              leftSection={<IconPlayerPlay size={16} />}
              loading={runPending.isPending}
              onClick={() => runPending.mutate()}
            >
              Run {pendingCount} pending
            </Button>
          )}
          <Button
            component={Link}
            to="/theme-coding-jobs/new"
            leftSection={<IconPlus size={16} />}
          >
            New job
          </Button>
        </Group>
      </Group>

      {runPending.error && <ErrorAlert error={runPending.error} />}

      {jobs.isLoading ? (
        <Text c="dimmed">Loading…</Text>
      ) : !jobs.data || jobs.data.length === 0 ? (
        <Card padding="md" withBorder>
          <Text c="dimmed" ta="center" py="md">
            No theme coding jobs yet. Create one and run it to develop
            themes from the codebook.
          </Text>
        </Card>
      ) : (
        <Card padding="md" withBorder>
          <Table verticalSpacing="xs" striped highlightOnHover>
            <Table.Thead>
              <Table.Tr>
                <Table.Th>Job</Table.Th>
                <Table.Th>Status</Table.Th>
                <Table.Th>Codebook</Table.Th>
                <Table.Th>Themes</Table.Th>
                <Table.Th>Created</Table.Th>
                <Table.Th>Prompt</Table.Th>
              </Table.Tr>
            </Table.Thead>
            <Table.Tbody>
              {jobs.data
                .slice()
                .sort((a, b) => b.id - a.id)
                .map((j) => (
                  <Table.Tr key={j.id}>
                    <Table.Td>
                      <Anchor
                        component={Link}
                        to={`/theme-coding-jobs/${j.id}`}
                      >
                        #{j.id}
                      </Anchor>
                    </Table.Td>
                    <Table.Td>
                      <Badge
                        color={STATUS_COLOR[j.run_status]}
                        variant="light"
                      >
                        {STATUS_LABEL[j.run_status]}
                      </Badge>
                    </Table.Td>
                    <Table.Td>
                      <Anchor
                        component={Link}
                        to={`/codebook/${j.codebook_used_id}`}
                      >
                        v{j.codebook_used_id}
                      </Anchor>
                    </Table.Td>
                    <Table.Td>
                      {j.run_status === "not_run" ||
                      j.run_status === "running" ? (
                        <Text size="xs" c="dimmed">
                          —
                        </Text>
                      ) : (
                        <>
                          {j.n_themes_active}
                          {j.n_themes !== j.n_themes_active && (
                            <Text span size="xs" c="dimmed">
                              {" "}
                              ({j.n_themes - j.n_themes_active} deleted)
                            </Text>
                          )}
                        </>
                      )}
                    </Table.Td>
                    <Table.Td>
                      <Text size="xs" c="dimmed">
                        {j.created_at?.slice(0, 19).replace("T", " ")}
                      </Text>
                    </Table.Td>
                    <Table.Td>
                      <Text size="xs" lineClamp={2}>
                        {j.prompt}
                      </Text>
                    </Table.Td>
                  </Table.Tr>
                ))}
            </Table.Tbody>
          </Table>
        </Card>
      )}
    </Stack>
  );
}
