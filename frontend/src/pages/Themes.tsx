import {
  Button,
  Card,
  Collapse,
  Group,
  Select,
  Stack,
  Text,
  TextInput,
  Textarea,
  Title,
} from "@mantine/core";
import { IconPlus } from "@tabler/icons-react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useMemo, useState } from "react";
import { api, type ThemeFull } from "../api";
import { ErrorAlert } from "../components/ErrorAlert";
import { ThemeCard } from "../components/ThemeCard";
import { useConfirmDelete } from "../components/ConfirmDelete";

type SortKey =
  | "codes_desc"
  | "code_quotes_desc"
  | "title_asc"
  | "created_desc"
  | "created_asc";

const SORT_OPTIONS: { value: SortKey; label: string }[] = [
  { value: "codes_desc", label: "Codes (most first)" },
  { value: "code_quotes_desc", label: "Quotes attached to codes (most first)" },
  { value: "title_asc", label: "Title (A→Z)" },
  { value: "created_desc", label: "Newest first" },
  { value: "created_asc", label: "Oldest first" },
];

function totalCodeQuotes(t: ThemeFull): number {
  return t.codes.reduce((sum, c) => sum + c.n_quotes, 0);
}

function sortThemes(themes: ThemeFull[], key: SortKey): ThemeFull[] {
  const out = [...themes];
  switch (key) {
    case "codes_desc":
      out.sort((a, b) => b.codes.length - a.codes.length);
      break;
    case "code_quotes_desc":
      out.sort((a, b) => totalCodeQuotes(b) - totalCodeQuotes(a));
      break;
    case "title_asc":
      out.sort((a, b) => a.title.localeCompare(b.title));
      break;
    case "created_desc":
      out.sort((a, b) => (b.created_at ?? "").localeCompare(a.created_at ?? ""));
      break;
    case "created_asc":
      out.sort((a, b) => (a.created_at ?? "").localeCompare(b.created_at ?? ""));
      break;
  }
  return out;
}

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

  const [sortKey, setSortKey] = useState<SortKey>("codes_desc");
  const sortedThemes = useMemo(
    () => (themes.data ? sortThemes(themes.data, sortKey) : undefined),
    [themes.data, sortKey],
  );

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
        <Group gap="sm" align="flex-end">
          <Select
            label="Sort by"
            size="xs"
            data={SORT_OPTIONS}
            value={sortKey}
            onChange={(v) => v && setSortKey(v as SortKey)}
            allowDeselect={false}
            w={240}
          />
          <Button
            leftSection={<IconPlus size={16} />}
            variant={showAdd ? "default" : "filled"}
            onClick={() => setShowAdd((v) => !v)}
          >
            {showAdd ? "Cancel" : "Add manual theme"}
          </Button>
        </Group>
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
      ) : !sortedThemes || sortedThemes.length === 0 ? (
        <Card padding="md" withBorder>
          <Text c="dimmed" ta="center" py="md">
            No themes yet. Run a theme coding job or add one manually.
          </Text>
        </Card>
      ) : (
        <Stack gap="sm">
          {sortedThemes.map((t) => (
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
