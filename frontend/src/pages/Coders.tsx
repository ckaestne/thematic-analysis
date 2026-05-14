import {
  ActionIcon,
  Anchor,
  Button,
  Card,
  Checkbox,
  Group,
  Modal,
  Stack,
  Table,
  Text,
  TextInput,
  Textarea,
  Title,
  Tooltip,
} from "@mantine/core";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { IconPlus, IconTrash } from "@tabler/icons-react";
import { useState } from "react";
import { Link } from "react-router-dom";
import { notifications } from "@mantine/notifications";
import { api } from "../api";
import { ErrorAlert } from "../components/ErrorAlert";
import { useConfirmDelete } from "../components/ConfirmDelete";

export function Coders() {
  const qc = useQueryClient();
  const { data, error, isLoading } = useQuery({
    queryKey: ["coders"],
    queryFn: api.coders,
  });
  const [opened, setOpened] = useState(false);
  const [form, setForm] = useState({ coder_id: "", identity: "" });
  const [force, setForce] = useState(true);

  const add = useMutation({
    mutationFn: () => api.addCoder(form.coder_id, form.identity),
    onSuccess: () => {
      notifications.show({ message: "Coder added", color: "teal" });
      setOpened(false);
      setForm({ coder_id: "", identity: "" });
      qc.invalidateQueries({ queryKey: ["coders"] });
      qc.invalidateQueries({ queryKey: ["status"] });
    },
    onError: (e) =>
      notifications.show({
        message: e instanceof Error ? e.message : String(e),
        color: "red",
      }),
  });

  const del = useMutation({
    mutationFn: (id: string) => api.deleteCoder(id, force),
    onSuccess: () => {
      notifications.show({ message: "Coder removed", color: "teal" });
      qc.invalidateQueries({ queryKey: ["coders"] });
      qc.invalidateQueries({ queryKey: ["status"] });
    },
    onError: (e) =>
      notifications.show({
        message: e instanceof Error ? e.message : String(e),
        color: "red",
      }),
  });

  const confirm = useConfirmDelete();

  if (error) return <ErrorAlert error={error} />;

  return (
    <Stack gap="md">
      {confirm.modal}
      <Modal
        opened={opened}
        onClose={() => setOpened(false)}
        title="Add coder"
        centered
      >
        <Stack>
          <TextInput
            label="Coder ID"
            value={form.coder_id}
            onChange={(e) =>
              setForm({ ...form, coder_id: e.currentTarget.value })
            }
            required
          />
          <Textarea
            label="Identity"
            description="Analytical perspective shown to the agent."
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

      <Group justify="space-between">
        <Title order={2}>Coders</Title>
        <Group>
          <Checkbox
            label="Force delete (drop runs)"
            checked={force}
            onChange={(e) => setForce(e.currentTarget.checked)}
          />
          <Button
            leftSection={<IconPlus size={16} />}
            onClick={() => setOpened(true)}
          >
            Add coder
          </Button>
        </Group>
      </Group>

      <Card padding={0}>
        <Table verticalSpacing="sm" highlightOnHover>
          <Table.Thead>
            <Table.Tr>
              <Table.Th>ID</Table.Th>
              <Table.Th>Identity</Table.Th>
              <Table.Th>Created</Table.Th>
              <Table.Th></Table.Th>
            </Table.Tr>
          </Table.Thead>
          <Table.Tbody>
            {isLoading && (
              <Table.Tr>
                <Table.Td colSpan={4}>Loading…</Table.Td>
              </Table.Tr>
            )}
            {data?.map((c) => (
              <Table.Tr key={c.coder_id}>
                <Table.Td>
                  <Anchor component={Link} to={`/coder-runs?coder_id=${c.coder_id}`}>
                    {c.coder_id}
                  </Anchor>
                </Table.Td>
                <Table.Td>
                  <Text size="sm">{c.identity}</Text>
                </Table.Td>
                <Table.Td>
                  <Text size="xs" c="dimmed">
                    {c.created_at}
                  </Text>
                </Table.Td>
                <Table.Td>
                  <Tooltip label="Delete coder">
                    <ActionIcon
                      variant="subtle"
                      color="red"
                      onClick={() =>
                        confirm.ask({
                          title: `Delete coder ${c.coder_id}?`,
                          body: force
                            ? "Deletes the coder and ALL their runs (force=true)."
                            : "Refuses if the coder has any runs. Enable 'Force delete' to cascade.",
                          onConfirm: () => del.mutateAsync(c.coder_id),
                        })
                      }
                    >
                      <IconTrash size={16} />
                    </ActionIcon>
                  </Tooltip>
                </Table.Td>
              </Table.Tr>
            ))}
            {data && data.length === 0 && (
              <Table.Tr>
                <Table.Td colSpan={4}>
                  <Text c="dimmed" ta="center" py="md">
                    No coders registered.
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
