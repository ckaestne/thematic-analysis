import {
  Anchor,
  Card,
  Code,
  Grid,
  Group,
  SimpleGrid,
  Stack,
  Table,
  Text,
  Title,
  Tooltip,
} from "@mantine/core";
import { useQuery } from "@tanstack/react-query";
import {
  IconBook2,
  IconCategory,
  IconClipboardCheck,
  IconDatabase,
  IconStack2,
  IconUsers,
} from "@tabler/icons-react";
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

export function Overview() {
  const { data, error, isLoading } = useQuery({
    queryKey: ["status"],
    queryFn: api.status,
  });

  if (error) return <ErrorAlert error={error} />;
  if (isLoading || !data) return <Text c="dimmed">Loading…</Text>;

  const { stage1, stage2, per_coder, per_theme_coder } = data;

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

      <SimpleGrid cols={{ base: 2, sm: 3, md: 6 }} spacing="md">
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
        <StatCard
          label="Themes"
          value={stage2.themes_in_result}
          hint={`v${stage2.codebook_version}`}
          icon={<IconCategory size={28} />}
        />
      </SimpleGrid>

      <Grid>
        <Grid.Col span={{ base: 12, md: 6 }}>
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
        <Grid.Col span={{ base: 12, md: 6 }}>
          <Card padding="md">
            <Stack gap="sm">
              <Title order={4}>Stage 2 progress</Title>
              <Text size="sm" c="dimmed">
                Theme coder runs (codebook v{stage2.codebook_version})
              </Text>
              <StatusBar counts={stage2.theme_coder_runs_by_status} />
              <Text size="sm" c="dimmed" mt="xs">
                Theme aggregations
              </Text>
              <StatusBar counts={stage2.theme_aggregations_by_status} />
            </Stack>
          </Card>
        </Grid.Col>
      </Grid>

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
                  <Table.Td>
                    <Anchor
                      component={Link}
                      to={`/coder-runs?coder_id=${c.coder_id}`}
                    >
                      {c.coder_id}
                    </Anchor>
                  </Table.Td>
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

      <Card padding="md">
        <Title order={4} mb="sm">
          Per-theme-coder progress (Stage 2)
        </Title>
        {per_theme_coder.length === 0 ? (
          <Text c="dimmed" size="sm">
            No theme coders registered.
          </Text>
        ) : (
          <Table verticalSpacing="xs" striped>
            <Table.Thead>
              <Table.Tr>
                <Table.Th>Coder</Table.Th>
                <Table.Th>Identity</Table.Th>
                <Table.Th>Status</Table.Th>
                <Table.Th>Finished</Table.Th>
              </Table.Tr>
            </Table.Thead>
            <Table.Tbody>
              {per_theme_coder.map((c) => (
                <Table.Tr key={c.theme_coder_id}>
                  <Table.Td>{c.theme_coder_id}</Table.Td>
                  <Table.Td>
                    <Tooltip label={c.identity} multiline w={300}>
                      <Text size="sm" lineClamp={1}>
                        {c.identity}
                      </Text>
                    </Tooltip>
                  </Table.Td>
                  <Table.Td>{c.run?.status ?? "not started"}</Table.Td>
                  <Table.Td>{c.run?.finished_at ?? "—"}</Table.Td>
                </Table.Tr>
              ))}
            </Table.Tbody>
          </Table>
        )}
      </Card>
    </Stack>
  );
}
