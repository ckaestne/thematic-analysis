import {
  Anchor,
  Button,
  Card,
  Group,
  Stack,
  Table,
  Text,
  Title,
} from "@mantine/core";
import { IconPlus } from "@tabler/icons-react";
import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { api } from "../api";
import { ErrorAlert } from "../components/ErrorAlert";

export function ThemeCodingJobsPage() {
  const jobs = useQuery({
    queryKey: ["theme-coding-jobs"],
    queryFn: api.themeCodingJobs,
  });

  if (jobs.error) return <ErrorAlert error={jobs.error} />;

  return (
    <Stack gap="md">
      <Group justify="space-between" align="flex-end">
        <div>
          <Title order={2}>Theme coding jobs</Title>
          <Text c="dimmed" size="sm">
            Each job runs an LLM theme coder once over a chosen codebook
            revision with a researcher-supplied prompt, then stores the
            themes it produces.
          </Text>
        </div>
        <Button
          component={Link}
          to="/theme-coding-jobs/new"
          leftSection={<IconPlus size={16} />}
        >
          New job
        </Button>
      </Group>

      {jobs.isLoading ? (
        <Text c="dimmed">Loading…</Text>
      ) : !jobs.data || jobs.data.length === 0 ? (
        <Card padding="md" withBorder>
          <Text c="dimmed" ta="center" py="md">
            No theme coding jobs yet. Start one to develop themes from the
            codebook.
          </Text>
        </Card>
      ) : (
        <Card padding="md" withBorder>
          <Table verticalSpacing="xs" striped highlightOnHover>
            <Table.Thead>
              <Table.Tr>
                <Table.Th>Job</Table.Th>
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
                      <Anchor
                        component={Link}
                        to={`/codebook/${j.codebook_used_id}`}
                      >
                        v{j.codebook_used_id}
                      </Anchor>
                    </Table.Td>
                    <Table.Td>
                      {j.n_themes_active}
                      {j.n_themes !== j.n_themes_active && (
                        <Text span size="xs" c="dimmed">
                          {" "}
                          ({j.n_themes - j.n_themes_active} deleted)
                        </Text>
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
