import {
  ActionIcon,
  Alert,
  Badge,
  Button,
  Card,
  Checkbox,
  Code,
  Drawer,
  Group,
  Modal,
  ScrollArea,
  Stack,
  Table,
  Text,
  TextInput,
  Textarea,
  Title,
  Tooltip,
} from "@mantine/core";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { IconAlertTriangle, IconEye, IconPlus, IconTrash } from "@tabler/icons-react";
import { useState } from "react";
import { notifications } from "@mantine/notifications";
import { api } from "../api";
import { ErrorAlert } from "../components/ErrorAlert";
import { StatusBadge } from "../components/StatusBadge";
import { useConfirmDelete } from "../components/ConfirmDelete";

export function ThemeCoders() {
  const qc = useQueryClient();
  const coders = useQuery({
    queryKey: ["theme-coders"],
    queryFn: api.themeCoders,
  });
  const runs = useQuery({
    queryKey: ["theme-coder-runs"],
    queryFn: () => api.themeCoderRuns(),
  });
  const [opened, setOpened] = useState(false);
  const [form, setForm] = useState({ coder_id: "", identity: "" });
  const [force, setForce] = useState(true);
  const [viewRunId, setViewRunId] = useState<number | null>(null);

  const runDetail = useQuery({
    queryKey: ["theme-coder-run", viewRunId],
    queryFn: () => api.themeCoderRun(viewRunId!),
    enabled: viewRunId !== null,
  });

  const add = useMutation({
    mutationFn: () => api.addThemeCoder(form.coder_id, form.identity),
    onSuccess: () => {
      notifications.show({ message: "Theme coder added", color: "teal" });
      setOpened(false);
      setForm({ coder_id: "", identity: "" });
      qc.invalidateQueries({ queryKey: ["theme-coders"] });
    },
    onError: (e) =>
      notifications.show({
        message: e instanceof Error ? e.message : String(e),
        color: "red",
      }),
  });

  const delCoder = useMutation({
    mutationFn: (id: string) => api.deleteThemeCoder(id, force),
    onSuccess: () => {
      notifications.show({ message: "Theme coder removed", color: "teal" });
      qc.invalidateQueries({ queryKey: ["theme-coders"] });
      qc.invalidateQueries({ queryKey: ["theme-coder-runs"] });
    },
    onError: (e) =>
      notifications.show({
        message: e instanceof Error ? e.message : String(e),
        color: "red",
      }),
  });

  const delRun = useMutation({
    mutationFn: (id: number) => api.deleteThemeCoderRun(id),
    onSuccess: () => {
      notifications.show({ message: "Run deleted", color: "teal" });
      qc.invalidateQueries({ queryKey: ["theme-coder-runs"] });
    },
    onError: (e) =>
      notifications.show({
        message: e instanceof Error ? e.message : String(e),
        color: "red",
      }),
  });

  const confirm = useConfirmDelete();

  if (coders.error) return <ErrorAlert error={coders.error} />;
  if (runs.error) return <ErrorAlert error={runs.error} />;

  return (
    <Stack gap="md">
      {confirm.modal}
      <Modal
        opened={opened}
        onClose={() => setOpened(false)}
        title="Add theme coder"
        centered
      >
        <Stack>
          <TextInput
            label="Theme coder ID"
            value={form.coder_id}
            onChange={(e) =>
              setForm({ ...form, coder_id: e.currentTarget.value })
            }
            required
          />
          <Textarea
            label="Identity"
            autosize
            minRows={2}
            value={form.identity}
            onChange={(e) =>
              setForm({ ...form, identity: e.currentTarget.value })
            }
            required
          />
          <Group justify="flex-end">
            <Button variant="default" onClick={() => setOpened(false)}>
              Cancel
            </Button>
            <Button
              onClick={() => add.mutate()}
              loading={add.isPending}
              disabled={!form.coder_id || !form.identity}
            >
              Add
            </Button>
          </Group>
        </Stack>
      </Modal>

      <Drawer
        opened={viewRunId !== null}
        onClose={() => setViewRunId(null)}
        position="right"
        size="xl"
        title={`Run #${viewRunId} · ${runDetail.data?.theme_coder_id ?? ""}`}
      >
        {runDetail.isLoading && <Text>Loading…</Text>}
        {runDetail.error && <ErrorAlert error={runDetail.error} />}
        {runDetail.data && (
          <Stack>
            <Group>
              <StatusBadge status={runDetail.data.status} />
              <Text size="xs" c="dimmed">
                codebook v{runDetail.data.codebook_version}
              </Text>
            </Group>
            {runDetail.data.error && (
              <Alert
                color="red"
                variant="light"
                icon={<IconAlertTriangle size={16} />}
              >
                <Code>{runDetail.data.error}</Code>
              </Alert>
            )}
            {runDetail.data.result?.themes && (
              <Stack gap="sm">
                <Title order={5}>
                  Proposed themes ({runDetail.data.result.themes.length})
                </Title>
                {runDetail.data.result.themes.map((t, i) => (
                  <Card key={i} padding="sm" withBorder shadow="none">
                    <Text fw={600}>{t.name}</Text>
                    {t.description && (
                      <Text size="sm" c="dimmed" mt={4}>
                        {t.description}
                      </Text>
                    )}
                    {t.codes && t.codes.length > 0 && (
                      <Group gap={4} mt="xs">
                        {t.codes.map((c, j) => (
                          <Badge key={j} variant="light" size="xs">
                            {c}
                          </Badge>
                        ))}
                      </Group>
                    )}
                  </Card>
                ))}
              </Stack>
            )}
            {runDetail.data.raw_response && (
              <Card padding="sm" withBorder shadow="none">
                <Text size="xs" c="dimmed" mb={4}>
                  Raw LLM response
                </Text>
                <ScrollArea.Autosize mah={300}>
                  <Code block style={{ whiteSpace: "pre-wrap" }}>
                    {runDetail.data.raw_response}
                  </Code>
                </ScrollArea.Autosize>
              </Card>
            )}
          </Stack>
        )}
      </Drawer>

      <Group justify="space-between">
        <Title order={2}>Theme coders</Title>
        <Group>
          <Checkbox
            label="Force delete"
            checked={force}
            onChange={(e) => setForce(e.currentTarget.checked)}
          />
          <Button
            leftSection={<IconPlus size={16} />}
            onClick={() => setOpened(true)}
          >
            Add theme coder
          </Button>
        </Group>
      </Group>

      <Card padding={0}>
        <Table verticalSpacing="sm">
          <Table.Thead>
            <Table.Tr>
              <Table.Th>ID</Table.Th>
              <Table.Th>Identity</Table.Th>
              <Table.Th></Table.Th>
            </Table.Tr>
          </Table.Thead>
          <Table.Tbody>
            {coders.data?.map((c) => (
              <Table.Tr key={c.theme_coder_id}>
                <Table.Td>{c.theme_coder_id}</Table.Td>
                <Table.Td>
                  <Text size="sm">{c.identity}</Text>
                </Table.Td>
                <Table.Td>
                  <Tooltip label="Delete theme coder">
                    <ActionIcon
                      variant="subtle"
                      color="red"
                      onClick={() =>
                        confirm.ask({
                          title: `Delete ${c.theme_coder_id}?`,
                          body: force
                            ? "Force=true will cascade-delete all runs."
                            : "Refuses if the coder has any runs.",
                          onConfirm: () => delCoder.mutateAsync(c.theme_coder_id),
                        })
                      }
                    >
                      <IconTrash size={16} />
                    </ActionIcon>
                  </Tooltip>
                </Table.Td>
              </Table.Tr>
            ))}
            {coders.data && coders.data.length === 0 && (
              <Table.Tr>
                <Table.Td colSpan={3}>
                  <Text c="dimmed" ta="center" py="md">
                    No theme coders registered.
                  </Text>
                </Table.Td>
              </Table.Tr>
            )}
          </Table.Tbody>
        </Table>
      </Card>

      <Title order={3} mt="md">
        Theme coder runs
      </Title>
      <Card padding={0}>
        <Table verticalSpacing="xs" striped>
          <Table.Thead>
            <Table.Tr>
              <Table.Th>ID</Table.Th>
              <Table.Th>Coder</Table.Th>
              <Table.Th>Codebook</Table.Th>
              <Table.Th>Status</Table.Th>
              <Table.Th>Finished</Table.Th>
              <Table.Th>Result bytes</Table.Th>
              <Table.Th></Table.Th>
            </Table.Tr>
          </Table.Thead>
          <Table.Tbody>
            {runs.data?.map((r) => (
              <Table.Tr key={r.id}>
                <Table.Td>
                  <Text size="xs" c="dimmed">
                    #{r.id}
                  </Text>
                </Table.Td>
                <Table.Td>{r.theme_coder_id}</Table.Td>
                <Table.Td>v{r.codebook_version}</Table.Td>
                <Table.Td>
                  <StatusBadge status={r.status} />
                </Table.Td>
                <Table.Td>
                  <Text size="xs" c="dimmed">
                    {r.finished_at ?? "—"}
                  </Text>
                </Table.Td>
                <Table.Td>{r.result_bytes}</Table.Td>
                <Table.Td>
                  <Group gap={2}>
                    <Tooltip label="View run details">
                      <ActionIcon
                        variant="subtle"
                        onClick={() => setViewRunId(r.id)}
                      >
                        <IconEye size={16} />
                      </ActionIcon>
                    </Tooltip>
                    <Tooltip label="Delete run">
                      <ActionIcon
                        variant="subtle"
                        color="red"
                        onClick={() =>
                          confirm.ask({
                            title: `Delete run #${r.id}?`,
                            body: "The theme coder will re-run on the next theme-code pass.",
                            onConfirm: () => delRun.mutateAsync(r.id),
                          })
                        }
                      >
                        <IconTrash size={16} />
                      </ActionIcon>
                    </Tooltip>
                  </Group>
                </Table.Td>
              </Table.Tr>
            ))}
            {runs.data && runs.data.length === 0 && (
              <Table.Tr>
                <Table.Td colSpan={7}>
                  <Text c="dimmed" ta="center" py="md">
                    No theme coder runs yet.
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
