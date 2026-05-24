import {
  Button,
  Card,
  Collapse,
  Group,
  Stack,
  Text,
  TextInput,
  Textarea,
  Title,
} from "@mantine/core";
import { IconPlus } from "@tabler/icons-react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { api } from "../api";
import { ErrorAlert } from "../components/ErrorAlert";
import { ThemeCard } from "../components/ThemeCard";
import { useConfirmDelete } from "../components/ConfirmDelete";

export function ThemesPage() {
  const qc = useQueryClient();
  const { ask, modal } = useConfirmDelete();

  const themes = useQuery({
    queryKey: ["themes"],
    queryFn: api.themes,
  });

  const del = useMutation({
    mutationFn: (id: number) => api.deleteTheme(id),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["themes"] });
    },
  });

  const [showAdd, setShowAdd] = useState(false);
  const [title, setTitle] = useState("");
  const [description, setDescription] = useState("");
  const [rationale, setRationale] = useState("");
  const create = useMutation({
    mutationFn: () => api.createManualTheme({ title, description, rationale }),
    onSuccess: () => {
      setTitle("");
      setDescription("");
      setRationale("");
      setShowAdd(false);
      qc.invalidateQueries({ queryKey: ["themes"] });
    },
  });

  if (themes.error) return <ErrorAlert error={themes.error} />;

  return (
    <Stack gap="md">
      <Group justify="space-between" align="flex-end">
        <div>
          <Title order={2}>Themes</Title>
          <Text c="dimmed" size="sm">
            All active themes — produced by theme coding jobs, aggregator
            runs, or added manually. Themes that have been merged into a
            newer one, or marked deleted, are hidden.
          </Text>
        </div>
        <Button
          leftSection={<IconPlus size={16} />}
          variant={showAdd ? "default" : "filled"}
          onClick={() => setShowAdd((v) => !v)}
        >
          {showAdd ? "Cancel" : "Add manual theme"}
        </Button>
      </Group>

      <Collapse in={showAdd}>
        <Card padding="md" withBorder>
          <Stack gap="sm">
            <Title order={4}>Add a manual theme</Title>
            <Text size="xs" c="dimmed">
              Hand-curated themes have no codes or quotes attached and do
              not pin a codebook revision.
            </Text>
            <TextInput
              label="Title"
              value={title}
              onChange={(e) => setTitle(e.currentTarget.value)}
              required
            />
            <Textarea
              label="Description"
              value={description}
              onChange={(e) => setDescription(e.currentTarget.value)}
              autosize
              minRows={2}
            />
            <Textarea
              label="Rationale"
              value={rationale}
              onChange={(e) => setRationale(e.currentTarget.value)}
              autosize
              minRows={2}
            />
            {create.error && <ErrorAlert error={create.error} />}
            <Group justify="flex-end">
              <Button
                disabled={!title.trim() || create.isPending}
                loading={create.isPending}
                onClick={() => create.mutate()}
              >
                Create theme
              </Button>
            </Group>
          </Stack>
        </Card>
      </Collapse>

      {themes.isLoading ? (
        <Text c="dimmed">Loading…</Text>
      ) : !themes.data || themes.data.length === 0 ? (
        <Card padding="md" withBorder>
          <Text c="dimmed" ta="center" py="md">
            No themes yet. Run a theme coding job or add one manually.
          </Text>
        </Card>
      ) : (
        <Stack gap="sm">
          {themes.data.map((t) => (
            <ThemeCard
              key={t.theme_id}
              theme={t}
              showSourceBadge
              showJobLink
              onDelete={() =>
                ask({
                  title: "Delete theme?",
                  body: (
                    <>
                      Mark <b>{t.title}</b> as deleted? It will be hidden
                      from this page but kept in the source job's history.
                    </>
                  ),
                  onConfirm: () => del.mutateAsync(t.theme_id),
                })
              }
            />
          ))}
        </Stack>
      )}
      {modal}
    </Stack>
  );
}
