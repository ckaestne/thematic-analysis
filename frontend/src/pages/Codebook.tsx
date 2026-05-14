import {
  Anchor,
  Badge,
  Card,
  Code,
  Grid,
  Group,
  ScrollArea,
  Stack,
  Table,
  Text,
  Title,
} from "@mantine/core";
import { useQuery } from "@tanstack/react-query";
import { useEffect } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { api } from "../api";
import { ErrorAlert } from "../components/ErrorAlert";

export function CodebookPage() {
  const navigate = useNavigate();
  const { version: versionParam } = useParams();
  const versions = useQuery({
    queryKey: ["codebook-versions"],
    queryFn: api.codebookVersions,
  });

  const selectedVersion = versionParam
    ? parseInt(versionParam, 10)
    : versions.data?.[0]?.version;

  const detail = useQuery({
    queryKey: ["codebook-version", selectedVersion],
    queryFn: () => api.codebookVersion(selectedVersion!),
    enabled: !!selectedVersion,
  });

  useEffect(() => {
    if (!versionParam && versions.data && versions.data.length > 0) {
      navigate(`/codebook/${versions.data[0].version}`, { replace: true });
    }
  }, [versionParam, versions.data, navigate]);

  if (versions.error) return <ErrorAlert error={versions.error} />;

  return (
    <Stack gap="md">
      <Title order={2}>Codebook</Title>
      <Grid>
        <Grid.Col span={{ base: 12, md: 4 }}>
          <Card padding={0}>
            <ScrollArea h={600}>
              <Table verticalSpacing="xs" highlightOnHover>
                <Table.Thead>
                  <Table.Tr>
                    <Table.Th>Version</Table.Th>
                    <Table.Th ta="right">Codes</Table.Th>
                    <Table.Th>By</Table.Th>
                  </Table.Tr>
                </Table.Thead>
                <Table.Tbody>
                  {versions.data?.map((v) => (
                    <Table.Tr
                      key={v.version}
                      bg={
                        v.version === selectedVersion
                          ? "var(--mantine-color-indigo-light)"
                          : undefined
                      }
                    >
                      <Table.Td>
                        <Anchor component={Link} to={`/codebook/${v.version}`}>
                          v{v.version}
                        </Anchor>
                      </Table.Td>
                      <Table.Td ta="right">
                        <Badge variant="light">{v.n_codes}</Badge>
                      </Table.Td>
                      <Table.Td>
                        <Text size="xs" c="dimmed" lineClamp={1}>
                          {v.created_by}
                        </Text>
                      </Table.Td>
                    </Table.Tr>
                  ))}
                  {versions.data && versions.data.length === 0 && (
                    <Table.Tr>
                      <Table.Td colSpan={3}>
                        <Text c="dimmed" ta="center" py="md">
                          No codebook versions.
                        </Text>
                      </Table.Td>
                    </Table.Tr>
                  )}
                </Table.Tbody>
              </Table>
            </ScrollArea>
          </Card>
        </Grid.Col>
        <Grid.Col span={{ base: 12, md: 8 }}>
          {detail.error ? (
            <ErrorAlert error={detail.error} />
          ) : !detail.data ? (
            <Text c="dimmed">Select a version.</Text>
          ) : (
            <Stack gap="sm">
              <Card padding="md">
                <Group justify="space-between">
                  <Stack gap={2}>
                    <Title order={3}>Version {detail.data.version}</Title>
                    <Text size="xs" c="dimmed">
                      {detail.data.created_at} · by {detail.data.created_by}
                      {detail.data.parent_version != null && (
                        <>
                          {" "}
                          · parent{" "}
                          <Anchor
                            component={Link}
                            to={`/codebook/${detail.data.parent_version}`}
                            size="xs"
                          >
                            v{detail.data.parent_version}
                          </Anchor>
                        </>
                      )}
                    </Text>
                  </Stack>
                  <Badge variant="light" size="lg">
                    {detail.data.codebook.codes.length} codes
                  </Badge>
                </Group>
              </Card>
              {detail.data.codebook.codes.length === 0 ? (
                <Card padding="md">
                  <Text c="dimmed" ta="center" py="md">
                    Empty codebook.
                  </Text>
                </Card>
              ) : (
                detail.data.codebook.codes.map((c, i) => (
                  <Card key={i} padding="md">
                    <Group justify="space-between" mb="xs">
                      <Code style={{ fontSize: 14, fontWeight: 600 }}>
                        {c.code}
                      </Code>
                      <Text size="xs" c="dimmed">
                        {c.quotes.length} quote(s)
                      </Text>
                    </Group>
                    <Stack gap={6}>
                      {c.quotes.slice(0, 8).map((q, j) => (
                        <div
                          key={j}
                          style={{
                            borderLeft: "3px solid var(--mantine-color-indigo-6)",
                            padding: "4px 10px",
                            background: "var(--mantine-color-default-hover)",
                            borderRadius: 3,
                          }}
                        >
                          <Text size="xs" c="dimmed" ff="monospace">
                            {q.quote_id}
                          </Text>
                          <Text size="sm">{q.text}</Text>
                        </div>
                      ))}
                      {c.quotes.length > 8 && (
                        <Text size="xs" c="dimmed">
                          …and {c.quotes.length - 8} more
                        </Text>
                      )}
                    </Stack>
                  </Card>
                ))
              )}
            </Stack>
          )}
        </Grid.Col>
      </Grid>
    </Stack>
  );
}
